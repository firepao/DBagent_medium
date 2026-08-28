from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import QueryRequest, ToolResponse


class EvaluationCase(BaseModel):
    id: str
    question: str
    expected_behavior: Literal["success", "clarification", "unsupported"]
    expected_tables: list[str] = Field(default_factory=list)
    expected_values: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class GoldenValuesUpdate(BaseModel):
    expected_values: dict[str, str | int | float | bool | None] = Field(max_length=20)

    @field_validator("expected_values")
    @classmethod
    def validate_values(cls, values):
        for key, value in values.items():
            if not key or len(key) > 100:
                raise ValueError("黄金值字段名长度必须为 1-100")
            if isinstance(value, str) and len(value) > 500:
                raise ValueError("黄金值文本不能超过 500 字符")
        return values


class BulkGoldenValuesUpdate(BaseModel):
    cases: dict[str, GoldenValuesUpdate] = Field(min_length=1, max_length=100)


class EvaluationCaseResult(BaseModel):
    case_id: str
    passed: bool
    behavior_passed: bool
    tables_passed: bool
    values_passed: bool
    actual_behavior: str
    actual_tables: list[str] = Field(default_factory=list)
    error_code: str | None = None
    duration_ms: int
    request_id: str
    issues: list[str] = Field(default_factory=list)
    model_calls: int = 0
    total_tokens: int | None = None


class EvaluationRun(BaseModel):
    id: str
    target_type: Literal["baseline", "rule"]
    target_id: str
    status: Literal["running", "completed"]
    total: int
    passed: int
    pass_rate: float
    value_cases_total: int = 0
    value_cases_passed: int = 0
    value_accuracy: float | None = None
    model_calls: int = 0
    total_tokens: int | None = None
    p50_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    started_at: str
    completed_at: str | None = None
    results: list[EvaluationCaseResult] = Field(default_factory=list)


class EvaluationRunRequest(BaseModel):
    target_type: Literal["baseline", "rule"] = "baseline"
    target_id: str = "baseline"
    rule_id: str | None = None
    case_ids: list[str] | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("case_ids")
    @classmethod
    def validate_case_ids(cls, values):
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("评测题号不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("评测题号不能重复")
        return normalized


class EvaluationReadiness(BaseModel):
    total_cases: int
    golden_cases: int
    golden_coverage: float
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_pass_rate: float | None = None
    latest_run_complete: bool = False
    ready_for_release: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class EvaluationCaseChange(BaseModel):
    case_id: str
    change: Literal["fixed", "regressed", "still_passed", "still_failed", "added", "removed"]
    baseline_passed: bool | None = None
    candidate_passed: bool | None = None


class EvaluationComparison(BaseModel):
    baseline_run_id: str
    candidate_run_id: str
    baseline_target_id: str
    candidate_target_id: str
    pass_rate_delta: float
    average_duration_delta_ms: float
    p50_duration_delta_ms: float
    p95_duration_delta_ms: float
    model_calls_delta: int
    total_tokens_delta: int | None = None
    fixed: int
    regressed: int
    still_passed: int
    still_failed: int
    changes: list[EvaluationCaseChange]


class EvaluationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    status TEXT NOT NULL, total INTEGER NOT NULL, passed INTEGER NOT NULL,
                    pass_rate REAL NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
                    results_json TEXT NOT NULL, value_cases_total INTEGER NOT NULL DEFAULT 0,
                    value_cases_passed INTEGER NOT NULL DEFAULT 0, value_accuracy REAL,
                    model_calls INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS evaluation_case_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                    actor TEXT NOT NULL, changed_at TEXT NOT NULL,
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
            for name in ("p50_duration_ms", "p95_duration_ms"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE evaluation_runs ADD COLUMN {name} REAL NOT NULL DEFAULT 0")

    def import_validation_cases(self, path: str | Path) -> int:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cases = []
        behavior = {
            "supported": "success",
            "needs_customer_confirmation": "clarification",
            "not_supported": "unsupported",
            "out_of_scope": "unsupported",
        }
        for item in raw.get("cases", []):
            if item.get("status") not in behavior:
                continue
            cases.append(
                EvaluationCase(
                    id=item["id"], question=item["question"],
                    expected_behavior=behavior[item["status"]],
                    expected_tables=item.get("scope_tables", []),
                    tags=[item["status"]],
                )
            )
        with self._connect() as connection:
            for case in cases:
                existing = connection.execute(
                    "SELECT payload_json FROM evaluation_cases WHERE id = ?", (case.id,)
                ).fetchone()
                if existing is not None:
                    prior = EvaluationCase.model_validate_json(existing[0])
                    case = case.model_copy(update={"expected_values": prior.expected_values})
                connection.execute(
                    "INSERT OR REPLACE INTO evaluation_cases VALUES (?, ?)",
                    (case.id, case.model_dump_json()),
                )
        return len(cases)

    def list_cases(self) -> list[EvaluationCase]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM evaluation_cases ORDER BY id").fetchall()
        return [EvaluationCase.model_validate_json(row[0]) for row in rows]

    def select_cases(self, case_ids: list[str] | None = None) -> list[EvaluationCase]:
        cases = self.list_cases()
        if case_ids is None:
            return cases
        indexed = {case.id: case for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in indexed]
        if missing:
            raise KeyError(", ".join(missing))
        return [indexed[case_id] for case_id in case_ids]

    def update_golden_values(
        self, case_id: str, update: GoldenValuesUpdate, actor: str = "local-admin"
    ) -> EvaluationCase:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM evaluation_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if row is None:
                raise KeyError(case_id)
            case = EvaluationCase.model_validate_json(row[0])
            before = case.expected_values
            updated = case.model_copy(update={"expected_values": update.expected_values})
            connection.execute(
                "UPDATE evaluation_cases SET payload_json = ? WHERE id = ?",
                (updated.model_dump_json(), case_id),
            )
            connection.execute(
                """INSERT INTO evaluation_case_audit
                (case_id, actor, changed_at, before_json, after_json)
                VALUES (?, ?, ?, ?, ?)""",
                (case_id, actor, datetime.now(UTC).isoformat(),
                 json.dumps(before, ensure_ascii=False),
                 json.dumps(update.expected_values, ensure_ascii=False)),
            )
        return updated

    def update_golden_values_bulk(
        self, update: BulkGoldenValuesUpdate, actor: str = "local-admin"
    ) -> list[EvaluationCase]:
        updated_cases: list[EvaluationCase] = []
        with self._connect() as connection:
            rows = {
                row["id"]: row["payload_json"]
                for row in connection.execute(
                    f"SELECT id, payload_json FROM evaluation_cases WHERE id IN ({','.join('?' for _ in update.cases)})",
                    tuple(update.cases),
                ).fetchall()
            }
            missing = sorted(set(update.cases) - set(rows))
            if missing:
                raise KeyError(", ".join(missing))
            changed_at = datetime.now(UTC).isoformat()
            for case_id, values_update in update.cases.items():
                case = EvaluationCase.model_validate_json(rows[case_id])
                updated = case.model_copy(update={"expected_values": values_update.expected_values})
                connection.execute(
                    "UPDATE evaluation_cases SET payload_json = ? WHERE id = ?",
                    (updated.model_dump_json(), case_id),
                )
                connection.execute(
                    """INSERT INTO evaluation_case_audit
                    (case_id, actor, changed_at, before_json, after_json)
                    VALUES (?, ?, ?, ?, ?)""",
                    (case_id, actor, changed_at,
                     json.dumps(case.expected_values, ensure_ascii=False),
                     json.dumps(values_update.expected_values, ensure_ascii=False)),
                )
                updated_cases.append(updated)
        return updated_cases

    def save_run(self, run: EvaluationRun) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO evaluation_runs (
                    id, target_type, target_id, status, total, passed, pass_rate,
                    started_at, completed_at, results_json, value_cases_total,
                    value_cases_passed, value_accuracy, model_calls, total_tokens
                    , p50_duration_ms, p95_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id, run.target_type, run.target_id, run.status, run.total,
                    run.passed, run.pass_rate, run.started_at, run.completed_at,
                    json.dumps([item.model_dump() for item in run.results], ensure_ascii=False),
                    run.value_cases_total, run.value_cases_passed, run.value_accuracy,
                    run.model_calls, run.total_tokens,
                    run.p50_duration_ms, run.p95_duration_ms,
                ),
            )

    def list_runs(self) -> list[EvaluationRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evaluation_runs ORDER BY started_at DESC"
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def readiness(self) -> EvaluationReadiness:
        cases = self.list_cases()
        golden_cases = sum(bool(case.expected_values) for case in cases)
        runs = self.list_runs()
        latest = runs[0] if runs else None
        reasons: list[str] = []
        if not cases:
            reasons.append("评测题集为空")
        if golden_cases < len(cases):
            reasons.append(f"黄金值未覆盖全部题目（{golden_cases}/{len(cases)}）")
        complete = bool(latest and latest.status == "completed" and latest.total == len(cases))
        if not complete:
            reasons.append("尚无覆盖全部题目的完整评测运行")
        if latest and latest.pass_rate < 1.0:
            reasons.append(f"最近完整评测行为通过率为 {latest.pass_rate:.1%}")
        ready = bool(cases and golden_cases == len(cases) and complete and latest and latest.pass_rate == 1.0)
        return EvaluationReadiness(
            total_cases=len(cases), golden_cases=golden_cases,
            golden_coverage=(golden_cases / len(cases) if cases else 0.0),
            latest_run_id=latest.id if latest else None,
            latest_run_status=latest.status if latest else None,
            latest_pass_rate=latest.pass_rate if latest else None,
            latest_run_complete=complete, ready_for_release=ready,
            blocking_reasons=reasons,
        )

    def get_run(self, run_id: str) -> EvaluationRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evaluation_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def compare_runs(self, baseline_run_id: str, candidate_run_id: str) -> EvaluationComparison:
        baseline = self.get_run(baseline_run_id)
        candidate = self.get_run(candidate_run_id)
        baseline_results = {item.case_id: item for item in baseline.results}
        candidate_results = {item.case_id: item for item in candidate.results}
        changes: list[EvaluationCaseChange] = []
        for case_id in sorted(set(baseline_results) | set(candidate_results)):
            before = baseline_results.get(case_id)
            after = candidate_results.get(case_id)
            if before is None:
                change = "added"
            elif after is None:
                change = "removed"
            elif before.passed and not after.passed:
                change = "regressed"
            elif not before.passed and after.passed:
                change = "fixed"
            elif before.passed:
                change = "still_passed"
            else:
                change = "still_failed"
            changes.append(EvaluationCaseChange(
                case_id=case_id, change=change,
                baseline_passed=before.passed if before else None,
                candidate_passed=after.passed if after else None,
            ))
        return EvaluationComparison(
            baseline_run_id=baseline.id, candidate_run_id=candidate.id,
            baseline_target_id=baseline.target_id, candidate_target_id=candidate.target_id,
            pass_rate_delta=candidate.pass_rate - baseline.pass_rate,
            average_duration_delta_ms=(
                self._average_duration(candidate) - self._average_duration(baseline)
            ),
            p50_duration_delta_ms=candidate.p50_duration_ms - baseline.p50_duration_ms,
            p95_duration_delta_ms=candidate.p95_duration_ms - baseline.p95_duration_ms,
            model_calls_delta=candidate.model_calls - baseline.model_calls,
            total_tokens_delta=(
                candidate.total_tokens - baseline.total_tokens
                if candidate.total_tokens is not None and baseline.total_tokens is not None
                else None
            ),
            fixed=sum(item.change == "fixed" for item in changes),
            regressed=sum(item.change == "regressed" for item in changes),
            still_passed=sum(item.change == "still_passed" for item in changes),
            still_failed=sum(item.change == "still_failed" for item in changes),
            changes=changes,
        )

    @staticmethod
    def _average_duration(run: EvaluationRun) -> float:
        if not run.results:
            return 0.0
        return sum(item.duration_ms for item in run.results) / len(run.results)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> EvaluationRun:
        return EvaluationRun(
            id=row["id"], target_type=row["target_type"], target_id=row["target_id"],
            status=row["status"], total=row["total"], passed=row["passed"],
            pass_rate=row["pass_rate"], started_at=row["started_at"],
            value_cases_total=row["value_cases_total"],
            value_cases_passed=row["value_cases_passed"], value_accuracy=row["value_accuracy"],
            model_calls=row["model_calls"] if "model_calls" in row.keys() else 0,
            total_tokens=row["total_tokens"] if "total_tokens" in row.keys() else None,
            p50_duration_ms=row["p50_duration_ms"] if "p50_duration_ms" in row.keys() else 0.0,
            p95_duration_ms=row["p95_duration_ms"] if "p95_duration_ms" in row.keys() else 0.0,
            completed_at=row["completed_at"],
            results=[EvaluationCaseResult.model_validate(item) for item in json.loads(row["results_json"])],
        )


class EvaluationRunner:
    def __init__(self, service, store: EvaluationStore, rule_store=None) -> None:
        self.service = service
        self.store = store
        self.rule_store = rule_store

    async def run_cases(
        self, cases: list[EvaluationCase], *, target_type: Literal["baseline", "rule"],
        target_id: str, candidate_rule_id: str | None = None,
    ) -> EvaluationRun:
        if not cases:
            raise ValueError("评测集不能为空")
        run_id = f"eval_{uuid.uuid4().hex}"
        started_at = datetime.now(UTC).isoformat()
        results = []
        context = (
            self.rule_store.candidate_context(candidate_rule_id)
            if candidate_rule_id and self.rule_store
            else _nullcontext()
        )
        with context:
            for case in cases:
                started = time.monotonic()
                response = await self.service.query(
                    QueryRequest(question=case.question), evaluation_mode=True
                )
                results.append(
                    self._score(case, response, started, self._run_usage(response.request_id))
                )
        passed = sum(item.passed for item in results)
        value_results = [
            result for case, result in zip(cases, results, strict=True) if case.expected_values
        ]
        value_passed = sum(item.values_passed for item in value_results)
        model_calls = sum(item.model_calls for item in results)
        usage_complete = all(
            item.model_calls == 0 or item.total_tokens is not None for item in results
        )
        run = EvaluationRun(
            id=run_id, target_type=target_type, target_id=target_id,
            status="completed", total=len(results), passed=passed,
            pass_rate=passed / len(results), started_at=started_at,
            value_cases_total=len(value_results), value_cases_passed=value_passed,
            value_accuracy=value_passed / len(value_results) if value_results else None,
            model_calls=model_calls,
            total_tokens=(
                sum(item.total_tokens or 0 for item in results)
                if usage_complete else None
            ),
            p50_duration_ms=self._percentile([item.duration_ms for item in results], 0.50),
            p95_duration_ms=self._percentile([item.duration_ms for item in results], 0.95),
            completed_at=datetime.now(UTC).isoformat(), results=results,
        )
        self.store.save_run(run)
        if candidate_rule_id and self.rule_store:
            self.rule_store.record_evaluation_gate(
                candidate_rule_id, run.id, run.pass_rate == 1.0, run.pass_rate
            )
        return run

    def _run_usage(self, request_id: str) -> tuple[int, int | None]:
        event_store = getattr(self.service, "event_store", None)
        if event_store is None:
            return 0, None
        events = [
            event for event in event_store.list(request_id)
            if event.status == "completed" and event.tool == "llm"
        ]
        if not events:
            return 0, 0
        if any(event.total_tokens is None for event in events):
            return len(events), None
        return len(events), sum(event.total_tokens or 0 for event in events)

    @staticmethod
    def _percentile(values: list[int], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999999) - 1)))
        return float(ordered[index])

    @staticmethod
    def _score(
        case: EvaluationCase,
        response: ToolResponse,
        started: float,
        usage: tuple[int, int | None] = (0, None),
    ) -> EvaluationCaseResult:
        if response.success:
            actual = "success"
        elif response.error and response.error.code == "CLARIFICATION_REQUIRED":
            actual = "clarification"
        else:
            actual = "unsupported"
        behavior_passed = actual == case.expected_behavior
        actual_tables = sorted(
            (response.diagnostics or {}).get("plan", {}).get("table_hints", [])
        )
        tables_passed = not case.expected_tables or set(case.expected_tables).issubset(actual_tables)
        flattened = {}
        if response.data:
            flattened.update(response.data.summary)
            if len(response.data.rows) == 1:
                flattened.update(response.data.rows[0])
        values_passed = all(flattened.get(key) == value for key, value in case.expected_values.items())
        issues = []
        if not behavior_passed:
            issues.append(f"期望行为 {case.expected_behavior}，实际 {actual}")
        if not tables_passed:
            issues.append("Planner 未覆盖期望数据表")
        if not values_passed:
            issues.append("确定性结果值不匹配")
        return EvaluationCaseResult(
            case_id=case.id, passed=behavior_passed and tables_passed and values_passed,
            behavior_passed=behavior_passed, tables_passed=tables_passed,
            values_passed=values_passed, actual_behavior=actual,
            actual_tables=actual_tables,
            error_code=response.error.code if response.error else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            request_id=response.request_id, issues=issues,
            model_calls=usage[0], total_tokens=usage[1],
        )


class _nullcontext:
    def __enter__(self): return None
    def __exit__(self, *_args): return False
