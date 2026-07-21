import json
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models import QueryPlan


class LLMConfigurationError(RuntimeError):
    pass


class LLMResponseError(RuntimeError):
    pass


class QueryLLM(Protocol):
    async def plan(self, question: str, planning_context: str) -> QueryPlan: ...

    async def generate_sql(
        self, question: str, plan: QueryPlan, context: str
    ) -> str: ...


class OpenAIQueryLLM:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=60.0)

    @property
    def is_configured(self) -> bool:
        return self.settings.llm_configured

    def _ensure_configured(self) -> None:
        if not self.settings.llm_configured:
            raise LLMConfigurationError("OpenAI 兼容接口配置不完整")

    async def _chat(self, system: str, user: str) -> str:
        self._ensure_configured()
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        try:
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openai_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("模型接口返回无效响应") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("模型接口返回空内容")
        return content.strip()

    async def plan(self, question: str, planning_context: str) -> QueryPlan:
        system = (
            "你是数据查询规划器。只返回一个 JSON 对象，不要返回 Markdown。"
            "query_type 只能是 aggregation、list、ranking、detail、comparison、time_series；"
            "table_hints 只能从轻量表卡中选择 1 到 4 张已发布表；limit 范围为 1 到 100。"
            "若问题无法确定必要条件，设置 requires_clarification=true 并给出"
            "clarification_question，但仍需选择最相关的已发布表。"
        )
        user = json.dumps(
            {"question": question, "planning_context": planning_context},
            ensure_ascii=False,
        )
        content = self._strip_json_fence(await self._chat(system, user))
        try:
            return QueryPlan.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError("查询规划结果不符合约定结构") from exc

    async def generate_sql(
        self, question: str, plan: QueryPlan, context: str
    ) -> str:
        system = (
            "你是 SQLite 查询生成器。只返回一条 SELECT 或 WITH...SELECT 查询，"
            "不要返回 Markdown、注释、解释或多条语句。只能使用上下文列出的表和字段。"
        )
        user = "\n\n".join(
            [
                f"用户问题:\n{question}",
                f"查询计划:\n{plan.model_dump_json()}",
                f"受控目录:\n{context}",
            ]
        )
        return self._strip_sql_fence(await self._chat(system, user))

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
    "LLMResponseError",
    "OpenAIQueryLLM",
    "QueryLLM",
    "QueryPlan",
]
