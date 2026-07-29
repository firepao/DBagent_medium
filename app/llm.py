import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeVar

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.llm_trace import LLMTraceRepository
from app.llm_providers import (
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderPool,
)
from app.models import PreExecutionReview, QueryPlan, ResultReview
from app.prompts import PromptRegistry


class LLMConfigurationError(RuntimeError):
    pass


class LLMResponseError(RuntimeError):
    pass


class LLMUpstreamUnavailableError(LLMResponseError):
    pass


class LLMTimeoutError(LLMResponseError):
    pass


class LLMInvalidResponseError(LLMResponseError):
    pass


class LLMPlanSchemaError(LLMResponseError):
    pass


class LLMReviewSchemaError(LLMResponseError):
    pass


class QueryLLM(Protocol):
    async def plan(self, question: str, planning_context: str) -> QueryPlan: ...

    async def generate_sql(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        *,
        task_mode: str = "initial",
        previous_sql: str | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> str: ...

    async def review_before_execution(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        candidate_sql: str,
        expected_result_contract: dict[str, Any],
    ) -> PreExecutionReview: ...

    async def review_result(
        self,
        question: str,
        plan: QueryPlan,
        expected_result_contract: dict[str, Any],
        evidence: dict[str, Any],
    ) -> ResultReview: ...


T = TypeVar("T")


class OpenAIQueryLLM:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        prompts: PromptRegistry | Mapping[str, str] | None = None,
        trace_repository: LLMTraceRepository | None = None,
        provider_pool: LLMProviderPool | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self.trace_repository = trace_repository
        try:
            self.provider_pool = provider_pool or LLMProviderPool.from_settings(
                settings, base_dir=base_dir
            )
        except LLMProviderConfigurationError as exc:
            raise LLMConfigurationError(str(exc)) from exc
        if prompts is None:
            self.prompts = PromptRegistry.from_file(settings.prompts_path)
        elif isinstance(prompts, PromptRegistry):
            self.prompts = prompts
        else:
            self.prompts = PromptRegistry("test", prompts)

    @property
    def is_configured(self) -> bool:
        return True

    def _ensure_configured(self) -> None:
        if not self.provider_pool:
            raise LLMConfigurationError("大模型供应商池配置不完整")

    async def _chat(
        self,
        stage: str,
        system: str,
        user: str,
        validator: Callable[[str], T] | None = None,
    ) -> str | T:
        self._ensure_configured()
        last_error: Exception | None = None
        for provider in self.provider_pool.candidates(stage):
            for attempt in range(self.settings.llm_provider_retry_count + 1):
                try:
                    normalized = await self._chat_provider(provider, system, user)
                    validated = validator(normalized) if validator else normalized
                    self.provider_pool.record_success(provider.id)
                    if self.trace_repository is not None:
                        self.trace_repository.record_output(
                            provider.model, normalized, provider=provider.id
                        )
                    return validated
                except httpx.TimeoutException as exc:
                    last_error = exc
                    self._record_error(provider, "timeout")
                    if attempt < self.settings.llm_provider_retry_count:
                        await asyncio.sleep(0.2)
                        continue
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status = exc.response.status_code
                    self._record_error(provider, f"upstream_http_{status}")
                    if status in {400, 422}:
                        raise LLMInvalidResponseError(
                            f"模型接口拒绝请求（HTTP {status}）"
                        ) from exc
                    if (
                        status in {408, 429, 500, 502, 503, 504}
                        and attempt < self.settings.llm_provider_retry_count
                    ):
                        await asyncio.sleep(0.2)
                        continue
                except httpx.HTTPError as exc:
                    last_error = exc
                    self._record_error(provider, "upstream_http_error")
                    if attempt < self.settings.llm_provider_retry_count:
                        await asyncio.sleep(0.2)
                        continue
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    last_error = exc
                    self._record_error(provider, "invalid_response")
                except LLMInvalidResponseError as exc:
                    last_error = exc
                    self._record_error(provider, "empty_content")
                except (LLMPlanSchemaError, LLMReviewSchemaError) as exc:
                    last_error = exc
                    self._record_error(provider, "schema_invalid")
                break
            self.provider_pool.record_failure(provider.id)
        if isinstance(last_error, (LLMPlanSchemaError, LLMReviewSchemaError)):
            raise last_error
        if isinstance(last_error, httpx.TimeoutException):
            raise LLMTimeoutError("所有模型供应商均调用超时") from last_error
        raise LLMUpstreamUnavailableError(
            "所有模型供应商均不可用"
        ) from last_error

    async def _chat_provider(
        self, provider: LLMProvider, system: str, user: str
    ) -> str:
        request_body: dict[str, Any] = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if provider.reasoning is False:
            # DeepSeek V4 OpenAI-compatible endpoints use this payload to
            # explicitly disable thinking instead of relying on provider defaults.
            request_body["thinking"] = {"type": "disabled"}
        response = await self.client.post(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        message = payload["choices"][0]["message"]
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            if provider.reasoning is False:
                raise LLMInvalidResponseError("模型接口在关闭思考模式后返回空内容")
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                content = reasoning
            else:
                raise LLMInvalidResponseError("模型接口返回空内容")
        return content.strip()

    def _record_error(self, provider: LLMProvider, error_type: str) -> None:
        if self.trace_repository is not None:
            self.trace_repository.record_error(
                provider.model, error_type, provider=provider.id
            )

    async def plan(self, question: str, planning_context: str) -> QueryPlan:
        system = self.prompts.get("planner")
        user = json.dumps(
            {"question": question, "planning_context": planning_context},
            ensure_ascii=False,
        )
        def validate(content: str) -> QueryPlan:
            try:
                return QueryPlan.model_validate_json(self._strip_json_fence(content))
            except ValidationError as exc:
                raise LLMPlanSchemaError("查询规划结果不符合约定结构") from exc

        return await self._chat("planning", system, user, validate)

    async def generate_sql(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        *,
        task_mode: str = "initial",
        previous_sql: str | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> str:
        system = self.prompts.get("sql_generator")
        user = "\n\n".join(
            [
                f"用户问题:\n{question}",
                f"任务模式:\n{task_mode}",
                f"查询计划:\n{plan.model_dump_json()}",
                f"受控目录:\n{context}",
                f"上一次候选 SQL:\n{previous_sql or '无'}",
                f"服务端反馈:\n{json.dumps(feedback or {}, ensure_ascii=False)}",
                "initial 模式生成首次 SQL。其他模式只修改反馈指出的问题；仍只返回一条 SQL。",
            ]
        )
        return self._strip_sql_fence(
            await self._chat("sql_generation", system, user)
        )

    async def review_before_execution(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        candidate_sql: str,
        expected_result_contract: dict[str, Any],
    ) -> PreExecutionReview:
        system = self.prompts.get("pre_execution_reviewer")
        user = json.dumps(
            {
                "question": question,
                "query_plan": plan.model_dump(),
                "controlled_context": context,
                "candidate_sql": candidate_sql,
                "expected_result_contract": expected_result_contract,
            },
            ensure_ascii=False,
        )
        def validate(content: str) -> PreExecutionReview:
            try:
                review = PreExecutionReview.model_validate_json(
                    self._strip_json_fence(content)
                )
            except ValidationError as exc:
                raise LLMReviewSchemaError("执行前审核结果不符合约定结构") from exc
            if review.decision == "clarification" and not review.clarification:
                raise LLMReviewSchemaError("执行前审核未提供澄清问题")
            return review

        return await self._chat("pre_execution_review", system, user, validate)

    async def review_result(
        self,
        question: str,
        plan: QueryPlan,
        expected_result_contract: dict[str, Any],
        evidence: dict[str, Any],
    ) -> ResultReview:
        system = self.prompts.get("result_reviewer")
        user = json.dumps(
            {
                "question": question,
                "query_plan": plan.model_dump(),
                "expected_result_contract": expected_result_contract,
                "result_evidence": evidence,
            },
            ensure_ascii=False,
        )
        def validate(content: str) -> ResultReview:
            try:
                review = ResultReview.model_validate_json(
                    self._strip_json_fence(content)
                )
            except ValidationError as exc:
                raise LLMReviewSchemaError("执行后结果审核不符合约定结构") from exc
            if review.decision == "clarification" and not review.clarification:
                raise LLMReviewSchemaError("执行后审核未提供澄清问题")
            return review

        return await self._chat("result_review", system, user, validate)

    @staticmethod
    def _strip_json_fence(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```json") and stripped.endswith("```"):
            return stripped[7:-3].strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            return stripped[3:-3].strip()
        return stripped

    @staticmethod
    def _strip_sql_fence(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```sql") and stripped.endswith("```"):
            return stripped[6:-3].strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            return stripped[3:-3].strip()
        return stripped

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


__all__ = [
    "LLMConfigurationError",
    "LLMInvalidResponseError",
    "LLMPlanSchemaError",
    "LLMResponseError",
    "LLMReviewSchemaError",
    "LLMTimeoutError",
    "LLMUpstreamUnavailableError",
    "OpenAIQueryLLM",
    "QueryLLM",
    "QueryPlan",
]
