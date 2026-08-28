from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
        CREATE TABLE IF NOT EXISTS rule_versions (
            id TEXT PRIMARY KEY, rule_key TEXT NOT NULL, version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','published','archived')),
            payload_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            published_at TEXT, UNIQUE(rule_key, version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_published_rule_version
        ON rule_versions(rule_key) WHERE status = 'published';
        CREATE TABLE IF NOT EXISTS rule_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, action TEXT NOT NULL,
            actor TEXT NOT NULL, timestamp TEXT NOT NULL, details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rule_evaluation_gates (
            rule_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL, passed INTEGER NOT NULL,
            score REAL NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluation_cases (
            id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluation_runs (
            id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
            status TEXT NOT NULL, total INTEGER NOT NULL, passed INTEGER NOT NULL,
            pass_rate REAL NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            results_json TEXT NOT NULL
        );
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS run_events (
            request_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(request_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS run_events_request_id ON run_events(request_id);
    """),
    (3, """
        ALTER TABLE evaluation_runs ADD COLUMN value_cases_total INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE evaluation_runs ADD COLUMN value_cases_passed INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE evaluation_runs ADD COLUMN value_accuracy REAL;
    """),
    (4, """
        CREATE TABLE IF NOT EXISTS evaluation_case_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            actor TEXT NOT NULL, changed_at TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evaluation_case_audit_case_id
        ON evaluation_case_audit(case_id);
    """),
    (5, """
        ALTER TABLE evaluation_runs ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE evaluation_runs ADD COLUMN total_tokens INTEGER;
    """),
    (6, """
        ALTER TABLE evaluation_runs ADD COLUMN p50_duration_ms REAL NOT NULL DEFAULT 0;
        ALTER TABLE evaluation_runs ADD COLUMN p95_duration_ms REAL NOT NULL DEFAULT 0;
    """),
    (7, """
        CREATE TABLE IF NOT EXISTS agent_sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('active','waiting_user','archived')),
            title TEXT,
            summary TEXT,
            summary_version INTEGER NOT NULL DEFAULT 0,
            catalog_snapshot TEXT NOT NULL,
            rule_versions_json TEXT NOT NULL,
            active_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
            run_id TEXT,
            sequence INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
            content TEXT NOT NULL,
            content_type TEXT NOT NULL,
            tool_name TEXT,
            tool_call_id TEXT,
            parent_message_id TEXT,
            visibility TEXT NOT NULL CHECK (visibility IN ('model','user','internal')),
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS agent_messages_session_seq
            ON agent_messages(session_id, sequence);
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
            request_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            turn_count INTEGER NOT NULL DEFAULT 0,
            sql_count INTEGER NOT NULL DEFAULT 0,
            llm_calls INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS agent_runs_session_time
            ON agent_runs(session_id, started_at);
    """),
    (8, """
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS agent_events_run_seq ON agent_events(run_id, sequence);
        CREATE TABLE IF NOT EXISTS agent_checkpoints (
            run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id),
            sequence INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            saved_at TEXT NOT NULL
        );
    """),
)


def migrate_platform_database(path: str | Path) -> int:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS platform_schema_migrations (
                version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
            )"""
        )
        applied = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT version, checksum FROM platform_schema_migrations"
            ).fetchall()
        }
        for version, script in MIGRATIONS:
            checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise RuntimeError(f"平台数据库迁移 v{version} 校验和不一致")
                continue
            if version == 6:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(evaluation_runs)")
                }
                if "p50_duration_ms" not in columns:
                    connection.execute(
                        "ALTER TABLE evaluation_runs ADD COLUMN p50_duration_ms REAL NOT NULL DEFAULT 0"
                    )
                if "p95_duration_ms" not in columns:
                    connection.execute(
                        "ALTER TABLE evaluation_runs ADD COLUMN p95_duration_ms REAL NOT NULL DEFAULT 0"
                    )
            else:
                connection.executescript(script)
            connection.execute(
                "INSERT INTO platform_schema_migrations VALUES (?, ?, ?)",
                (version, checksum, datetime.now(UTC).isoformat()),
            )
    return MIGRATIONS[-1][0] if MIGRATIONS else 0
