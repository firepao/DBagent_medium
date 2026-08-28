from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    """Public, sanitized progress event for one query run."""

    model_config = {"extra": "forbid"}

    request_id: str
    sequence: int = Field(ge=1)
    stage: str
    status: Literal["started", "completed", "failed"]
    timestamp: str
    duration_ms: int | None = Field(default=None, ge=0)
    summary: str
    error_type: str | None = None
    model: str | None = None
    provider: str | None = None
    tool: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    rule_versions: list[str] = Field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        sequence: int,
        stage: str,
        status: Literal["started", "completed", "failed"],
        summary: str,
        duration_ms: int | None = None,
        error_type: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        tool: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        rule_versions: list[str] | None = None,
    ) -> "RunEvent":
        return cls(
            request_id=request_id,
            sequence=sequence,
            stage=stage,
            status=status,
            timestamp=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            summary=summary,
            error_type=error_type,
            model=model,
            provider=provider,
            tool=tool,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            rule_versions=list(rule_versions or []),
        )


STAGE_LABELS: dict[str, str] = {
    "routing": "准备已发布数据目录",
    "planning": "理解问题并制定查询计划",
    "planning_categorical_retry": "根据已发布分类口径修正计划",
    "sql_generation_initial": "生成候选数据查询",
    "sql_guard_initial": "执行确定性安全检查",
    "sql_guard_repair_1": "按安全检查意见修正查询",
    "sql_guard_repair_validation_1": "重新执行安全检查",
    "pre_execution_review_1": "审核查询业务口径",
    "sql_semantic_revision_1": "按业务审核意见修正查询",
    "sql_guard_revision_1": "检查修正后的查询",
    "pre_execution_review_2": "复核修正后的业务口径",
    "execution_primary": "执行只读数据查询",
    "result_review_1": "检查结果是否足以回答",
    "sql_result_requery_1": "生成一次受控补查",
    "sql_guard_requery_1": "检查补查安全性",
    "execution_supplemental": "执行受控补查",
    "result_review_2": "复核补查结果",
}


def stage_summary(stage: str, status: str) -> str:
    label = STAGE_LABELS.get(stage, "处理查询")
    suffix = {"started": "中", "completed": "完成", "failed": "失败"}[status]
    return f"{label}{suffix}"


class RunEventStore:
    """脱敏 RunEvent 持久化，用于按 request ID 重放关键轨迹。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: RunEvent) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO run_events (request_id, sequence, payload_json) VALUES (?, ?, ?)",
                (event.request_id, event.sequence, event.model_dump_json()),
            )
            connection.commit()

    def list(self, request_id: str) -> list[RunEvent]:
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM run_events WHERE request_id = ? ORDER BY sequence",
                (request_id,),
            ).fetchall()
        return [RunEvent.model_validate_json(row[0]) for row in rows]
