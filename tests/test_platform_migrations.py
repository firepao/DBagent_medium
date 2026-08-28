import sqlite3

import pytest

from app import platform_migrations


def test_platform_migration_is_versioned_and_idempotent(tmp_path):
    path = tmp_path / "platform.sqlite3"
    assert platform_migrations.migrate_platform_database(path) == 8
    assert platform_migrations.migrate_platform_database(path) == 8
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM platform_schema_migrations"
        ).fetchone()[0]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == 8
    assert {"rule_versions", "evaluation_cases", "evaluation_runs", "agent_sessions", "agent_messages", "agent_runs"}.issubset(tables)


def test_changed_historical_migration_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "platform.sqlite3"
    platform_migrations.migrate_platform_database(path)
    monkeypatch.setattr(platform_migrations, "MIGRATIONS", ((1, "SELECT 1;"), *platform_migrations.MIGRATIONS[1:]))
    with pytest.raises(RuntimeError, match="校验和不一致"):
        platform_migrations.migrate_platform_database(path)


def test_v6_adopts_columns_created_by_transitional_component_compatibility(tmp_path):
    path = tmp_path / "platform.sqlite3"
    migrations_through_v5 = platform_migrations.MIGRATIONS[:5]
    original = platform_migrations.MIGRATIONS
    platform_migrations.MIGRATIONS = migrations_through_v5
    try:
        platform_migrations.migrate_platform_database(path)
    finally:
        platform_migrations.MIGRATIONS = original
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE evaluation_runs ADD COLUMN p50_duration_ms REAL NOT NULL DEFAULT 0"
        )
        connection.execute(
            "ALTER TABLE evaluation_runs ADD COLUMN p95_duration_ms REAL NOT NULL DEFAULT 0"
        )

    assert platform_migrations.migrate_platform_database(path) == 8
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_schema_migrations WHERE version = 6"
        ).fetchone()[0] == 1
