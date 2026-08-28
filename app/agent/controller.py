from __future__ import annotations

import json
from typing import Protocol

from app.agent.models import AGENT_TOOL_NAMES, AgentAction, AgentRunState
from app.llm import LLMInvalidResponseError, OpenAIQueryLLM


class AgentController(Protocol):
    async def decide(self, state: AgentRunState, planning_context: str) -> AgentAction: ...


class OpenAIAgentController:
    """Strict JSON action controller over the existing OpenAI-compatible client."""

    SYSTEM_PROMPT = """你是受控数据查询 Agent 的决策核心。你只能输出一个 JSON 对象，格式为：
{"tool_name":"工具名","arguments":{},"reasoning_summary":"不含内部字段和 SQL 的简短决策说明"}

可用工具只有：get_table_context、inspect_field_profile、execute_readonly_query、review_evidence、ask_user_question、finalize_answer。

行为约束：
1. 只能通过工具获取数据库事实，不得直接回答任何数据结论。
2. 先获取相关表上下文，再生成并执行 SQL。
3. SQL 被拒绝、执行失败或结果为空时，必须阅读真实工具观察，自主修改 SQL、检查字段画像、换表或澄清；空结果不等于数据库没有数据。
4. 不得重复调用相同工具和相同参数。
5. 结果充分后先调用 review_evidence；只有审核通过后才能调用 finalize_answer。
6. 只有目录和字段画像仍无法确定业务口径时才能调用 ask_user_question。
7. 不得修改用户原始问题，不得输出 SQL、DDL、内部表字段、数据库路径、异常堆栈或思维过程。
8. arguments 必须严格匹配工具要求，不得在自然语言中伪造工具调用。"""

    def __init__(self, llm: OpenAIQueryLLM) -> None:
        self.llm = llm

    async def decide(self, state: AgentRunState, planning_context: str) -> AgentAction:
        payload = {
            "original_question": state.original_question,
            "budgets": {
                "turns_remaining": state.max_turns - state.turns_used,
                "sql_queries_remaining": state.max_sql_queries - state.sql_queries_used,
            },
            "loaded_context_ids": state.loaded_context_ids,
            "evidence_ids": state.evidence_ids,
            "approved_evidence_ids": state.approved_evidence_ids,
            "conversation": state.messages[-16:],
            "lightweight_catalog": planning_context,
            "tool_schemas": self._tool_schemas(),
        }

        def validate(content: str) -> AgentAction:
            try:
                return AgentAction.model_validate_json(
                    OpenAIQueryLLM._strip_json_fence(content)
                )
            except Exception as exc:
                raise LLMInvalidResponseError("Agent 控制器未返回合法工具动作") from exc

        return await self.llm._chat(
            "planning",
            self.SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            validator=validate,
        )

    @staticmethod
    def _tool_schemas() -> dict[str, dict[str, object]]:
        return {
            "get_table_context": {"table_hints": ["已发布表名"]},
            "inspect_field_profile": {
                "context_id": "ctx_xxx",
                "field": "已发布字段名",
                "table": "可选，已发布表名",
            },
            "execute_readonly_query": {"context_id": "ctx_xxx", "sql": "SELECT ..."},
            "review_evidence": {"evidence_ids": ["ev_xxx"]},
            "ask_user_question": {"question": "面向业务用户的单一澄清问题"},
            "finalize_answer": {"evidence_ids": ["ev_xxx"]},
            "allowed_tools": list(AGENT_TOOL_NAMES),
        }
