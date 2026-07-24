from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class QueryPlan(BaseModel):
    """Business intent contract created before physical schema resolution."""

    model_config = {"extra": "forbid"}

    original_question: str = Field(default="", max_length=2000)
    query_type: Literal[
        "aggregation", "list", "ranking", "detail", "comparison", "time_series"
    ]
    table_hints: list[str] = Field(min_length=1, max_length=4)
    required_outputs: list[str] = Field(default_factory=list, max_length=20)
    business_objects: list[str] = Field(default_factory=list, max_length=20)
    time_requirements: list[str] = Field(default_factory=list, max_length=10)
    presentation_requirements: list[str] = Field(default_factory=list, max_length=10)
    requires_clarification: bool = False
    clarification_question: str | None = None


class ReviewIssue(BaseModel):
    """审核器可输出的、面向业务的有限问题项。"""

    model_config = {"extra": "forbid"}

    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=240)


class PreExecutionReview(BaseModel):
    """执行前审核只提出意见，绝不携带或修改 SQL。"""

    model_config = {"extra": "forbid"}

    decision: Literal["pass", "revise", "clarification", "unsupported"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    required_changes: list[str] = Field(default_factory=list, max_length=10)
    clarification: str | None = Field(default=None, max_length=240)

    @field_validator("required_changes")
    @classmethod
    def revise_requires_changes(cls, value: list[str], info: Any) -> list[str]:
        if info.data.get("decision") == "revise" and not value:
            raise ValueError("decision=revise 时必须提供 required_changes")
        return value


class ResultColumn(BaseModel):
    name: str
    semantic_label: str
    type: str


class ResultEvidence(BaseModel):
    """执行结果的脱敏、确定性摘要，仅供结果审核。"""

    model_config = {"extra": "forbid"}

    result_status: Literal["data_found", "no_match"]
    row_count: int = Field(ge=0)
    truncated: bool
    columns: list[ResultColumn]
    rows_preview: list[dict[str, Any]] = Field(default_factory=list)
    aggregate_profile: dict[str, Any] = Field(default_factory=dict)
    applied_scope: str
    source_datasets: list[str] = Field(default_factory=list)
    data_as_of: str | None = None
    data_quality: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class ResultReview(BaseModel):
    """执行后审核只判断充分性，不生成 SQL 或数值。"""

    model_config = {"extra": "forbid"}

    decision: Literal["answer", "requery", "clarification", "unsupported"]
    result_issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    required_changes: list[str] = Field(default_factory=list, max_length=10)
    answer_limitations: list[str] = Field(default_factory=list, max_length=10)
    clarification: str | None = Field(default=None, max_length=240)

    @field_validator("required_changes")
    @classmethod
    def requery_requires_changes(cls, value: list[str], info: Any) -> list[str]:
        if info.data.get("decision") == "requery" and not value:
            raise ValueError("decision=requery 时必须提供 required_changes")
        return value


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class QueryData(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    schema_: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    data_as_of: str | None = None
    result_status: Literal["data_found", "no_match"] = "data_found"
    result_reason: str | None = None

    model_config = {"populate_by_name": True}


class SourceInfo(BaseModel):
    dataset: str
    version: str | None = None
    data_as_of: str | None = None


class ResultSet(BaseModel):
    id: Literal["primary", "supplemental"]
    purpose: str
    data: QueryData


class Coverage(BaseModel):
    applied_scope: str
    dimensions: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)


class ToolResponse(BaseModel):
    success: bool
    data: QueryData | None = None
    sources: list[SourceInfo] = Field(default_factory=list)
    request_id: str
    warnings: list[str] = Field(default_factory=list)
    result_sets: list[ResultSet] = Field(default_factory=list)
    coverage: Coverage | None = None
    limitations: list[str] = Field(default_factory=list)
    answer_guidance: dict[str, Any] | None = None
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
        clarification = code == "CLARIFICATION_REQUIRED"
        return cls(
            success=False,
            request_id=request_id,
            answer_guidance={
                "response_mode": "clarification" if clarification else "error",
                "hard_constraints": [
                    "只能转述 error.message 中已经确认的原因",
                    "不得推测字段不存在、字段名不匹配、数据未接入或用户需要补数据库字段",
                    "不得补造内部查询语句、表名、字段名、模型意见或其他可能原因",
                    "回答末尾保留 request_id，便于服务端排查",
                ],
                "template": (
                    "需要补充查询口径：{error.message} 请求编号：{request_id}"
                    if clarification
                    else "{error.message} 请求编号：{request_id}"
                ),
            },
            error=ErrorInfo(code=code, message=message, retryable=retryable),
            diagnostics=diagnostics,
        )
