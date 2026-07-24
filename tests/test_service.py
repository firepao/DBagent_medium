import asyncio
import json
import sqlite3

from app.models import PreExecutionReview, QueryPlan, QueryRequest, ResultReview, ReviewIssue


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
        self.generated_plans = []
        self.planning_context = ""

    async def plan(self, question, planning_context):
        self.plan_calls += 1
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
    assert [call[0] for call in llm.calls] == ["generate", "pre", "result"]


def test_deterministic_route_skips_planner_but_keeps_query_plan(tmp_path):
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
    assert llm.plan_calls == 0
    assert response.diagnostics["plan"]["planning_mode"] == "deterministic_route"
    assert llm.generated_plans[0].original_question == request().question
    assert llm.generated_plans[0].required_outputs == [request().question]
    assert llm.generated_plans[0].table_hints == ["stations"]


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


def test_concept_clarification_stops_before_planning_and_hides_schema_names(tmp_path):
    llm = FakeLLM(plan(), "SELECT SUM(capacity_mw) FROM stations")
    response = asyncio.run(
        build_service(tmp_path, llm, concept_alternatives=True).query(
            request("张家口新能源领域各业主单位装机容量汇总")
        )
    )

    assert response.success is False
    assert response.error.code == "CLARIFICATION_REQUIRED"
    assert "归属上级集团" in response.error.message
    assert "项目建设方" in response.error.message
    assert "按归属上级集团汇总已运行集中式新能源电站装机容量" in response.error.message
    assert "parent_group" not in response.error.message
    assert "project_builder" not in response.error.message
    assert response.answer_guidance["response_mode"] == "clarification"
    assert llm.planning_context == ""


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
