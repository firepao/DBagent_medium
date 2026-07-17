import asyncio
import importlib
import sqlite3

import pytest


def load_executor_module():
    try:
        return importlib.import_module("app.executor")
    except ModuleNotFoundError:
        pytest.fail("app.executor 尚未实现")


def build_database(tmp_path):
    db_path = tmp_path / "executor.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE stations (id INTEGER, name TEXT)")
        connection.executemany(
            "INSERT INTO stations VALUES (?, ?)",
            [(1, "A"), (2, "B"), (3, "C")],
        )
    return db_path


def test_executor_returns_dictionary_rows_and_truncation_warning(tmp_path) -> None:
    module = load_executor_module()
    db_path = build_database(tmp_path)
    executor = module.SQLiteExecutor(db_path, timeout_seconds=2, max_rows=2)

    result = asyncio.run(executor.execute("SELECT id, name FROM stations ORDER BY id"))

    assert result.rows == [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    assert result.row_count == 2
    assert result.truncated is True
    assert result.schema == [
        {"name": "id", "type": "integer"},
        {"name": "name", "type": "string"},
    ]


def test_executor_opens_database_read_only(tmp_path) -> None:
    module = load_executor_module()
    db_path = build_database(tmp_path)
    executor = module.SQLiteExecutor(db_path, timeout_seconds=2, max_rows=10)

    with pytest.raises(module.QueryExecutionError):
        asyncio.run(executor.execute("INSERT INTO stations VALUES (4, 'D')"))

    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    assert count == 3


def test_executor_interrupts_query_after_timeout(tmp_path) -> None:
    module = load_executor_module()
    db_path = build_database(tmp_path)
    executor = module.SQLiteExecutor(db_path, timeout_seconds=0.001, max_rows=10)

    expensive_query = (
        "WITH RECURSIVE cnt(x) AS ("
        "SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 10000000"
        ") SELECT SUM(x) AS total FROM cnt"
    )
    with pytest.raises(module.QueryTimeoutError):
        asyncio.run(executor.execute(expensive_query))
