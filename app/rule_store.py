from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RuleInput(BaseModel):
    model_config = {"extra": "forbid"}

    rule_key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=5, max_length=1000)
    business_objects: list[str] = Field(min_length=1, max_length=20)
    metric: str = Field(min_length=1, max_length=120)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    scope_tables: list[str] = Field(min_length=1, max_length=8)
    required_fields: dict[str, list[str]] = Field(default_factory=dict)
    calculation: str = Field(min_length=2, max_length=1000)
    unit: str = Field(min_length=1, max_length=40)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    exceptions: list[str] = Field(default_factory=list, max_length=30)
    examples: list[str] = Field(default_factory=list, max_length=20)
    counter_examples: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("scope_tables", "business_objects", "dimensions")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class RuleVersion(BaseModel):
    id: str
    rule_key: str
    version: int
    status: Literal["draft", "published", "archived"]
    payload: RuleInput
    created_by: str
    created_at: str
    published_at: str | None = None


class RuleValidation(BaseModel):
    valid: bool
    issues: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class RuleEvaluationGate(BaseModel):
    rule_id: str
    evaluation_run_id: str
    passed: bool
    score: float
    recorded_at: str


class RuleAuditEvent(BaseModel):
    id: int
    rule_id: str
    action: str
    actor: str
    timestamp: str
    details: dict[str, Any] = Field(default_factory=dict)


class RuleFieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class RuleVersionDiff(BaseModel):
    rule_key: str
    from_version: int | None = None
    to_version: int
    changes: list[RuleFieldChange] = Field(default_factory=list)


class RuleStore:
    def __init__(self, path: str | Path, catalog: Any, require_evaluation: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog = catalog
        self.require_evaluation = require_evaluation
        self._candidate_rule: ContextVar[RuleVersion | None] = ContextVar(
            "candidate_rule", default=None
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rule_versions (
                    id TEXT PRIMARY KEY,
                    rule_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','published','archived')),
                    payload_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    UNIQUE(rule_key, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_published_rule_version
                ON rule_versions(rule_key) WHERE status = 'published';
                CREATE TABLE IF NOT EXISTS rule_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_evaluation_gates (
                    rule_id TEXT PRIMARY KEY,
                    evaluation_run_id TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    score REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def create_draft(self, payload: RuleInput, actor: str = "local-admin") -> RuleVersion:
        now = self._now()
        with self._connect() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM rule_versions WHERE rule_key = ?",
                (payload.rule_key,),
            ).fetchone()[0]
            rule_id = f"rule_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO rule_versions VALUES (?, ?, ?, 'draft', ?, ?, ?, NULL)",
                (rule_id, payload.rule_key, version, payload.model_dump_json(), actor, now),
            )
            self._audit(connection, rule_id, "draft_created", actor, {"version": version})
        return self.get(rule_id)

    def list(self) -> list[RuleVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rule_versions ORDER BY created_at DESC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, rule_id: str) -> RuleVersion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE id = ?", (rule_id,)
            ).fetchone()
        if row is None:
            raise KeyError(rule_id)
        return self._from_row(row)

    def audit_events(self, rule_id: str) -> list[RuleAuditEvent]:
        self.get(rule_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rule_audit WHERE rule_id = ? ORDER BY id",
                (rule_id,),
            ).fetchall()
        return [
            RuleAuditEvent(
                id=row["id"], rule_id=row["rule_id"], action=row["action"],
                actor=row["actor"], timestamp=row["timestamp"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        ]

    def version_diff(self, rule_id: str) -> RuleVersionDiff:
        current = self.get(rule_id)
        with self._connect() as connection:
            previous_row = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_key = ? AND version < ? "
                "ORDER BY version DESC LIMIT 1",
                (current.rule_key, current.version),
            ).fetchone()
        previous = self._from_row(previous_row) if previous_row is not None else None
        before = previous.payload.model_dump() if previous else {}
        after = current.payload.model_dump()
        changes = [
            RuleFieldChange(field=field, before=before.get(field), after=after.get(field))
            for field in after
            if before.get(field) != after.get(field)
        ]
        return RuleVersionDiff(
            rule_key=current.rule_key,
            from_version=previous.version if previous else None,
            to_version=current.version,
            changes=changes,
        )

    def validate(self, rule_id: str) -> RuleValidation:
        rule = self.get(rule_id)
        payload = rule.payload
        issues: list[str] = []
        allowed_tables = self.catalog.allowed_tables
        for table in payload.scope_tables:
            if table not in allowed_tables:
                issues.append(f"未发布数据表：{table}")
        for table, fields in payload.required_fields.items():
            if table not in payload.scope_tables:
                issues.append(f"字段约束引用了范围外数据表：{table}")
                continue
            if table in allowed_tables:
                missing = sorted(set(fields) - self.catalog.allowed_columns(table))
                if missing:
                    issues.append(f"{table} 包含未发布字段：{', '.join(missing)}")
        conflicts = self._conflicts(payload, exclude_id=rule_id)
        return RuleValidation(valid=not issues and not conflicts, issues=issues, conflicts=conflicts)

    def publish(
        self, rule_id: str, actor: str = "local-admin", *, bypass_evaluation: bool = False
    ) -> RuleVersion:
        rule = self.get(rule_id)
        if rule.status != "draft":
            raise ValueError("只有草稿版本可以发布")
        validation = self.validate(rule_id)
        if not validation.valid:
            raise ValueError("规则未通过发布校验：" + "；".join(validation.issues + validation.conflicts))
        if self.require_evaluation and not bypass_evaluation:
            with self._connect() as connection:
                gate = connection.execute(
                    "SELECT passed, score FROM rule_evaluation_gates WHERE rule_id = ?",
                    (rule_id,),
                ).fetchone()
            if gate is None or not gate["passed"]:
                raise ValueError("规则尚未通过沙箱评测，不能发布")
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE rule_versions SET status = 'archived' WHERE rule_key = ? AND status = 'published'",
                (rule.rule_key,),
            )
            connection.execute(
                "UPDATE rule_versions SET status = 'published', published_at = ? WHERE id = ?",
                (now, rule_id),
            )
            self._audit(connection, rule_id, "published", actor, {"version": rule.version})
        return self.get(rule_id)

    def record_evaluation_gate(
        self, rule_id: str, run_id: str, passed: bool, score: float
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO rule_evaluation_gates VALUES (?, ?, ?, ?, ?)",
                (rule_id, run_id, int(passed), score, self._now()),
            )
            self._audit(
                connection, rule_id, "evaluation_completed", "evaluator",
                {"run_id": run_id, "passed": passed, "score": score},
            )

    def evaluation_gates(self) -> list[RuleEvaluationGate]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rule_evaluation_gates ORDER BY recorded_at DESC"
            ).fetchall()
        return [
            RuleEvaluationGate(
                rule_id=row["rule_id"], evaluation_run_id=row["evaluation_run_id"],
                passed=bool(row["passed"]), score=row["score"], recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def rollback(self, rule_key: str, version: int, actor: str = "local-admin") -> RuleVersion:
        with self._connect() as connection:
            source = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_key = ? AND version = ?",
                (rule_key, version),
            ).fetchone()
        if source is None:
            raise KeyError(f"{rule_key}@{version}")
        draft = self.create_draft(RuleInput.model_validate_json(source["payload_json"]), actor)
        validation = self.validate(draft.id)
        if not validation.valid:
            raise ValueError("历史版本已不符合当前数据目录：" + "；".join(validation.issues + validation.conflicts))
        # A rollback restores a previously published payload for incident recovery.
        # It still passes current catalog validation and remains fully audited.
        published = self.publish(draft.id, actor, bypass_evaluation=True)
        with self._connect() as connection:
            self._audit(connection, published.id, "rolled_back", actor, {"source_version": version})
        return published

    def published_rules(self) -> list[dict[str, Any]]:
        rules = [
            {
                "id": f"managed:{rule.rule_key}:v{rule.version}",
                "status": "published",
                "runtime_enabled": True,
                "scope_tables": rule.payload.scope_tables,
                "required_fields": rule.payload.required_fields,
                "content": rule.payload.description,
                "formula": rule.payload.calculation,
                "unit": rule.payload.unit,
                "business_objects": rule.payload.business_objects,
                "metric": rule.payload.metric,
                "dimensions": rule.payload.dimensions,
                "constraints": rule.payload.constraints,
                "exceptions": rule.payload.exceptions,
                "examples": rule.payload.examples,
                "counter_examples": rule.payload.counter_examples,
            }
            for rule in self.list()
            if rule.status == "published"
        ]
        candidate = self._candidate_rule.get()
        if candidate is not None:
            rules.append(self._runtime_rule(candidate, candidate=True))
        return rules

    @contextmanager
    def candidate_context(self, rule_id: str):
        rule = self.get(rule_id)
        if rule.status != "draft":
            raise ValueError("只有草稿规则可以进入沙箱评测")
        token = self._candidate_rule.set(rule)
        try:
            yield rule
        finally:
            self._candidate_rule.reset(token)

    @staticmethod
    def _runtime_rule(rule: RuleVersion, candidate: bool = False) -> dict[str, Any]:
        return {
            "id": f"managed:{rule.rule_key}:v{rule.version}" + (":candidate" if candidate else ""),
            "status": "published",
            "runtime_enabled": True,
            "scope_tables": rule.payload.scope_tables,
            "required_fields": rule.payload.required_fields,
            "content": rule.payload.description,
            "formula": rule.payload.calculation,
            "unit": rule.payload.unit,
            "business_objects": rule.payload.business_objects,
            "metric": rule.payload.metric,
            "dimensions": rule.payload.dimensions,
            "constraints": rule.payload.constraints,
            "exceptions": rule.payload.exceptions,
            "examples": rule.payload.examples,
            "counter_examples": rule.payload.counter_examples,
        }

    def _conflicts(self, payload: RuleInput, exclude_id: str) -> list[str]:
        conflicts = []
        candidate_tables = set(payload.scope_tables)
        for current in self.list():
            if current.id == exclude_id or current.status != "published":
                continue
            other = current.payload
            if (
                other.rule_key != payload.rule_key
                and
                other.metric.casefold() == payload.metric.casefold()
                and candidate_tables.intersection(other.scope_tables)
                and other.calculation.strip() != payload.calculation.strip()
            ):
                conflicts.append(
                    f"与已发布规则 {other.name}（{current.rule_key}@v{current.version}）的计算口径冲突"
                )
        return conflicts

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RuleVersion:
        return RuleVersion(
            id=row["id"], rule_key=row["rule_key"], version=row["version"],
            status=row["status"], payload=RuleInput.model_validate_json(row["payload_json"]),
            created_by=row["created_by"], created_at=row["created_at"],
            published_at=row["published_at"],
        )

    def _audit(
        self, connection: sqlite3.Connection, rule_id: str, action: str,
        actor: str, details: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO rule_audit(rule_id, action, actor, timestamp, details_json) VALUES (?, ?, ?, ?, ?)",
            (rule_id, action, actor, self._now(), json.dumps(details, ensure_ascii=False)),
        )
