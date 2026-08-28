from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Awaitable

from app.agent.controller import AgentController
from app.agent.models import AgentAction, AgentRunResponse, AgentRunState, AgentToolResult
from app.agent.tools import AgentToolRegistry
from app.catalog import MetadataCatalog
from app.llm import LLMConfigurationError, LLMResponseError
from app.models import ToolResponse
from app.agent.middleware import DefaultToolMiddleware, ToolMiddleware
from app.agent.policy import BudgetPolicy


class AgentRuntime:
    """Bounded observe-decide-act loop with deterministic tool policy."""

    def __init__(
        self,
        catalog: MetadataCatalog,
        controller: AgentController,
        tools: AgentToolRegistry,
        *,
        max_turns: int = 10,
        max_sql_queries: int = 4,
        max_llm_calls: int = 12,
        max_total_tokens: int = 80000,
        max_wall_time_seconds: float = 240.0,
        max_consecutive_tool_errors: int = 3,
        middleware: ToolMiddleware | None = None,
    ) -> None:
        self.catalog = catalog
        self.controller = controller
        self.tools = tools
        self.max_turns = max_turns
        self.max_sql_queries = max_sql_queries
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.max_wall_time_seconds = max_wall_time_seconds
        self.max_consecutive_tool_errors = max_consecutive_tool_errors
        self.middleware = middleware or DefaultToolMiddleware(
            BudgetPolicy(max_turns=max_turns, max_sql_queries=max_sql_queries, max_llm_calls=max_llm_calls, max_total_tokens=max_total_tokens, max_wall_time_seconds=max_wall_time_seconds, max_consecutive_tool_errors=max_consecutive_tool_errors)
        )

    async def run(
        self,
        question: str,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        original_question: str | None = None,
        include_current_question: bool = True,
        cancel_check: Callable[[], bool] | None = None,
        existing_state: AgentRunState | None = None,
        state_callback: Callable[[AgentRunState], None | Awaitable[None]] | None = None,
    ) -> AgentRunResponse:
        request_id = request_id or f"qry_{uuid.uuid4().hex}"
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        normalized = (original_question or question).strip()
        current_message = question.strip()
        state = existing_state or AgentRunState(
            run_id=run_id,
            request_id=request_id,
            original_question=normalized,
            original_question_hash=self._question_hash(normalized),
            max_turns=self.max_turns,
            max_sql_queries=self.max_sql_queries,
            max_llm_calls=self.max_llm_calls,
            max_total_tokens=self.max_total_tokens,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_consecutive_tool_errors=self.max_consecutive_tool_errors,
            messages=[*(initial_messages or []), *([{"role": "user", "content": current_message}] if include_current_question else [])],
        )
        if existing_state is not None:
            state.run_id = run_id
            state.request_id = request_id
            state.status = "running"
            state.started_at_monotonic = time.monotonic()
            if include_current_question or current_message:
                state.messages.append({"role": "user", "content": current_message})
        else:
            state.started_at_monotonic = time.monotonic()
        seen_actions: set[str] = set(existing_state.seen_action_keys if existing_state else [])
        planning_context = self.catalog.build_planning_context()

        while state.status == "running" and state.turns_used < state.max_turns:
            if cancel_check is not None and cancel_check():
                state.status = "cancelled"
                state.error_code = "CANCELLED_BY_USER"
                break
            if self._question_hash(state.original_question) != state.original_question_hash:
                return self._fail(state, "ORIGINAL_QUESTION_CHANGED")
            self._emit_event(
                event_sink,
                state,
                stage=f"agent_turn_{state.turns_used + 1}_start",
                status="started",
                summary="Agent 正在决定下一步操作。",
                tool=None,
                duration_ms=0,
            )
            try:
                decision_started = time.monotonic()
                action = await self.controller.decide(state, planning_context)
            except LLMConfigurationError:
                return self._fail(state, "LLM_NOT_CONFIGURED")
            except LLMResponseError:
                return self._fail(state, "AGENT_CONTROLLER_FAILED")
            except Exception:
                return self._fail(state, "AGENT_CONTROLLER_FAILED")

            if self._question_hash(state.original_question) != state.original_question_hash:
                return self._fail(state, "ORIGINAL_QUESTION_CHANGED")

            state.turns_used += 1
            state.llm_calls_used += 1
            metadata = getattr(getattr(self.controller, "llm", None), "last_call_metadata", None)
            if isinstance(metadata, dict):
                state.total_tokens_used += int(metadata.get("total_tokens") or metadata.get("output_tokens") or 0)
            event_metadata = dict(metadata) if isinstance(metadata, dict) else {}
            event_metadata["action_tool"] = action.tool_name
            self._emit_event(
                event_sink,
                state,
                stage=f"agent_turn_{state.turns_used}_decision",
                status="completed",
                summary=f"Agent 第 {state.turns_used} 轮选择 {action.tool_name}",
                tool="llm",
                duration_ms=int((time.monotonic() - decision_started) * 1000),
                metadata=event_metadata,
            )
            action_key = self._action_key(action, state)
            state.messages.append(
                {
                    "role": "assistant",
                    "tool_call": {
                        "name": action.tool_name,
                        "arguments": action.arguments,
                    },
                }
            )
            policy_result = self.middleware.before(action, state, action_key, seen_actions)
            if policy_result is None:
                policy_result = self._check_policy(action, state, action_key, seen_actions)
            tool_started = time.monotonic()
            if policy_result is None:
                seen_actions.add(action_key)
                if action_key not in state.seen_action_keys:
                    state.seen_action_keys.append(action_key)
                if action.tool_name == "execute_readonly_query":
                    state.sql_queries_used += 1
                if action.tool_name == "ask_user_question":
                    state.user_questions_used += 1
                result = await self.tools.invoke(action.tool_name, action.arguments, state)
            else:
                result = policy_result

            result = self.middleware.after(action, result, state)
            self._apply_result(state, result)
            state.consecutive_tool_errors = state.consecutive_tool_errors + 1 if result.status in {"revision_required", "blocked", "error"} else 0
            if state.consecutive_tool_errors >= state.max_consecutive_tool_errors and result.retryable:
                state.status = "failed"
                state.error_code = "CONSECUTIVE_TOOL_ERROR_BUDGET_EXHAUSTED"
            if state_callback is not None:
                callback_result = state_callback(state)
                if hasattr(callback_result, "__await__"):
                    await callback_result
            self._emit_event(
                event_sink,
                state,
                stage=f"agent_turn_{state.turns_used}_{result.tool_name}",
                status=("completed" if result.status in {"ok", "no_match", "needs_user_input"} else "failed"),
                summary=(
                    f"{result.content} Agent 将根据观察继续修正。"
                    if result.status == "revision_required"
                    else result.content
                ),
                tool=result.tool_name,
                duration_ms=int((time.monotonic() - tool_started) * 1000),
                error_type=(
                    str(result.model_payload.get("error_code") or result.status)
                    if result.status in {"revision_required", "blocked", "error"}
                    else None
                ),
            )

            if result.tool_name == "ask_user_question" and result.status == "needs_user_input":
                state.status = "waiting_user"
                break
            if result.tool_name == "finalize_answer" and result.status == "ok":
                state.final_answer = str(result.model_payload.get("answer") or "")
                state.status = "completed"
                break
            if result.terminate and result.status in {"blocked", "error"}:
                state.status = "failed"
                state.error_code = str(
                    result.model_payload.get("error_code") or "AGENT_TOOL_FAILED"
                )
                break

        if state.status == "running":
            state.status = "failed"
            state.error_code = "AGENT_TURN_BUDGET_EXHAUSTED"
        if state_callback is not None:
            callback_result = state_callback(state)
            if hasattr(callback_result, "__await__"):
                await callback_result
        return self._response(state)

    def _check_policy(
        self,
        action: AgentAction,
        state: AgentRunState,
        action_key: str,
        seen_actions: set[str],
    ) -> AgentToolResult | None:
        if action_key in seen_actions:
            return AgentToolResult(
                tool_name=action.tool_name,
                status="blocked",
                content="相同工具和参数已经调用过，请根据已有观察采取不同动作。",
                model_payload={"error_code": "DUPLICATE_TOOL_CALL", "retryable": True},
                retryable=True,
            )
        if (
            action.tool_name == "execute_readonly_query"
            and state.sql_queries_used >= state.max_sql_queries
        ):
            return AgentToolResult(
                tool_name=action.tool_name,
                status="blocked",
                content="只读查询次数预算已耗尽，不能继续执行 SQL。",
                model_payload={"error_code": "SQL_QUERY_BUDGET_EXHAUSTED", "retryable": False},
                terminate=True,
            )
        if action.tool_name == "ask_user_question" and state.user_questions_used >= 1:
            return AgentToolResult(
                tool_name=action.tool_name,
                status="blocked",
                content="本次运行已经请求过一次用户澄清。",
                model_payload={"error_code": "USER_QUESTION_BUDGET_EXHAUSTED", "retryable": False},
                terminate=True,
            )
        return None

    @staticmethod
    def _apply_result(state: AgentRunState, result: AgentToolResult) -> None:
        state.observations.append(result)
        state.messages.append(
            {
                "role": "tool",
                "name": result.tool_name,
                "status": result.status,
                "content": result.content,
                "payload": result.model_payload,
            }
        )
        if result.tool_name == "get_table_context" and result.status == "ok":
            context_id = str(result.model_payload.get("context_id") or "")
            if context_id and context_id not in state.loaded_context_ids:
                state.loaded_context_ids.append(context_id)
        for evidence_id in result.evidence_ids:
            if evidence_id not in state.evidence_ids:
                state.evidence_ids.append(evidence_id)
        if result.tool_name == "review_evidence" and result.status == "ok":
            for evidence_id in result.model_payload.get("approved_evidence_ids", []):
                if evidence_id not in state.approved_evidence_ids:
                    state.approved_evidence_ids.append(evidence_id)

    def _response(self, state: AgentRunState) -> AgentRunResponse:
        clarification = next(
            (
                item.content
                for item in reversed(state.observations)
                if item.status == "needs_user_input"
            ),
            None,
        )
        tool_response = self.tools.final_responses.get(state.run_id)
        if state.status == "waiting_user" and tool_response is None:
            tool_response = ToolResponse.failure(
                request_id=state.request_id,
                code="CLARIFICATION_REQUIRED",
                message=clarification or "请补充查询口径。",
                retryable=False,
            )
        if state.status == "failed" and tool_response is None:
            tool_response = ToolResponse.failure(
                request_id=state.request_id,
                code=state.error_code or "AGENT_FAILED",
                message=self._failure_message(state.error_code),
                retryable=False,
            )
        return AgentRunResponse(
            run_id=state.run_id,
            request_id=state.request_id,
            status=state.status,
            answer=state.final_answer,
            clarification_question=clarification,
            response=tool_response,
            error_code=state.error_code,
            turns_used=state.turns_used,
            llm_calls_used=state.llm_calls_used,
            sql_queries_used=state.sql_queries_used,
            tool_trace=[
                {
                    "tool": item.tool_name,
                    "status": item.status,
                    "summary": item.content,
                    "evidence_ids": list(item.evidence_ids),
                    "error_code": item.model_payload.get("error_code"),
                }
                for item in state.observations
            ],
        )

    @staticmethod
    def _emit_event(
        event_sink: Callable[[dict[str, Any]], None] | None,
        state: AgentRunState,
        *,
        stage: str,
        status: str,
        summary: str,
        tool: str | None,
        duration_ms: int,
        error_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if event_sink is not None:
            event_sink(
                {
                    "request_id": state.request_id,
                    "run_id": state.run_id,
                    "turn": state.turns_used,
                    "stage": stage,
                    "status": status,
                    "summary": summary,
                    "tool": tool,
                    "duration_ms": duration_ms,
                    "error_type": error_type,
                    **(metadata if isinstance(metadata, dict) else {}),
                }
            )

    def _fail(self, state: AgentRunState, code: str) -> AgentRunResponse:
        state.status = "failed"
        state.error_code = code
        return self._response(state)

    @staticmethod
    def _question_hash(question: str) -> str:
        return hashlib.sha256(question.encode("utf-8")).hexdigest()

    @staticmethod
    def _action_key(action: AgentAction, state: AgentRunState) -> str:
        key_payload: dict[str, Any] = {
            "tool_name": action.tool_name,
            "arguments": action.arguments,
        }
        # A finalize attempt before review and one after review are different
        # policy states. SQL calls intentionally do not receive this exception.
        if action.tool_name == "finalize_answer":
            key_payload["approved_evidence_ids"] = sorted(state.approved_evidence_ids)
        payload = json.dumps(
            key_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _failure_message(code: str | None) -> str:
        messages = {
            "LLM_NOT_CONFIGURED": "Agent 控制器尚未配置模型服务。",
            "AGENT_CONTROLLER_FAILED": "Agent 控制器未能返回合法决策。",
            "AGENT_TURN_BUDGET_EXHAUSTED": "Agent 在限定轮次内未形成经过验证的答案。",
            "ORIGINAL_QUESTION_CHANGED": "运行过程中原始问题校验失败。",
        }
        return messages.get(code or "", "Agent 查询未能完成。")
