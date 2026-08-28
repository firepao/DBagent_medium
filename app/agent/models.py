from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import ToolResponse


AGENT_TOOL_NAMES = (
    "get_table_context",
    "inspect_field_profile",
    "execute_readonly_query",
    "review_evidence",
    "ask_user_question",
    "finalize_answer",
)


class AgentAction(BaseModel):
    """One controller decision. Natural-language pseudo tool calls are rejected."""

    model_config = {"extra": "forbid"}

    tool_name: Literal[
        "get_table_context",
        "inspect_field_profile",
        "execute_readonly_query",
        "review_evidence",
        "ask_user_question",
        "finalize_answer",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)
    reasoning_summary: str = Field(default="", max_length=240)


class AgentToolResult(BaseModel):
    model_config = {"extra": "forbid"}

    tool_name: str
    status: Literal[
        "ok",
        "no_match",
        "needs_user_input",
        "revision_required",
        "blocked",
        "error",
    ]
    content: str
    model_payload: dict[str, Any] = Field(default_factory=dict)
    details_ref: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    retryable: bool = False
    terminate: bool = False


class AgentRunState(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: str
    request_id: str
    original_question: str
    original_question_hash: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[AgentToolResult] = Field(default_factory=list)
    loaded_context_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    approved_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "running", "waiting_user", "completed", "failed", "cancelled"
    ] = "running"
    turns_used: int = 0
    llm_calls_used: int = 0
    sql_queries_used: int = 0
    user_questions_used: int = 0
    max_turns: int = Field(default=10, ge=1, le=30)
    max_sql_queries: int = Field(default=4, ge=1, le=10)
    max_llm_calls: int = Field(default=12, ge=1, le=50)
    max_total_tokens: int = Field(default=80000, ge=1, le=500000)
    max_wall_time_seconds: float = Field(default=240.0, gt=0, le=3600)
    max_consecutive_tool_errors: int = Field(default=3, ge=1, le=10)
    consecutive_tool_errors: int = 0
    total_tokens_used: int = 0
    # Runtime-only monotonic clock. It is reset for a resumed execution slice
    # and never exposed in API/checkpoint payloads.
    started_at_monotonic: float | None = Field(default=None, exclude=True)
    seen_action_keys: list[str] = Field(default_factory=list)
    final_answer: str | None = None
    error_code: str | None = None


class AgentRunResponse(BaseModel):
    run_id: str
    request_id: str
    status: Literal["waiting_user", "completed", "failed", "cancelled"]
    answer: str | None = None
    clarification_question: str | None = None
    response: ToolResponse | None = None
    error_code: str | None = None
    turns_used: int
    llm_calls_used: int
    sql_queries_used: int
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("tool_trace")
    @classmethod
    def public_trace_is_bounded(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return value[:30]
