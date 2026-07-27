import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.config import Settings


class LLMProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMProvider:
    id: str
    base_url: str
    api_key: str
    model: str
    priority: int = 100


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0


class LLMProviderPool:
    STAGES = {
        "planning",
        "sql_generation",
        "pre_execution_review",
        "result_review",
    }

    def __init__(
        self,
        providers: list[LLMProvider],
        stage_routes: dict[str, list[str]],
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        max_attempts: int,
    ) -> None:
        if not providers:
            raise LLMProviderConfigurationError("未配置可用的大模型供应商")
        self._providers = {provider.id: provider for provider in providers}
        self._default_route = [
            provider.id for provider in sorted(providers, key=lambda item: item.priority)
        ]
        self._stage_routes = stage_routes
        self._health = {provider.id: ProviderHealth() for provider in providers}
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_attempts = max_attempts

    @classmethod
    def from_settings(
        cls, settings: Settings, *, base_dir: Path | None = None
    ) -> "LLMProviderPool":
        if settings.llm_providers_path is None:
            if not settings.openai_base_url or not settings.openai_api_key or not settings.openai_model:
                raise LLMProviderConfigurationError("OpenAI 兼容接口配置不完整")
            providers = [
                LLMProvider(
                    id="legacy",
                    base_url=settings.openai_base_url,
                    api_key=settings.openai_api_key,
                    model=settings.openai_model,
                    priority=1,
                )
            ]
            routes: dict[str, list[str]] = {}
        else:
            path = settings.llm_providers_path
            if not path.is_absolute():
                path = (base_dir or Path.cwd()) / path
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LLMProviderConfigurationError(
                    f"无法读取大模型供应商配置: {path}"
                ) from exc
            dotenv_path = (base_dir or Path.cwd()) / ".env"
            dotenv_secrets = {
                str(key): str(value)
                for key, value in dotenv_values(dotenv_path).items()
                if value is not None
            }
            providers = cls._parse_providers(payload, dotenv_secrets)
            routes = cls._parse_routes(payload, {item.id for item in providers})
        return cls(
            providers,
            routes,
            failure_threshold=settings.llm_circuit_failure_threshold,
            cooldown_seconds=settings.llm_circuit_cooldown_seconds,
            max_attempts=settings.llm_max_provider_attempts,
        )

    @staticmethod
    def _parse_providers(
        payload: dict[str, Any], dotenv_secrets: dict[str, str] | None = None
    ) -> list[LLMProvider]:
        result: list[LLMProvider] = []
        seen: set[str] = set()
        for raw in payload.get("providers", []):
            if raw.get("enabled", True) is not True:
                continue
            provider_id = str(raw.get("id", "")).strip()
            base_url = str(raw.get("base_url", "")).strip()
            model = str(raw.get("model", "")).strip()
            api_key_env = str(raw.get("api_key_env", "")).strip()
            api_key = (
                os.getenv(api_key_env)
                or (dotenv_secrets or {}).get(api_key_env)
                or ""
            ).strip()
            if not provider_id or provider_id in seen:
                raise LLMProviderConfigurationError("供应商 id 缺失或重复")
            if not base_url or not model or not api_key_env or not api_key:
                raise LLMProviderConfigurationError(
                    f"供应商 {provider_id} 的地址、模型或密钥环境变量不完整"
                )
            seen.add(provider_id)
            result.append(
                LLMProvider(
                    id=provider_id,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    priority=int(raw.get("priority", 100)),
                )
            )
        return result

    @classmethod
    def _parse_routes(
        cls, payload: dict[str, Any], provider_ids: set[str]
    ) -> dict[str, list[str]]:
        routes: dict[str, list[str]] = {}
        for stage, raw_route in payload.get("stage_routes", {}).items():
            if stage not in cls.STAGES or not isinstance(raw_route, list):
                raise LLMProviderConfigurationError(f"无效的阶段路由: {stage}")
            route = [str(item) for item in raw_route]
            unknown = set(route) - provider_ids
            if unknown:
                raise LLMProviderConfigurationError(
                    f"阶段 {stage} 引用了未知供应商: {sorted(unknown)}"
                )
            routes[stage] = route
        return routes

    def candidates(self, stage: str) -> list[LLMProvider]:
        now = time.monotonic()
        route = self._stage_routes.get(stage, self._default_route)
        available = [
            self._providers[provider_id]
            for provider_id in route
            if self._health[provider_id].circuit_open_until <= now
        ]
        if not available:
            # Allow the provider whose cooldown expires first to probe recovery.
            provider_id = min(
                route, key=lambda item: self._health[item].circuit_open_until
            )
            available = [self._providers[provider_id]]
        return available[: self.max_attempts]

    def record_success(self, provider_id: str) -> None:
        self._health[provider_id] = ProviderHealth()

    def record_failure(self, provider_id: str) -> None:
        health = self._health[provider_id]
        health.consecutive_failures += 1
        if health.consecutive_failures >= self.failure_threshold:
            health.circuit_open_until = time.monotonic() + self.cooldown_seconds

    def snapshot(self) -> list[dict[str, str]]:
        now = time.monotonic()
        return [
            {
                "id": provider.id,
                "model": provider.model,
                "state": (
                    "cooldown"
                    if self._health[provider.id].circuit_open_until > now
                    else "available"
                ),
            }
            for provider in sorted(self._providers.values(), key=lambda item: item.priority)
        ]
