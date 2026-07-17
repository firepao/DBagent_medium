import importlib
import json
import sqlite3

import pytest


def load_modules():
    try:
        catalog_module = importlib.import_module("app.catalog")
        guard_module = importlib.import_module("app.sql_guard")
        return catalog_module, guard_module
    except ModuleNotFoundError:
        pytest.fail("app.sql_guard 尚未实现")


def build_catalog(tmp_path):
    catalog_module, _ = load_modules()
    db_path = tmp_path / "guard.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE stations (id INTEGER, county TEXT, capacity_mw REAL, secret TEXT)"
        )
        connection.execute("CREATE TABLE hidden (id INTEGER)")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "datasets": [
                    {
                        "table": "stations",
                        "dataset": "电站",
                        "excluded_columns": ["secret"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    examples_path = tmp_path / "examples.json"
    examples_path.write_text("[]", encoding="utf-8")
    return catalog_module.MetadataCatalog(db_path, catalog_path, examples_path)


def test_guard_accepts_allowlisted_select_and_enforces_limit(tmp_path) -> None:
    _, guard_module = load_modules()
    guard = guard_module.SqlGuard(build_catalog(tmp_path), max_rows=100)

    validated = guard.validate(
        "SELECT county, SUM(capacity_mw) AS total FROM stations GROUP BY county"
    )

    assert validated.tables == {"stations"}
    assert validated.columns == {"county", "capacity_mw"}
    assert "LIMIT 100" in validated.sql.upper()


def test_guard_accepts_read_only_cte(tmp_path) -> None:
    _, guard_module = load_modules()
    guard = guard_module.SqlGuard(build_catalog(tmp_path), max_rows=50)

    validated = guard.validate(
        "WITH base AS (SELECT county, capacity_mw FROM stations) "
        "SELECT county, SUM(capacity_mw) AS total FROM base GROUP BY county"
    )

    assert validated.tables == {"stations"}
    assert "LIMIT 50" in validated.sql.upper()


def test_guard_accepts_boolean_filter_operators(tmp_path) -> None:
    _, guard_module = load_modules()
    guard = guard_module.SqlGuard(build_catalog(tmp_path), max_rows=100)

    validated = guard.validate(
        "SELECT county, capacity_mw FROM stations "
        "WHERE county = '张北县' AND capacity_mw > 0"
    )

    assert validated.tables == {"stations"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT county FROM stations; SELECT id FROM stations",
        "DELETE FROM stations",
        "PRAGMA table_info(stations)",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT * FROM hidden",
        "SELECT secret FROM stations",
        "SELECT * FROM stations",
        "SELECT county FROM stations -- bypass",
        "SELECT load_extension('malicious') FROM stations",
    ],
)
def test_guard_rejects_unsafe_or_unpublished_sql(tmp_path, sql: str) -> None:
    _, guard_module = load_modules()
    guard = guard_module.SqlGuard(build_catalog(tmp_path), max_rows=100)

    with pytest.raises(guard_module.SqlValidationError):
        guard.validate(sql)
