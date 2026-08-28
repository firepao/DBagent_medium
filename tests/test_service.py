import asyncio
import json
import sqlite3
from datetime import date

from app.models import PreExecutionReview, QueryPlan, QueryRequest, ResultReview, ReviewIssue


def test_pre_review_normalizes_only_snapshot_time_misinterpretation():
    from app.service import QueryService

    review = PreExecutionReview(
        decision="revise",
        issues=[
            ReviewIssue(code="DATA_TIME_NOT_IN_QUERY", message="缺少数据时间字段"),
            ReviewIssue(code="DATA_TIME_SEMANTICS_MISMATCH", message="数据时间语义不一致"),
        ],
        required_changes=["增加数据时间"],
    )

    normalized = QueryService._normalize_pre_review(
        review, {"authoritative_data_as_of": "2026-07-17"}
    )

    assert normalized.decision == "pass"
    assert normalized.issues == []
    assert normalized.required_changes == []


def test_pre_review_keeps_mixed_business_issues_for_revision():
    from app.service import QueryService

    review = PreExecutionReview(
        decision="revise",
        issues=[
            ReviewIssue(code="DATA_TIME_NOT_IN_QUERY", message="缺少数据时间字段"),
            ReviewIssue(code="WRONG_SCOPE", message="范围不符合问题"),
        ],
        required_changes=["修正范围"],
    )

    normalized = QueryService._normalize_pre_review(
        review, {"authoritative_data_as_of": "2026-07-17"}
    )

    assert normalized.decision == "revise"


def test_query_data_normalizes_only_suspicious_current_date(tmp_path):
    from app.models import SourceInfo
    from app.service import QueryService
    from app.executor import ExecutionResult

    service = object.__new__(QueryService)
    source = SourceInfo(dataset="测试", version="v1", data_as_of="2026-07-17")
    execution = ExecutionResult(
        rows=[{"数据时间": date.today().isoformat(), "历史时间": "2026-01-01", "value": 1}],
        row_count=1,
        schema=[
            {"name": "数据时间", "type": "string"},
            {"name": "历史时间", "type": "string"},
            {"name": "value", "type": "integer"},
        ],
        truncated=False,
        duration_ms=1,
    )

    normalized = service._query_data(execution, [source])

    assert normalized.data_as_of == "2026-07-17"
    assert normalized.rows[0]["数据时间"] == "2026-07-17"
    assert normalized.rows[0]["历史时间"] == "2026-01-01"


def test_query_data_preserves_real_historical_data_time(tmp_path):
    from app.models import SourceInfo
    from app.service import QueryService
    from app.executor import ExecutionResult

    service = object.__new__(QueryService)
    source = SourceInfo(dataset="测试", version="v1", data_as_of="2026-07-17")
    execution = ExecutionResult(
        rows=[{"数据时间": "2025-12-31", "value": 1}],
        row_count=1,
        schema=[{"name": "数据时间", "type": "string"}, {"name": "value", "type": "integer"}],
        truncated=False,
        duration_ms=1,
    )

    normalized = service._query_data(execution, [source])

    assert normalized.rows[0]["数据时间"] == "2025-12-31"


class FakeLLM:
    is_configured = True

    def __init__(self, plan, sql, *, pre_reviews=None, result_reviews=None, generated=None):
        self.plan_result = plan
        self.sql_result = sql
        self.pre_reviews = list(pre_reviews or [PreExecutionReview(decision="pass")])
        self.result_reviews = list(result_reviews or [ResultReview(decision="answer")])
        self.generated = list(generated or [])
        self.calls = []
        self.plan_calls = 0
        self.plan_questions = []
        self.generated_plans = []
        self.planning_context = ""
        self.last_call_metadata = {
            "model": "fake-model",
            "provider": "fake-provider",
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }

    async def plan(self, question, planning_context):
        self.plan_calls += 1
        self.plan_questions.append(question)
        self.planning_context = planning_context
        if isinstance(self.plan_result, list):
            return self.plan_result.pop(0)
        return self.plan_result

    async def generate_sql(self, question, plan, context, *, task_mode="initial", previous_sql=None, feedback=None):
        self.generated_plans.append(plan)
        self.calls.append(("generate", task_mode, previous_sql, feedback))
        return self.generated.pop(0) if self.generated else self.sql_result

    async def review_before_execution(self, question, plan, context, candidate_sql, expected_result_contract):
        self.calls.append(("pre", candidate_sql, expected_result_contract))
        return self.pre_reviews.pop(0)

    async def review_result(self, question, plan, expected_result_contract, evidence):
        self.calls.append(("result", evidence))
        return self.result_reviews.pop(0)


def build_service(
    tmp_path,
    llm,
    *,
    diagnostics_enabled=False,
    object_scope=False,
    concept_alternatives=False,
):
    from app.audit import AuditRepository
    from app.catalog import MetadataCatalog
    from app.executor import SQLiteExecutor
    from app.service import QueryService
    from app.sql_guard import SqlGuard

    db_path = tmp_path / "service.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE stations (id INTEGER, county TEXT, capacity_mw REAL, phone TEXT)")
        connection.executemany("INSERT INTO stations VALUES (?, ?, ?, ?)", [(1, "张北县", 100.0, "13800000000"), (2, "张北县", 150.0, "13900000000"), (3, "尚义县", 80.0, "13700000000")])
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"datasets": [{"table": "stations", "dataset": "已运行电站", "version": "test-v1", "data_as_of": "2026-07-17", "description": "电站装机数据", "aliases": {"county": "区县", "capacity_mw": "装机容量"}}]}, ensure_ascii=False), encoding="utf-8")
    examples_path = tmp_path / "examples.json"
    examples_path.write_text("[]", encoding="utf-8")
    table_cards_path = None
    if object_scope:
        table_cards_path = tmp_path / "table_cards.json"
        table_cards_path.write_text(
            json.dumps(
                {
                    "table_cards": [
                        {
                            "table": "stations",
                            "dataset": "已运行电站",
                            "description": "已运行电站装机数据",
                            "coverage": "全表均为已运行电站",
                            "supported_queries": ["已运行电站装机查询"],
                            "aliases": {"区县": "county", "装机容量": "capacity_mw"},
                            "metrics": [],
                            "dimensions": [],
                            "important_fields": ["county", "capacity_mw"],
                            "data_limitations": [],
                            "object_scope": {
                                "object_terms": ["已运行电站"],
                                "row_scope": "all_rows",
                                "description": "本表全体记录均为已运行电站",
                                "status_filters": [
                                    {
                                        "field": "county",
                                        "semantic_label": "区县",
                                        "requires_explicit_user_value": True,
                                        "allowed_values": ["张北县", "尚义县"],
                                    }
                                ],
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    query_knowledge_path = None
    if concept_alternatives:
        query_knowledge_path = tmp_path / "query_knowledge.json"
        query_knowledge_path.write_text(
            json.dumps(
                {
                    "concept_alternatives": [
                        {
                            "id": "owner_capacity_scope",
                            "status": "published",
                            "runtime_enabled": True,
                            "all_terms": ["业主", "装机"],
                            "none_terms": ["备案", "拟建"],
                            "message": "当前没有经确认的“业主单位”统计口径。现有数据可按“归属上级集团”或“项目建设方”汇总装机容量。请补充为“按归属上级集团汇总已运行集中式新能源电站装机容量”或“按项目建设方汇总已运行集中式新能源电站装机容量”。",
                            "suggestions": [
                                {"business_label": "归属上级集团"},
                                {"business_label": "项目建设方"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    catalog = MetadataCatalog(
        db_path,
        catalog_path,
        examples_path,
        table_cards_path=table_cards_path,
        query_knowledge_path=query_knowledge_path,
    )
    return QueryService(catalog, llm, SqlGuard(catalog, 100), SQLiteExecutor(db_path, 2, 100), AuditRepository(tmp_path / "audit.jsonl"), diagnostics_enabled=diagnostics_enabled)


def test_query_service_health_blocks_production_without_admin_key(tmp_path):
    service = build_service(tmp_path, FakeLLM(None, "SELECT 1"))
    service.deployment_mode = "production"
    service.admin_api_key = ""
    service.viewer_api_key = ""

    state = service.health()

    assert state["status"] == "degraded"
    assert state["management_auth"]["deployment_mode"] == "production"
    assert state["management_auth"]["mode"] == "production_unconfigured"
    assert state["management_auth"]["ready"] is False


def request(question="张北县电站装机容量是多少？"):
    return QueryRequest(question=question)


def plan():
    return QueryPlan(
        original_question="张北县电站装机容量是多少？",
        query_type="aggregation",
        table_hints=["stations"],
        required_outputs=["张北县电站装机容量"],
        business_objects=["电站"],
        presentation_requirements=["汇总值"],
    )


def test_service_runs_pre_and_post_review_then_returns_compatible_primary_data(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'")
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert response.result_sets[0].id == "primary"
    assert response.coverage.applied_scope == "张家口市全域"
    assert response.coverage.dimensions == []
    assert response.coverage.measures == ["total_capacity_mw"]
    assert response.data.schema_[0]["semantic_label"] == "装机容量合计"
    assert [call[0] for call in llm.calls] == ["generate", "pre", "result"]


def test_unexpected_agent_exception_is_sanitized(tmp_path):
    class BrokenLLM(FakeLLM):
        async def plan(self, question, planning_context):
            raise RuntimeError("secret prompt / DDL / C:\\private\\database.sqlite3")

    response = asyncio.run(build_service(tmp_path, BrokenLLM(plan(), "SELECT 1")).query(request()))

    assert response.success is False
    assert response.error.code == "INTERNAL_ERROR"
    assert "secret prompt" not in response.error.message
    assert "database.sqlite3" not in response.error.message
    assert "Traceback" not in response.error.message


def test_coverage_keeps_numeric_identifiers_and_rankings_as_dimensions(tmp_path):
    llm = FakeLLM(
        plan(),
        "SELECT id AS project_id, 2026 AS year, 1 AS ranking, capacity_mw FROM stations WHERE id = 1",
    )

    response = asyncio.run(build_service(tmp_path, llm).query(request("查看项目排名")))

    assert response.success is True
    assert response.coverage.dimensions == ["project_id", "year", "ranking"]
    assert response.coverage.measures == ["capacity_mw"]


def test_unknown_result_alias_is_not_given_a_guessed_business_label(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS unexplained_value FROM stations")
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is True
    assert "semantic_label" not in response.data.schema_[0]


def test_published_route_never_bypasses_agent_planning(tmp_path):
    from app.catalog import RoutingDecision

    llm = FakeLLM(
        plan(),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
    )
    service = build_service(tmp_path, llm, diagnostics_enabled=True)
    service.catalog.routing_decision = lambda question: RoutingDecision(
        intent_id="route_station_capacity",
        action="allow",
        required_tables=("stations",),
    )

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert llm.plan_calls == 1
    assert response.diagnostics["plan"]["planning_mode"] == "llm"
    assert llm.generated_plans[0].original_question == request().question
    assert llm.generated_plans[0].required_outputs == ["张北县电站装机容量"]
    assert llm.generated_plans[0].table_hints == ["stations"]


def test_route_tables_do_not_override_agent_selected_tables(tmp_path):
    from app.catalog import RoutingDecision

    selected = plan().model_copy(update={"table_hints": ["stations"]})
    llm = FakeLLM(
        selected,
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
    )
    service = build_service(tmp_path, llm)
    route = RoutingDecision(
        intent_id="legacy_route",
        action="allow",
        required_tables=("unpublished_legacy_table",),
    )

    normalized = service._normalize_plan(selected, request().question, route)

    assert normalized.table_hints == ["stations"]


def test_production_query_does_not_call_offline_validation_routes(tmp_path):
    llm = FakeLLM(
        plan(),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
    )
    service = build_service(tmp_path, llm)
    service.diagnostics_enabled = True

    def offline_only(*_args, **_kwargs):
        raise AssertionError("生产查询不得调用离线题集路由或固定示例")

    service.catalog.routing_decision = offline_only
    service.catalog.concept_clarification = offline_only
    service.catalog.exact_example = offline_only

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert llm.plan_calls == 1
    assert "fallback" not in response.diagnostics


def test_query_events_stream_real_sanitized_stages_and_final_response(tmp_path):
    llm = FakeLLM(
        plan(),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
    )
    service = build_service(tmp_path, llm)
    service.catalog.set_managed_rules_provider(
        lambda: [
            {
                "id": "managed:station_capacity:v2",
                "scope_tables": ["stations"],
                "content": "不得进入事件的规则正文",
            }
        ]
    )

    async def collect():
        return [item async for item in service.query_events(request())]

    items = asyncio.run(collect())
    events = items[:-1]
    response = items[-1]

    assert response.success is True
    assert events
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.stage for event in events} >= {
        "routing",
        "planning",
        "sql_generation_initial",
        "sql_guard_initial",
        "pre_execution_review_1",
        "execution_primary",
        "result_review_1",
    }
    assert {event.status for event in events} >= {"started", "completed"}
    assert all(event.request_id == response.request_id for event in events)
    assert all(event.duration_ms is None or event.duration_ms >= 0 for event in events)
    completed_by_stage = {
        event.stage: event for event in events if event.status == "completed"
    }
    assert completed_by_stage["planning"].model == "fake-model"
    assert completed_by_stage["planning"].provider == "fake-provider"
    assert completed_by_stage["planning"].total_tokens == 12
    assert completed_by_stage["planning"].tool == "llm"
    assert completed_by_stage["sql_guard_initial"].tool == "sql_guard"
    assert completed_by_stage["sql_guard_initial"].model is None
    assert completed_by_stage["execution_primary"].tool == "sqlite_readonly"
    assert all(
        event.rule_versions == ["managed:station_capacity:v2"] for event in events
    )
    serialized = "\n".join(event.model_dump_json() for event in events)
    assert "SELECT" not in serialized
    assert "capacity_mw" not in serialized
    assert "prompt" not in serialized.casefold()
    assert "不得进入事件的规则正文" not in serialized


def test_unique_published_category_replans_instead_of_asking_user(tmp_path):
    clarification = plan().model_copy(
        update={
            "requires_clarification": True,
            "clarification_question": "请确认数据库中的分类项。",
        }
    )
    corrected = plan()
    llm = FakeLLM(
        [clarification, corrected],
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
    )
    service = build_service(tmp_path, llm)
    service.catalog.resolved_categorical_values = lambda question, tables: [
        {
            "table": "stations",
            "field": "county",
            "business_label": "区县",
            "value": "张北县",
        }
    ]

    response = asyncio.run(service.query(request()))

    assert response.success is True
    assert "不得再要求用户确认这些数据库内部枚举" in llm.planning_context


def test_concept_alternative_is_advisory_context_for_planner(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) FROM stations")
    response = asyncio.run(
        build_service(tmp_path, llm, concept_alternatives=True).query(
            request("张家口新能源领域各业主单位装机容量汇总")
        )
    )

    assert response.success is True
    assert response.error is None
    assert "归属上级集团" in llm.planning_context
    assert "项目建设方" in llm.planning_context
    assert "parent_group" not in llm.planning_context
    assert "project_builder" not in llm.planning_context


def test_filing_owner_query_is_not_blocked_by_operating_owner_clarification(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations")
    response = asyncio.run(
        build_service(tmp_path, llm, concept_alternatives=True).query(
            request("备案项目拟定业主装机规模")
        )
    )

    assert response.error is None
    assert llm.planning_context


def test_semantic_reviewer_only_gives_advice_and_generator_makes_revision(tmp_path):
    revision = "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'"
    llm = FakeLLM(
        plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
        pre_reviews=[
            PreExecutionReview(decision="revise", issues=[ReviewIssue(code="SCOPE_MISSING", message="缺少用户指定区县范围")], required_changes=["按用户指定区县筛选"]),
            PreExecutionReview(decision="pass"),
        ], generated=["SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations", revision],
    )
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 250.0}
    assert [call[1] for call in llm.calls if call[0] == "generate"] == ["initial", "semantic_revision"]
    feedback = [call for call in llm.calls if call[0] == "generate"][1][3]
    assert feedback["required_changes"] == ["按用户指定区县筛选"]


def test_guard_repair_and_semantic_revision_share_one_budget(tmp_path):
    llm = FakeLLM(
        plan(), "SELECT unknown_column FROM stations",
        pre_reviews=[PreExecutionReview(decision="revise", issues=[ReviewIssue(code="SHAPE", message="需要汇总")], required_changes=["返回汇总值"])],
        generated=["SELECT unknown_column FROM stations", "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations"],
    )
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is False
    assert response.error.code == "PRE_EXECUTION_REVIEW_EXHAUSTED"
    assert [call[1] for call in llm.calls if call[0] == "generate"] == ["initial", "guard_repair"]


def test_object_scope_violation_is_repaired_by_generator_before_execution(tmp_path):
    llm = FakeLLM(
        QueryPlan(query_type="aggregation", table_hints=["stations"]),
        "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '已运行电站'",
        generated=[
            "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '已运行电站'",
            "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
        ],
    )
    response = asyncio.run(
        build_service(tmp_path, llm, object_scope=True).query(
            request("已运行电站总装机是多少？")
        )
    )
    assert response.success is True
    assert response.data.summary == {"total_capacity_mw": 330.0}
    assert [call[1] for call in llm.calls if call[0] == "generate"] == [
        "initial",
        "guard_repair",
    ]


def test_result_requery_is_generated_by_sql_generator_and_returns_two_result_sets(tmp_path):
    llm = FakeLLM(
        plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
        result_reviews=[
            ResultReview(decision="requery", result_issues=[ReviewIssue(code="CATEGORY_MISSING", message="缺少区县分类")], required_changes=["按区县返回分类汇总"]),
            ResultReview(decision="answer", answer_limitations=["补查结果用于展示区县分类。"]),
        ],
        generated=[
            "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
            "SELECT county, SUM(capacity_mw) AS total_capacity_mw FROM stations GROUP BY county",
        ],
    )
    response = asyncio.run(build_service(tmp_path, llm).query(request("全市装机及区县分类")))
    assert response.success is True
    assert len(response.result_sets) == 2
    assert response.result_sets[1].data.rows == [{"county": "尚义县", "total_capacity_mw": 80.0}, {"county": "张北县", "total_capacity_mw": 250.0}]
    assert response.coverage.dimensions == ["county"]
    assert response.coverage.measures == ["total_capacity_mw"]
    assert [call[1] for call in llm.calls if call[0] == "generate"] == ["initial", "result_requery"]


def test_result_evidence_does_not_expose_sensitive_columns(tmp_path):
    llm = FakeLLM(plan(), "SELECT id, phone FROM stations")
    response = asyncio.run(build_service(tmp_path, llm).query(request("查看站点")))
    assert response.success is True
    evidence = [call[1] for call in llm.calls if call[0] == "result"][0]
    assert all(column["name"] != "phone" for column in evidence["columns"])
    assert "phone" not in json.dumps(evidence, ensure_ascii=False)


def test_pre_review_clarification_is_returned_without_execution(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations", pre_reviews=[PreExecutionReview(decision="clarification", clarification="请明确统计时间范围。")])
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is False
    assert response.error.code == "CLARIFICATION_REQUIRED"
    assert not [call for call in llm.calls if call[0] == "result"]


def test_clarification_followup_reaches_planner_with_bounded_context(tmp_path):
    llm = FakeLLM(
        plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations WHERE county = '张北县'",
        pre_reviews=[
            PreExecutionReview(decision="clarification", clarification="请明确区县。"),
            PreExecutionReview(decision="pass"),
        ],
    )
    service = build_service(tmp_path, llm)
    first = asyncio.run(service.query(request("查询电站装机容量")))
    second = asyncio.run(service.query(QueryRequest(question="张北县", session_id=first.session_id)))

    assert first.error.code == "CLARIFICATION_REQUIRED"
    assert first.session_id.startswith("ses_")
    assert second.success is True
    assert second.session_id == first.session_id
    assert llm.plan_questions == [
        "查询电站装机容量",
        "原问题：查询电站装机容量\n用户补充：张北县",
    ]


def test_result_data_quality_issue_has_dedicated_user_error_code(tmp_path):
    llm = FakeLLM(
        plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations",
        result_reviews=[ResultReview(decision="unsupported", result_issues=[ReviewIssue(code="DATA_QUALITY_LIMITATION", message="关键容量记录存在无法解析的值")])],
    )
    response = asyncio.run(build_service(tmp_path, llm).query(request()))
    assert response.success is False
    assert response.error.code == "DATA_QUALITY_LIMITATION"


def test_zhangjiakou_scope_is_structural_not_city_filter(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations")
    response = asyncio.run(build_service(tmp_path, llm).query(request("张家口全市装机多少")))
    assert response.success is True
    assert "不得添加地址 LIKE '%张家口%'" in llm.planning_context


def test_total_request_timeout_stops_before_any_model_or_database_work(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) AS total_capacity_mw FROM stations")
    service = build_service(tmp_path, llm)
    service.total_timeout_seconds = 0
    response = asyncio.run(service.query(request()))
    assert response.success is False
    assert response.error.code == "QUERY_TIMEOUT"
    assert llm.calls == []
