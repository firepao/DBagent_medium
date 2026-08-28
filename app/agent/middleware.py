from __future__ import annotations

from typing import Protocol

from app.agent.models import AgentAction, AgentRunState, AgentToolResult
from app.agent.policy import BudgetPolicy


class ToolMiddleware(Protocol):
    def before(self, action: AgentAction, state: AgentRunState, action_key: str, seen_actions: set[str]) -> AgentToolResult | None: ...
    def after(self, action: AgentAction, result: AgentToolResult, state: AgentRunState) -> AgentToolResult: ...


class DefaultToolMiddleware:
    def __init__(self, policy: BudgetPolicy | None = None):
        self.policy = policy or BudgetPolicy()

    def before(self, action, state, action_key, seen_actions):
        return self.policy.check(action, state, seen_actions)

    def after(self, action, result, state):
        return result
