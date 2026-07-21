from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class QueryPlan(BaseModel):
    model_config = {"extra": "forbid"}

    query_type: Literal[
        "aggregation", "list", "ranking", "detail", "comparison", "time_series"
    ]
    table_hints: list[str] = Field(min_length=1, max_length=4)
    metrics: list[str] = Field(default_factory=list, max_length=20)
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    order_by: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=20, ge=1, le=100)
    requires_clarification: bool = False
    clarification_question: str | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class QueryData(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    schema_: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    data_as_of: str | None = None

    model_config = {"populate_by_name": True}


class SourceInfo(BaseModel):
    dataset: str
    version: str | None = None
    data_as_of: str | None = None


class ToolResponse(BaseModel):
    success: bool
    data: QueryData | None = None
    sources: list[SourceInfo] = Field(default_factory=list)
    request_id: str
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    diagnostics: dict[str, Any] | None = None

    @classmethod
    def failure(
        cls,
        *,
        request_id: str,
        code: str,
        message: str,
        retryable: bool,
        diagnostics: dict[str, Any] | None = None,
    ) -> "ToolResponse":
        return cls(
            success=False,
            request_id=request_id,
            error=ErrorInfo(code=code, message=message, retryable=retryable),
            diagnostics=diagnostics,
        )
