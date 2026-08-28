from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentSession(BaseModel):
    session_id: str
    status: Literal["active", "waiting_user", "archived"]
    title: str | None = None
    summary: str | None = None
    summary_version: int = 0
    catalog_snapshot: str
    rule_versions: list[str] = Field(default_factory=list)
    active_run_id: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None


class AgentMessage(BaseModel):
    message_id: str
    session_id: str
    run_id: str | None = None
    sequence: int
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    content_type: Literal["text", "tool_call", "tool_result", "summary"]
    tool_name: str | None = None
    tool_call_id: str | None = None
    parent_message_id: str | None = None
    visibility: Literal["model", "user", "internal"] = "model"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AgentRun(BaseModel):
    run_id: str
    session_id: str
    request_id: str
    status: Literal[
        "queued", "running", "waiting_user", "completed", "paused", "cancelled", "failed"
    ]
    turn_count: int = 0
    sql_count: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    error_code: str | None = None
    checkpoint_sequence: int = 0
    started_at: datetime
    ended_at: datetime | None = None


class AgentEvent(BaseModel):
    run_id: str
    sequence: int
    event_id: str
    event_type: Literal[
        "session_start", "run_start", "turn_start", "assistant_delta", "assistant_end",
        "tool_call", "tool_result", "clarification_required", "run_paused", "run_end", "error"
    ]
    status: Literal["started", "completed", "failed", "waiting"]
    summary: str
    tool_name: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
