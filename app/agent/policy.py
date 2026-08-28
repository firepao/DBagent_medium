from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.agent.models import AgentAction, AgentRunState, AgentToolResult


class BudgetPolicy:
    """Deterministic limits for one agent run; model remains responsible for choices."""

    def __init__(self, *, max_turns=10, max_sql_queries=4, max_llm_calls=12,
                 max_total_tokens=80000, max_wall_time_seconds=240, max_user_questions=1,
                 max_consecutive_tool_errors=3):
        self.max_turns = max_turns
        self.max_sql_queries = max_sql_queries
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.max_wall_time_seconds = max_wall_time_seconds
        self.max_user_questions = max_user_questions
        self.max_consecutive_tool_errors = max_consecutive_tool_errors
        self.started_at = time.monotonic()

    @staticmethod
    def action_key(action: AgentAction, state: AgentRunState) -> str:
        payload = {"tool_name": action.tool_name, "arguments": action.arguments}
        if action.tool_name == "finalize_answer":
            payload["approved_evidence_ids"] = sorted(state.approved_evidence_ids)
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def check(self, action: AgentAction, state: AgentRunState, seen_actions: set[str]) -> AgentToolResult | None:
        if state.turns_used > self.max_turns:
            return self._blocked(action, "TURN_BUDGET_EXHAUSTED", "Agent 轮次预算已耗尽。", terminate=True)
        if state.llm_calls_used > self.max_llm_calls:
            return self._blocked(action, "LLM_CALL_BUDGET_EXHAUSTED", "模型调用预算已耗尽。", terminate=True)
        if state.total_tokens_used >= self.max_total_tokens:
            return self._blocked(action, "TOKEN_BUDGET_EXHAUSTED", "模型 Token 预算已耗尽。", terminate=True)
        if state.sql_queries_used >= self.max_sql_queries and action.tool_name == "execute_readonly_query":
            return self._blocked(action, "SQL_QUERY_BUDGET_EXHAUSTED", "只读查询次数预算已耗尽。", terminate=True)
        if state.user_questions_used >= self.max_user_questions and action.tool_name == "ask_user_question":
            return self._blocked(action, "USER_QUESTION_BUDGET_EXHAUSTED", "本次运行的澄清次数预算已耗尽。", terminate=True)
        started_at = state.started_at_monotonic or self.started_at
        if time.monotonic() - started_at > self.max_wall_time_seconds:
            return self._blocked(action, "WALL_TIME_BUDGET_EXHAUSTED", "Agent 运行时间预算已耗尽。", terminate=True)
        if self.action_key(action, state) in seen_actions:
            return self._blocked(action, "DUPLICATE_TOOL_CALL", "相同工具和参数已经调用过，请根据观察采取不同动作。")
        return None

    @staticmethod
    def _blocked(action, code, content, *, terminate=False):
        return AgentToolResult(tool_name=action.tool_name, status="blocked", content=content, model_payload={"error_code": code, "retryable": not terminate}, retryable=not terminate, terminate=terminate)
