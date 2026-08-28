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
        "-- 字段：id | 别名：主键 | 类型：INTEGER | 说明：测试主键。\n"
        "-- 字段：county | 别名：区县 | 类型：TEXT | 说明：项目所属区县。\n"
        "-- 字段：capacity_mw | 别名：装机容量（MW） | 类型：REAL | 说明：已运行电站装机容量，单位为 MW。\n"
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
                        "coverage": "覆盖已发布的测试电站容量和区县信息。",
                        "aliases": {"装机容量": "capacity_mw"},
                        "metrics": ["装机容量"],
                        "dimensions": ["区县"],
                        "supported_queries": ["已运行电站的容量汇总、区县排行"],
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
                        "reference_enabled": True,
                        "scope_tables": ["published_station"],
                        "terms": ["限电率"],
                        "content": "候选规则，不得进入运行期。",
                    },
                ],
                "answer_guidance": {
                    "default": {"required_sections": ["结论", "来源"]},
                    "profiles": [
                        {
                            "id": "capacity_answer",
                            "terms": ["装机容量"],
                            "scope_tables": ["published_station"],
                            "guidance": {"template": "容量回答模板"},
                        }
                    ],
                },
                "routing_rules": [
                    {
                        "id": "unsupported_metric",
                        "status": "published",
                        "runtime_enabled": True,
                        "terms": ["无法计算指标"],
                        "action": "reject_capability",
                        "required_tables": ["published_station"],
                        "message": "当前数据不支持该指标。",
                    }
                ],
                "concept_alternatives": [
                    {
                        "id": "owner_capacity_scope",
                        "status": "published",
                        "runtime_enabled": True,
                        "all_terms": ["业主", "装机"],
                        "none_terms": ["备案", "拟建"],
                        "message": "当前没有经确认的业主单位口径，可按归属上级集团或项目建设方汇总，请确认采用哪一种。",
                        "suggestions": [
                            {"business_label": "归属上级集团"},
                            {"business_label": "项目建设方"},
                        ],
                    }
                ],
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
                        "routing_enabled": True,
                        "customer_note": "并网容量就是装机容量",
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
    assert "capacity_mw" in planning_context
    assert "容量字段仅可作为测试样例" in planning_context
    assert 'CREATE TABLE "published_station"' in sql_context
    assert "装机容量使用 capacity_mw 聚合" in sql_context
    assert "候选规则，不得进入运行期" in sql_context
    assert "容量字段仅可作为测试样例" in sql_context
    assert catalog.validation_case("各区县装机容量排行")["id"] == "Q1"


def test_context_exposes_table_scope_and_full_published_field_semantics(tmp_path) -> None:
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
    sql_context = catalog.build_sql_context("查询各区县装机容量", ["published_station"])

    assert "supported_queries" in planning_context
    assert "已运行电站的容量汇总、区县排行" in planning_context
    assert "字段语义" in sql_context
    assert "装机容量（MW）" in sql_context
    assert "已运行电站装机容量，单位为 MW" in sql_context


def test_expected_result_contract_exposes_authoritative_snapshot_time(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    catalog = module.MetadataCatalog(db_path, catalog_path, examples_path)
    from app.models import QueryPlan

    plan = QueryPlan(
        original_question="已运行电站装机容量是多少？",
        query_type="aggregation",
        table_hints=["published_station"],
        required_outputs=["装机容量"],
        time_requirements=["注明数据时间"],
    )

    contract = catalog.expected_result_contract(
        plan.original_question, plan.table_hints, plan
    )

    assert contract["authoritative_data_as_of"] == "2026-07-17"


def test_routing_prefers_exact_validation_intent_then_uses_published_terms(tmp_path) -> None:
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

    exact = catalog.routing_decision("各区县装机容量排行")
    terms = catalog.routing_decision("请查询无法计算指标")

    assert exact.intent_id == "Q1"
    assert exact.action == "allow"
    assert exact.customer_context["customer_note"] == "并网容量就是装机容量"
    assert terms.intent_id == "unsupported_metric"
    assert terms.action == "reject_capability"
    assert terms.match_type == "lightweight_terms"


def test_concept_alternative_requires_all_terms_and_respects_exclusions(tmp_path) -> None:
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

    clarification = catalog.concept_clarification("各业主单位装机容量汇总")

    assert clarification["id"] == "owner_capacity_scope"
    assert "归属上级集团" in clarification["message"]
    assert "项目建设方" in clarification["message"]
    assert catalog.concept_clarification("各业主单位有哪些") is None
    assert catalog.concept_clarification("备案项目拟定业主装机规模") is None


def test_runtime_rule_issues_validate_concept_alternatives(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    ddl_dir, cards, registry, knowledge, cases = build_query_knowledge_files(tmp_path)
    payload = json.loads(knowledge.read_text(encoding="utf-8"))
    payload["concept_alternatives"][0]["suggestions"] = []
    knowledge.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

    assert "概念替代 owner_capacity_scope 缺少业务口径建议" in catalog.runtime_rule_issues()


def test_context_keeps_candidate_formula_as_non_executable_reference_and_returns_answer_guidance(
    tmp_path,
) -> None:
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

    context = catalog.build_sql_context("限电率如何计算", ["published_station"])
    guidance = catalog.answer_guidance(
        "各区县装机容量排行", {"published_station"}, None
    )

    assert "待确认辅助规则" in context
    assert "候选规则，不得进入运行期" in context
    assert guidance["profile_id"] == "capacity_answer"
    assert guidance["profile_guidance"]["template"] == "容量回答模板"


def test_source_info_falls_back_to_table_card_dataset_name(tmp_path) -> None:
    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    ddl_dir, cards, registry, knowledge, cases = build_query_knowledge_files(tmp_path)
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["datasets"][0].pop("dataset")
    catalog_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

    assert catalog.source_info({"published_station"})[0]["dataset"] == "已发布电站"


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
