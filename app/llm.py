import json
from typing import Any, Mapping, Protocol

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.llm_trace import LLMTraceRepository
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


class OpenAIQueryLLM:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        prompts: PromptRegistry | Mapping[str, str] | None = None,
        trace_repository: LLMTraceRepository | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self.trace_repository = trace_repository
        if prompts is None:
            self.prompts = PromptRegistry.from_file(settings.prompts_path)
        elif isinstance(prompts, PromptRegistry):
            self.prompts = prompts
        else:
            self.prompts = PromptRegistry("test", prompts)

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
        except httpx.TimeoutException as exc:
            self._record_error("timeout")
            raise LLMTimeoutError("模型接口调用超时") from exc
        except httpx.HTTPError as exc:
            self._record_error("upstream_http_error")
            raise LLMUpstreamUnavailableError("模型接口不可用") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._record_error("invalid_response")
            raise LLMInvalidResponseError("模型接口返回无效响应") from exc
        if not isinstance(content, str) or not content.strip():
            self._record_error("empty_content")
            raise LLMInvalidResponseError("模型接口返回空内容")
        normalized = content.strip()
        if self.trace_repository is not None:
            self.trace_repository.record_output(self.settings.openai_model, normalized)
        return normalized

    def _record_error(self, error_type: str) -> None:
        if self.trace_repository is not None:
            self.trace_repository.record_error(self.settings.openai_model, error_type)

    async def plan(self, question: str, planning_context: str) -> QueryPlan:
        system = self.prompts.get("planner")
        user = json.dumps(
            {"question": question, "planning_context": planning_context},
            ensure_ascii=False,
        )
        content = self._strip_json_fence(await self._chat(system, user))
        try:
            return QueryPlan.model_validate_json(content)
        except ValidationError as exc:
            raise LLMPlanSchemaError("查询规划结果不符合约定结构") from exc

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
        return self._strip_sql_fence(await self._chat(system, user))

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
        content = self._strip_json_fence(await self._chat(system, user))
        try:
            review = PreExecutionReview.model_validate_json(content)
        except ValidationError as exc:
            raise LLMReviewSchemaError("执行前审核结果不符合约定结构") from exc
        if review.decision == "clarification" and not review.clarification:
            raise LLMReviewSchemaError("执行前审核未提供澄清问题")
        return review

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
        content = self._strip_json_fence(await self._chat(system, user))
        try:
            review = ResultReview.model_validate_json(content)
        except ValidationError as exc:
            raise LLMReviewSchemaError("执行后结果审核不符合约定结构") from exc
        if review.decision == "clarification" and not review.clarification:
            raise LLMReviewSchemaError("执行后审核未提供澄清问题")
        return review

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
