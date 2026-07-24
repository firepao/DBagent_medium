"""Deterministic, bounded evidence construction for result review."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from app.executor import ExecutionResult
from app.models import ResultColumn, ResultEvidence, SourceInfo


SENSITIVE_NAME = re.compile(
    r"(?:phone|mobile|contact|联系人|电话|经度|纬度|坐标|longitude|latitude)",
    re.IGNORECASE,
)


class ResultEvidenceBuilder:
    """Build review evidence without exposing raw large or sensitive result sets."""

    def __init__(self, max_preview_rows: int = 10, max_text_length: int = 160) -> None:
        self.max_preview_rows = max_preview_rows
        self.max_text_length = max_text_length

    def build(
        self,
        result: ExecutionResult,
        *,
        sources: Iterable[SourceInfo],
        applied_scope: str,
        field_labels: dict[str, str] | None = None,
        limitations: list[str] | None = None,
    ) -> ResultEvidence:
        labels = field_labels or {}
        visible_columns = [
            item for item in result.schema if not SENSITIVE_NAME.search(item["name"])
        ]
        visible_names = {item["name"] for item in visible_columns}
        preview = [
            {
                name: self._safe_value(value)
                for name, value in row.items()
                if name in visible_names
            }
            for row in result.rows[: self.max_preview_rows]
        ]
        profile = self._profile(result.rows, visible_names)
        source_list = list(sources)
        data_as_of = [source.data_as_of for source in source_list if source.data_as_of]
        evidence_limitations = list(limitations or [])
        if result.truncated:
            evidence_limitations.append("查询结果已按返回行数上限截断。")
        if len(visible_columns) != len(result.schema):
            evidence_limitations.append("敏感字段未提供给结果审核。")
        return ResultEvidence(
            result_status="data_found" if result.rows else "no_match",
            row_count=result.row_count,
            truncated=result.truncated,
            columns=[
                ResultColumn(
                    name=item["name"],
                    semantic_label=labels.get(item["name"], item["name"]),
                    type=item["type"],
                )
                for item in visible_columns
            ],
            rows_preview=preview,
            aggregate_profile=profile,
            applied_scope=applied_scope,
            source_datasets=[source.dataset for source in source_list],
            data_as_of=max(data_as_of) if data_as_of else None,
            data_quality={"sensitive_columns_excluded": len(visible_columns) != len(result.schema)},
            limitations=evidence_limitations,
        )

    def merge(self, primary: ResultEvidence, supplemental: ResultEvidence) -> dict[str, Any]:
        """Preserve separate result-set meaning; never add or coerce result values."""
        return {
            "result_sets": [
                {"id": "primary", "purpose": "主查询", "evidence": primary.model_dump()},
                {"id": "supplemental", "purpose": "补查", "evidence": supplemental.model_dump()},
            ],
            "relationship": "same_scope_complementary",
            "applied_scope": primary.applied_scope,
        }

    def _profile(self, rows: list[dict[str, Any]], visible_names: set[str]) -> dict[str, Any]:
        null_counts = {name: 0 for name in visible_names}
        values: dict[str, list[Any]] = {name: [] for name in visible_names}
        for row in rows:
            for name in visible_names:
                value = row.get(name)
                if value is None:
                    null_counts[name] += 1
                else:
                    values[name].append(value)
        numeric_min_max: dict[str, dict[str, float]] = {}
        distinct_counts: dict[str, int] = {}
        for name, column_values in values.items():
            distinct_counts[name] = len({self._distinct_key(value) for value in column_values})
            numeric = [
                float(value)
                for value in column_values
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ]
            if numeric:
                numeric_min_max[name] = {"min": min(numeric), "max": max(numeric)}
        return {
            "null_counts": null_counts,
            "distinct_counts": distinct_counts,
            "numeric_min_max": numeric_min_max,
        }

    @staticmethod
    def _distinct_key(value: Any) -> str:
        return repr(value)

    def _safe_value(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self.max_text_length:
            return value[: self.max_text_length] + "..."
        if isinstance(value, bytes):
            return "<binary>"
        return value
