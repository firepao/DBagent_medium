import importlib
import json
import sqlite3

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
