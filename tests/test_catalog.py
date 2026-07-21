import importlib
import json
import sqlite3
from hashlib import sha256

import pytest


def load_catalog_module():
    try:
        return importlib.import_module("app.catalog")
    except ModuleNotFoundError:
        pytest.fail("app.catalog 尚未实现")


def build_catalog_files(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE published_station (id INTEGER, county TEXT, capacity_mw REAL)"
        )
        connection.execute("CREATE TABLE hidden_secret (id INTEGER, secret TEXT)")

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "datasets": [
                    {
                        "table": "published_station",
                        "dataset": "已运行电站",
                        "version": "test-v1",
                        "data_as_of": "2026-07-17",
                        "description": "已发布电站数据",
                        "keywords": ["电站", "装机", "容量"],
                        "aliases": {"county": "区县", "capacity_mw": "装机容量"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    examples_path = tmp_path / "examples.json"
    examples_path.write_text(
        json.dumps(
            [
                {
                    "question": "各区县装机容量排行",
                    "tables": ["published_station"],
                    "query_plan": {"query_type": "ranking"},
                    "sql": "SELECT county, SUM(capacity_mw) FROM published_station GROUP BY county",
                },
                {
                    "question": "查询秘密",
                    "tables": ["hidden_secret"],
                    "query_plan": {"query_type": "list"},
                    "sql": "SELECT * FROM hidden_secret",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return db_path, catalog_path, examples_path


def build_query_knowledge_files(tmp_path):
    ddl_dir = tmp_path / "ddls"
    ddl_dir.mkdir()
    ddl = (
        'CREATE TABLE "published_station" '
        '(id INTEGER, county TEXT, capacity_mw REAL);\n'
    )
    (ddl_dir / "published_station.txt").write_text(ddl, encoding="utf-8")

    table_cards_path = tmp_path / "table_cards.json"
    table_cards_path.write_text(
        json.dumps(
            {
                "table_cards": [
                    {
                        "table": "published_station",
                        "dataset": "已发布电站",
                        "description": "电站装机容量和区县。",
                        "aliases": {"装机容量": "capacity_mw"},
                        "metrics": ["装机容量"],
                        "dimensions": ["区县"],
                        "important_fields": ["county", "capacity_mw"],
                        "data_limitations": ["容量字段仅可作为测试样例。"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry_path = tmp_path / "ddl_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "tables": {
                    "published_station": {
                        "file": "published_station.txt",
                        "sha256": sha256(ddl.encode("utf-8")).hexdigest(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    knowledge_path = tmp_path / "query_knowledge.json"
    knowledge_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "published_capacity_rule",
                        "status": "published",
                        "runtime_enabled": True,
                        "scope_tables": ["published_station"],
                        "terms": ["装机容量"],
                        "content": "装机容量使用 capacity_mw 聚合。",
                    },
                    {
                        "id": "candidate_rule",
                        "status": "needs_customer_confirmation",
                        "runtime_enabled": False,
                        "scope_tables": ["published_station"],
                        "terms": ["限电率"],
                        "content": "候选规则，不得进入运行期。",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    validation_cases_path = tmp_path / "validation_cases.json"
    validation_cases_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "Q1",
                        "question": "各区县装机容量排行",
                        "status": "supported",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return (
        ddl_dir,
        table_cards_path,
        registry_path,
        knowledge_path,
        validation_cases_path,
    )


def test_catalog_only_exposes_published_tables_and_columns(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    catalog = module.MetadataCatalog(db_path, catalog_path, examples_path)

    assert catalog.allowed_tables == {"published_station"}
    assert catalog.allowed_columns("published_station") == {
        "id",
        "county",
        "capacity_mw",
    }
    with pytest.raises(module.CatalogError):
        catalog.allowed_columns("hidden_secret")


def test_context_contains_only_requested_published_schema_and_examples(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    catalog = module.MetadataCatalog(db_path, catalog_path, examples_path)

    context = catalog.build_context(["published_station"], max_examples=3)

    assert "published_station" in context
    assert "capacity_mw" in context
    assert "装机容量" in context
    assert "各区县装机容量排行" in context
    assert "hidden_secret" not in context


def test_catalog_rejects_unpublished_context_request(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    catalog = module.MetadataCatalog(db_path, catalog_path, examples_path)

    with pytest.raises(module.CatalogError):
        catalog.build_context(["hidden_secret"])


def test_planning_context_has_all_table_cards_and_sql_context_is_scoped(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    ddl_dir, cards, registry, knowledge, cases = build_query_knowledge_files(tmp_path)
    catalog = module.MetadataCatalog(
        db_path,
        catalog_path,
        examples_path,
        table_cards_path=cards,
        ddl_registry_path=registry,
        query_knowledge_path=knowledge,
        validation_cases_path=cases,
        ddl_directory=ddl_dir,
    )

    planning_context = catalog.build_planning_context()
    sql_context = catalog.build_sql_context(
        "各区县装机容量排行", ["published_station"]
    )

    assert "published_station" in planning_context
    assert "capacity_mw" not in planning_context
    assert "容量字段仅可作为测试样例" in planning_context
    assert 'CREATE TABLE "published_station"' in sql_context
    assert "装机容量使用 capacity_mw 聚合" in sql_context
    assert "候选规则，不得进入运行期" not in sql_context
    assert "容量字段仅可作为测试样例" in sql_context
    assert catalog.validation_case("各区县装机容量排行")["id"] == "Q1"


def test_explicit_missing_query_knowledge_config_is_a_startup_error(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)

    with pytest.raises(module.CatalogError, match="配置文件不存在"):
        module.MetadataCatalog(
            db_path,
            catalog_path,
            examples_path,
            table_cards_path=tmp_path / "missing-table-cards.json",
        )


def test_registered_ddl_rejects_sqlite_type_or_column_order_drift(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    ddl_dir, cards, registry, knowledge, cases = build_query_knowledge_files(tmp_path)
    drifted_ddl = (
        'CREATE TABLE "published_station" '
        '(id INTEGER, county TEXT, capacity_mw TEXT);\n'
    )
    (ddl_dir / "published_station.txt").write_text(drifted_ddl, encoding="utf-8")
    registry.write_text(
        json.dumps(
            {
                "tables": {
                    "published_station": {
                        "file": "published_station.txt",
                        "sha256": sha256(drifted_ddl.encode("utf-8")).hexdigest(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = module.MetadataCatalog(
        db_path,
        catalog_path,
        examples_path,
        table_cards_path=cards,
        ddl_registry_path=registry,
        query_knowledge_path=knowledge,
        validation_cases_path=cases,
        ddl_directory=ddl_dir,
    )

    with pytest.raises(module.CatalogError, match="DDL 与 SQLite 结构不一致"):
        catalog.load_ddl("published_station")
