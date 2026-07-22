import asyncio
import importlib
import json
import sqlite3

import pytest

from app.llm import LLMPlanSchemaError, LLMResponseError
from app.models import QueryPlan, QueryRequest, SqlSemanticReview


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
        self.repair_calls = 0
        self.planning_context = None
        self.generated_plan = None

    async def plan(self, question: str, planning_context: str) -> QueryPlan:
        self.planning_context = planning_context
        return self.plan_result

    async def generate_sql(
        self, question: str, plan: QueryPlan, context: str
    ) -> str:
        self.generate_calls += 1
        self.generated_plan = plan
        return self.sql_result

    async def review_sql(
        self, question: str, plan: QueryPlan, context: str, candidate_sql: str
    ) -> SqlSemanticReview:
        return SqlSemanticReview(decision="pass")

    async def repair_sql(
        self, question: str, plan: QueryPlan, context: str, candidate_sql: str, feedback: str
    ) -> str:
        self.repair_calls += 1
        return self.sql_result


def build_service(
    tmp_path,
    llm,
    examples=None,
    diagnostics_enabled=False,
    validation_cases=None,
):
    audit_module, catalog_module, executor_module, service_module, guard_module = (
        load_service_modules()
    )
    db_path = tmp_path / "service.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE stations (id INTEGER, county TEXT, capacity_mw REAL)"
        )
        connection.execute("CREATE TABLE station_notes (id INTEGER, note TEXT)")
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
                ,
                    {
                        "table": "station_notes",
                        "dataset": "电站备注",
                        "version": "test-v1",
                        "data_as_of": "2026-07-17",
                        "description": "电站补充备注",
                    },
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
    validation_cases_path = None
    if validation_cases is not None:
        validation_cases_path = tmp_path / "validation_cases.json"
        validation_cases_path.write_text(
            json.dumps({"cases": validation_cases}, ensure_ascii=False),
            encoding="utf-8",
        )
    catalog = catalog_module.MetadataCatalog(
        db_path,
        catalog_path,
        examples_path,
        validation_cases_path=validation_cases_path,
    )
    executor = executor_module.SQLiteExecutor(db_path, timeout_seconds=2, max_rows=100)
    guard = guard_module.SqlGuard(catalog, max_rows=100)
    audit = audit_module.AuditRepository(tmp_path / "audit.jsonl")
    service = service_module.QueryService(
        catalog,
        llm,
        guard,
        executor,
        audit,
        diagnostics_enabled=diagnostics_enabled,
    )
    return service, tmp_path / "audit.jsonl"


def request(question: str = "张北县电站装机容量是多少？") -> QueryRequest:
    return QueryRequest(question=question)


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
    assert llm.repair_calls == 0
    serialized = response.model_dump_json().lower()
    assert "delete" not in serialized
    assert "stations" not in serialized


def test_service_repairs_one_rejected_sql_before_execution(tmp_path) -> None:
    class RepairingLLM(FakeLLM):
        async def repair_sql(self, question, plan, context, candidate_sql, feedback):
            self.repair_calls += 1
            assert "未发布的字段" in feedback
            return "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations"

    llm = RepairingLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(unknown_capacity) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert llm.repair_calls == 1
    assert response.data.summary == {"total_capacity_mw": 330.0}


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
    assert "NO_DATA_AFTER_VALID_FILTER" in response.warnings
    assert response.data.result_status == "no_match"


def test_service_rewrites_sql_after_semantic_review(tmp_path) -> None:
    class RewritingLLM(FakeLLM):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.review_calls = 0

        async def review_sql(self, question, plan, context, candidate_sql):
            self.review_calls += 1
            if self.review_calls > 1:
                return SqlSemanticReview(decision="pass")
            return SqlSemanticReview(
                decision="rewrite",
                semantic_issues=["需要限定张北县"],
                corrected_sql="SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
            )

    llm = RewritingLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert llm.review_calls == 2


def test_service_stops_when_semantic_review_requires_clarification(tmp_path) -> None:
    class ClarifyingLLM(FakeLLM):
        async def review_sql(self, question, plan, context, candidate_sql):
            return SqlSemanticReview(
                decision="clarification",
                clarification_question="请明确统计范围。",
            )

    llm = ClarifyingLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is False
    assert response.error.code == "CLARIFICATION_REQUIRED"
    assert response.error.message == "请明确统计范围。"


def test_service_returns_capability_not_supported_from_semantic_review(tmp_path) -> None:
    class UnsupportedLLM(FakeLLM):
        async def review_sql(self, question, plan, context, candidate_sql):
            return SqlSemanticReview(
                decision="unsupported",
                semantic_issues=["缺少可发布的计算口径"],
            )

    llm = UnsupportedLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is False
    assert response.error.code == "CAPABILITY_NOT_SUPPORTED"
    assert "缺少可发布的计算口径" in response.error.message
    assert "请按上述缺口" in response.error.message


def test_service_hides_internal_sql_terms_from_semantic_failure(tmp_path) -> None:
    class UnsupportedLLM(FakeLLM):
        async def review_sql(self, question, plan, context, candidate_sql):
            return SqlSemanticReview(
                decision="unsupported",
                semantic_issues=["SELECT 使用了 t01_internal_table 的错误字段"],
            )

    llm = UnsupportedLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is False
    assert "select" not in response.error.message.lower()
    assert "t01_internal_table" not in response.error.message
    assert "查询指标、时间范围或业务口径" in response.error.message


def test_service_maps_invalid_plan_schema_to_specific_error(tmp_path) -> None:
    class InvalidPlanLLM:
        is_configured = True

        async def plan(self, question, planning_context):
            raise LLMPlanSchemaError("规划字段不完整")

        async def generate_sql(self, question, plan, context):
            raise AssertionError("无效规划不能生成 SQL")

    service, _ = build_service(tmp_path, InvalidPlanLLM())

    response = asyncio.run(service.query(request()))

    assert response.success is False
    assert response.error.code == "LLM_PLAN_SCHEMA_INVALID"
    assert response.error.retryable is False


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
    assert response.error.code == "PLANNING_FAILED"


def test_service_uses_exact_verified_example_after_generated_sql_validation_fails(
    tmp_path,
) -> None:
    question = "张北县电站装机容量是多少？"
    examples = [
        {
            "question": question,
            "tables": ["stations"],
            "query_plan": {"query_type": "aggregation"},
            "sql": "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
        }
    ]
    llm = FakeLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT * FROM stations",
    )
    service, _ = build_service(tmp_path, llm, examples=examples)

    response = asyncio.run(service.query(request(question)))

    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert "LLM_FALLBACK_EXAMPLE" in response.warnings


def test_service_hides_diagnostics_when_disabled(tmp_path) -> None:
    plan = QueryPlan(query_type="list", table_hints=["stations"])
    llm = FakeLLM(plan, "SELECT id, county FROM stations")
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert response.diagnostics is None


def test_service_returns_planning_diagnostics_when_enabled(tmp_path) -> None:
    class FailingLLM:
        is_configured = True

        async def plan(self, question, candidate_tables):
            raise LLMResponseError("invalid planning response")

        async def generate_sql(self, question, plan, context):
            raise AssertionError("planning failure must not generate SQL")

    service, _ = build_service(
        tmp_path,
        FailingLLM(),
        diagnostics_enabled=True,
    )

    response = asyncio.run(service.query(request("查询电站装机容量")))

    assert response.success is False
    assert response.error.code == "PLANNING_FAILED"
    assert response.diagnostics == {
        "stage": "planning",
        "plan": {"status": "failed"},
            "sql_generation": {"status": "not_started", "sql": None},
            "sql_validation": {"status": "not_started", "result": None},
            "sql_repair": {"attempts": 0, "status": "not_started"},
            "semantic_review": {"status": "not_started", "decision": None},
            "fallback": {
            "attempted": True,
            "exact_question_match": False,
            "used": False,
        },
    }


def test_service_returns_generated_sql_and_validation_when_enabled(tmp_path) -> None:
    plan = QueryPlan(query_type="list", table_hints=["stations"])
    sql = "SELECT id, county FROM stations"
    service, _ = build_service(
        tmp_path,
        FakeLLM(plan, sql),
        diagnostics_enabled=True,
    )

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert response.diagnostics == {
        "stage": "completed",
        "plan": {"status": "passed", "table_hints": ["stations"]},
        "sql_generation": {"status": "generated", "sql": sql},
            "sql_validation": {
                "status": "passed",
                "result": {"tables": ["stations"]},
            },
            "sql_repair": {"attempts": 0, "status": "not_started"},
            "semantic_review": {"status": "completed", "decision": "pass", "attempt": 1},
            "fallback": {
            "attempted": False,
            "exact_question_match": False,
            "used": False,
        },
    }


def test_service_uses_full_table_cards_when_keywords_do_not_match(tmp_path) -> None:
    plan = QueryPlan(query_type="aggregation", table_hints=["stations"])
    llm = FakeLLM(plan, "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations")
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request("全市新能源资产规模是多少？")))

    assert response.success is True
    assert "stations" in llm.planning_context
    assert "已运行电站" in llm.planning_context


def test_service_allows_sql_to_use_a_semantically_sufficient_subset_of_planned_tables(
    tmp_path,
) -> None:
    llm = FakeLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
    )
    service, _ = build_service(
        tmp_path,
        llm,
        validation_cases=[
            {
                "id": "QX",
                "question": "测试含补充表的装机问题",
                "status": "supported",
                "routing_enabled": True,
                "scope_tables": ["stations", "station_notes"],
            }
        ],
    )

    response = asyncio.run(service.query(request("测试含补充表的装机问题")))

    assert response.success is True
    assert llm.generated_plan.table_hints == ["station_notes", "stations"]


def test_service_rejects_exact_unavailable_intent_before_calling_llm(tmp_path) -> None:
    class NeverCalledLLM:
        is_configured = True

        async def plan(self, question, planning_context):
            raise AssertionError("前置路由拒绝的问题不得调用规划模型")

    service, _ = build_service(
        tmp_path,
        NeverCalledLLM(),
        validation_cases=[
            {
                "id": "QY",
                "question": "最近三个月新增备案项目数",
                "status": "not_supported",
                "routing_enabled": True,
                "scope_tables": ["stations"],
                "reason": "没有备案日期。",
            }
        ],
    )

    response = asyncio.run(service.query(request("最近三个月新增备案项目数")))

    assert response.success is False
    assert response.error.code == "CAPABILITY_NOT_SUPPORTED"
    assert response.error.message == "没有备案日期。"


def test_service_rejects_unknown_table_after_planning(tmp_path) -> None:
    class InvalidTablePlanLLM:
        is_configured = True

        def __init__(self) -> None:
            self.plan_called = False

        async def plan(self, question: str, planning_context: str) -> QueryPlan:
            self.plan_called = True
            return QueryPlan(query_type="list", table_hints=["hidden_secret"])

        async def generate_sql(self, question, plan, context):
            raise AssertionError("未发布表不能进入 SQL 生成")

    llm = InvalidTablePlanLLM()
    service, _ = build_service(tmp_path, llm)

    response = asyncio.run(service.query(request("测试问题")))

    assert llm.plan_called is True
    assert response.success is False
    assert response.error.code == "QUERY_NOT_SUPPORTED"
