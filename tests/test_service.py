import asyncio
import importlib
import json
import sqlite3

import pytest

from app.llm import LLMResponseError
from app.models import QueryPlan, QueryRequest


def load_service_modules():
    try:
        return (
            importlib.import_module("app.audit"),
            importlib.import_module("app.catalog"),
            importlib.import_module("app.executor"),
            importlib.import_module("app.service"),
            importlib.import_module("app.sql_guard"),
        )
    except ModuleNotFoundError:
        pytest.fail("查询服务编排模块尚未实现")


class FakeLLM:
    is_configured = True

    def __init__(self, plan: QueryPlan, sql: str = "") -> None:
        self.plan_result = plan
        self.sql_result = sql
        self.generate_calls = 0

    async def plan(self, question: str, candidate_tables: list[str]) -> QueryPlan:
        return self.plan_result

    async def generate_sql(
        self, question: str, plan: QueryPlan, context: str
    ) -> str:
        self.generate_calls += 1
        return self.sql_result


def build_service(tmp_path, llm, examples=None):
    audit_module, catalog_module, executor_module, service_module, guard_module = (
        load_service_modules()
    )
    db_path = tmp_path / "service.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE stations (id INTEGER, county TEXT, capacity_mw REAL)"
        )
        connection.executemany(
            "INSERT INTO stations VALUES (?, ?, ?)",
            [(1, "张北县", 100.0), (2, "张北县", 150.0), (3, "尚义县", 80.0)],
        )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "datasets": [
                    {
                        "table": "stations",
                        "dataset": "已运行电站",
                        "version": "test-v1",
                        "data_as_of": "2026-07-17",
                        "description": "电站装机数据",
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
        json.dumps(examples or [], ensure_ascii=False), encoding="utf-8"
    )
    catalog = catalog_module.MetadataCatalog(db_path, catalog_path, examples_path)
    executor = executor_module.SQLiteExecutor(db_path, timeout_seconds=2, max_rows=100)
    guard = guard_module.SqlGuard(catalog, max_rows=100)
    audit = audit_module.AuditRepository(tmp_path / "audit.jsonl")
    service = service_module.QueryService(catalog, llm, guard, executor, audit)
    return service, tmp_path / "audit.jsonl"


def request(question: str = "张北县电站装机容量是多少？") -> QueryRequest:
    return QueryRequest(question=question, user_id="u_1", session_id="s_1")


def test_service_executes_validated_query_and_returns_sources(tmp_path) -> None:
    plan = QueryPlan(
        query_type="aggregation",
        table_hints=["stations"],
        metrics=["capacity_mw"],
        filters={"county": "张北县"},
    )
    llm = FakeLLM(
        plan,
        "SELECT SUM(capacity_mw) AS total_capacity_mw "
        "FROM stations WHERE county = '张北县'",
    )
    service, audit_path = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert response.data.rows == [{"total_capacity_mw": 250.0}]
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert response.sources[0].dataset == "已运行电站"
    assert response.sources[0].data_as_of == "2026-07-17"
    assert response.request_id.startswith("qry_")
    assert "select" not in response.model_dump_json().lower()
    audit_text = audit_path.read_text(encoding="utf-8").lower()
    assert "select" not in audit_text
    assert "total_capacity_mw" not in audit_text


def test_service_returns_clarification_without_generating_sql(tmp_path) -> None:
    plan = QueryPlan(
        query_type="aggregation",
        table_hints=["stations"],
        requires_clarification=True,
        clarification_question="请明确需要查询的区县。",
    )
    llm = FakeLLM(plan)
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request("查询电站装机容量")))

    assert response.success is False
    assert response.error.code == "CLARIFICATION_REQUIRED"
    assert response.error.message == "请明确需要查询的区县。"
    assert llm.generate_calls == 0


def test_service_maps_unsafe_sql_to_non_leaking_error(tmp_path) -> None:
    plan = QueryPlan(query_type="list", table_hints=["stations"])
    llm = FakeLLM(plan, "DELETE FROM stations")
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is False
    assert response.error.code == "SQL_VALIDATION_FAILED"
    serialized = response.model_dump_json().lower()
    assert "delete" not in serialized
    assert "stations" not in serialized


def test_service_returns_empty_query_as_success_with_warning(tmp_path) -> None:
    plan = QueryPlan(query_type="list", table_hints=["stations"])
    llm = FakeLLM(
        plan,
        "SELECT id, county FROM stations WHERE county = '不存在的区县'",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert response.data.rows == []
    assert "NO_DATA" in response.warnings


def test_service_uses_exact_verified_example_when_llm_planning_fails(tmp_path) -> None:
    class FailingLLM:
        is_configured = True

        async def plan(self, question, candidate_tables):
            raise LLMResponseError("无效的规划响应")

        async def generate_sql(self, question, plan, context):
            raise AssertionError("规划失败时不应调用 SQL 生成")

    question = "张北县电站装机容量是多少？"
    examples = [
        {
            "question": question,
            "tables": ["stations"],
            "query_plan": {"query_type": "aggregation"},
            "sql": "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
        }
    ]
    service, audit_path = build_service(tmp_path, FailingLLM(), examples)

    response = asyncio.run(service.query(request(question)))

    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert response.warnings == ["LLM_FALLBACK_EXAMPLE"]
    audit_text = audit_path.read_text(encoding="utf-8")
    assert '"fallback":"verified_example"' in audit_text


def test_service_does_not_fallback_when_question_is_not_exact_example(tmp_path) -> None:
    class FailingLLM:
        is_configured = True

        async def plan(self, question, candidate_tables):
            raise LLMResponseError("无效的规划响应")

        async def generate_sql(self, question, plan, context):
            raise AssertionError("规划失败时不应调用 SQL 生成")

    examples = [
        {
            "question": "张北县电站装机容量是多少？",
            "tables": ["stations"],
            "query_plan": {"query_type": "aggregation"},
            "sql": "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
        }
    ]
    service, _ = build_service(tmp_path, FailingLLM(), examples)

    response = asyncio.run(service.query(request("张北县全部电站装机是多少？")))

    assert response.success is False
    assert response.error.code == "SQL_GENERATION_FAILED"
