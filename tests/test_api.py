import asyncio
import importlib

import httpx
import pytest

from app.models import QueryData, SourceInfo, ToolResponse
from app.catalog import CatalogError


def create_app(service):
    try:
        return importlib.import_module("app.main").create_app(service)
    except ModuleNotFoundError:
        pytest.fail("app.main 尚未实现")


class StubService:
    async def query(self, request):
        return ToolResponse(
            success=True,
            request_id="qry_test",
            data=QueryData(rows=[{"total": 2}], summary={"total": 2}),
            sources=[SourceInfo(dataset="测试数据", version="v1")],
        )

    def health(self):
        return {
            "status": "healthy",
            "checks": {"database": "healthy", "llm": "configured"},
            "conversation": {
                "backend": "memory",
                "multi_replica_supported": False,
            },
            "management_auth": {
                "configured": False,
                "admin_configured": False,
                "viewer_configured": False,
                "mode": "development_open",
                "deployment_mode": "development",
                "ready": True,
            },
        }


class RuleCatalog:
    allowed_tables = {"stations"}

    @staticmethod
    def allowed_columns(_table):
        return {"capacity_mw", "county"}

    @staticmethod
    def dataset(table):
        return {"dataset": "测试电站", "table": table}

    @staticmethod
    def runtime_rule_summaries():
        return [{
            "id": "station_capacity", "name": "装机容量口径",
            "scope_tables": ["stations"], "content": "按并网容量统计。",
            "source": "system_config",
        }]


async def request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_query_endpoint_returns_tool_response() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data",
            json={
                "question": "查询项目数量",
            },
        )
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["summary"] == {"total": 2}
    assert response.json()["data"]["schema"] == []
    assert "diagnostics" not in response.json()


def test_query_endpoint_returns_stable_validation_error() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data",
            json={"question": "   "},
        )
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"] == {
        "code": "INVALID_ARGUMENT",
        "message": "请求参数不合法",
        "retryable": False,
    }


def test_health_endpoint_reports_component_state() -> None:
    response = asyncio.run(
        request(create_app(StubService()), "GET", "/health")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["checks"]["database"] == "healthy"
    assert response.json()["conversation"]["multi_replica_supported"] is False


def test_liveness_and_readiness_are_distinct() -> None:
    healthy_app = create_app(StubService())
    live = asyncio.run(request(healthy_app, "GET", "/live"))
    ready = asyncio.run(request(healthy_app, "GET", "/ready"))

    class DegradedService(StubService):
        def health(self):
            return {"status": "degraded", "checks": {"database": "unhealthy"}}

    degraded = asyncio.run(request(create_app(DegradedService()), "GET", "/ready"))

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert degraded.status_code == 503


def test_production_without_admin_key_is_not_ready():
    class ProductionService(StubService):
        admin_api_key = ""
        viewer_api_key = ""
        deployment_mode = "production"

        def health(self):
            return {
                "status": "degraded",
                "checks": {"database": "healthy", "llm": "configured"},
                "management_auth": {
                    "configured": False,
                    "admin_configured": False,
                    "viewer_configured": False,
                    "mode": "development_open",
                    "deployment_mode": "production",
                    "ready": False,
                },
            }

    ready = asyncio.run(request(create_app(ProductionService()), "GET", "/ready"))

    assert ready.status_code == 503
    assert ready.json()["management_auth"]["ready"] is False


def test_query_workbench_is_served_and_root_redirects() -> None:
    app = create_app(StubService())
    root = asyncio.run(request(app, "GET", "/", follow_redirects=False))
    page = asyncio.run(request(app, "GET", "/app"))
    script = asyncio.run(request(app, "GET", "/assets/app.js"))

    assert root.status_code in {302, 307}
    assert root.headers["location"] == "/app"
    assert page.status_code == 200
    assert "能源数据智能查询" in page.text
    assert "/api/v1/query-energy-data/events" in script.text
    assert "event.total_tokens" in script.text
    assert "event.rule_versions" in script.text
    assert "event.provider" in script.text
    assert "column.unit" in script.text
    assert "displayValue" in script.text
    assert "模型仍在处理，复杂问题可能需要更久" in script.text
    assert "finally{queryStartedAt=0;send.disabled=false" in script.text
    assert "queryStartedAt=Date.now()" in script.text
    assert "RESULT_TRUNCATED:'结果超过展示上限" in script.text
    assert ".map(warningText)" in script.text
    assert "查询口径" in script.text
    assert "coverage.dimensions" in script.text
    assert "coverage.measures" in script.text


def test_evaluation_workbench_consumes_case_diagnostics_and_comparison_changes() -> None:
    app = create_app(StubService())
    page = asyncio.run(request(app, "GET", "/evaluations"))
    script = asyncio.run(request(app, "GET", "/assets/eval-details.js"))
    main_script = asyncio.run(request(app, "GET", "/assets/evals.js"))

    assert page.status_code == 200
    assert "runDetailDialog" in page.text
    assert "item.behavior_passed" in script.text
    assert "item.tables_passed" in script.text
    assert "item.values_passed" in script.text
    assert "result.changes.filter" in script.text
    assert "运行指定题目" in page.text
    assert "case_ids" in main_script.text
    assert "selectedCaseIds" in main_script.text
    assert "并产生模型调用，是否继续" in main_script.text


def test_rule_workbench_collects_required_fields_from_catalog() -> None:
    app = create_app(StubService())
    page = asyncio.run(request(app, "GET", "/rules"))
    script = asyncio.run(request(app, "GET", "/assets/rule-fields.js"))
    rules_script = asyncio.run(request(app, "GET", "/assets/rules.js"))

    assert page.status_code == 200
    assert "requiredFieldOptions" in page.text
    assert "data-required-table" in script.text
    assert "required_fields:requiredFields" in script.text
    assert "请至少选择一个依赖字段" in script.text
    assert "当前生效规则" in page.text
    assert "/api/v1/rules/runtime" in rules_script.text


def test_runtime_rules_api_exposes_read_only_effective_rule_summaries() -> None:
    service = StubService()
    service.catalog = RuleCatalog()
    response = asyncio.run(request(create_app(service), "GET", "/api/v1/rules/runtime"))

    assert response.status_code == 200
    assert response.json() == [{
        "id": "station_capacity", "name": "装机容量口径",
        "scope_tables": ["stations"], "content": "按并网容量统计。",
        "source": "system_config",
    }]


def test_sse_endpoint_returns_final_tool_response() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data/events",
            json={"question": "查询项目数量"},
        )
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: result" in response.text
    assert '"request_id":"qry_test"' in response.text


def test_sse_endpoint_preserves_sanitized_run_metadata() -> None:
    from app.run_events import RunEvent

    class StreamingService(StubService):
        async def query_events(self, _request):
            yield RunEvent.create(
                request_id="qry_stream",
                sequence=1,
                stage="planning",
                status="completed",
                summary="规划完成",
                model="model-1",
                provider="primary",
                tool="llm",
                input_tokens=12,
                output_tokens=3,
                total_tokens=15,
                rule_versions=["managed:capacity:v2"],
            )
            yield ToolResponse(
                success=True,
                request_id="qry_stream",
                data=QueryData(rows=[{"total": 2}], summary={"total": 2}),
            )

    response = asyncio.run(request(
        create_app(StreamingService()),
        "POST",
        "/api/v1/query-energy-data/events",
        json={"question": "查询项目数量"},
    ))

    assert "event: progress" in response.text
    assert '"model":"model-1"' in response.text
    assert '"total_tokens":15' in response.text
    assert '"rule_versions":["managed:capacity:v2"]' in response.text
    assert "api_key" not in response.text


def test_rule_api_draft_validate_and_publish(tmp_path) -> None:
    from app.rule_store import RuleStore

    service = StubService()
    service.catalog = RuleCatalog()
    service.rule_store = RuleStore(tmp_path / "platform.sqlite3", service.catalog)
    app = create_app(service)
    payload = {
        "rule_key": "station_capacity",
        "name": "电站装机容量口径",
        "description": "按并网容量统计已运行电站装机容量。",
        "business_objects": ["已运行电站"],
        "metric": "装机容量",
        "dimensions": ["区县"],
        "scope_tables": ["stations"],
        "required_fields": {"stations": ["capacity_mw"]},
        "calculation": "装机容量合计 = SUM(capacity_mw)",
        "unit": "MW",
    }

    created = asyncio.run(request(app, "POST", "/api/v1/rules", json=payload))
    rule_id = created.json()["id"]
    validation = asyncio.run(request(app, "POST", f"/api/v1/rules/{rule_id}/validate"))
    published = asyncio.run(request(app, "POST", f"/api/v1/rules/{rule_id}/publish"))
    catalog = asyncio.run(request(app, "GET", "/api/v1/rules/catalog"))

    assert created.status_code == 201
    assert validation.json()["valid"] is True
    assert published.json()["status"] == "published"
    assert catalog.json()["tables"][0]["dataset"] == "测试电站"


def test_rule_detail_diff_and_audit_apis(tmp_path) -> None:
    from app.rule_store import RuleInput, RuleStore

    service = StubService()
    service.catalog = RuleCatalog()
    service.rule_store = RuleStore(tmp_path / "platform.sqlite3", service.catalog)
    base = RuleInput(
        rule_key="station_capacity", name="电站装机容量口径",
        description="按并网容量统计已运行电站装机容量。",
        business_objects=["已运行电站"], metric="装机容量", dimensions=["区县"],
        scope_tables=["stations"], required_fields={"stations": ["capacity_mw"]},
        calculation="装机容量合计 = SUM(capacity_mw)", unit="MW",
    )
    first = service.rule_store.publish(service.rule_store.create_draft(base).id)
    second = service.rule_store.create_draft(
        base.model_copy(update={"description": "改为按已确认并网容量统计。"})
    )
    app = create_app(service)

    detail = asyncio.run(request(app, "GET", f"/api/v1/rules/{second.id}"))
    diff = asyncio.run(request(app, "GET", f"/api/v1/rules/{second.id}/diff"))
    audit = asyncio.run(request(app, "GET", f"/api/v1/rules/{first.id}/audit"))
    static_gate = asyncio.run(request(app, "GET", "/api/v1/rules/evaluation-gates"))

    assert detail.json()["version"] == 2
    assert diff.json()["from_version"] == 1
    assert diff.json()["changes"][0]["field"] == "description"
    assert [item["action"] for item in audit.json()] == ["draft_created", "published"]
    assert static_gate.status_code == 200


def test_management_api_can_require_admin_key() -> None:
    service = StubService()
    service.admin_api_key = "admin-secret"
    app = create_app(service)

    denied = asyncio.run(request(app, "GET", "/api/v1/rules"))
    allowed = asyncio.run(
        request(app, "GET", "/api/v1/rules", headers={"X-Admin-Key": "admin-secret"})
    )
    public_query = asyncio.run(
        request(app, "POST", "/api/v1/query-energy-data", json={"question": "查询项目数量"})
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert public_query.status_code == 200


def test_evaluation_readiness_api_reports_blockers(tmp_path) -> None:
    from app.evaluation import EvaluationCase, EvaluationStore

    service = StubService()
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    case = EvaluationCase(id="Q1", question="查询装机", expected_behavior="success")
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO evaluation_cases VALUES (?, ?)",
            (case.id, case.model_dump_json()),
        )
    service.evaluation_store = store

    response = asyncio.run(
        request(create_app(service), "GET", "/api/v1/evaluations/readiness")
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_cases"] == 1
    assert payload["ready_for_release"] is False
    assert payload["blocking_reasons"]


def test_viewer_key_is_read_only_for_management_apis() -> None:
    service = StubService()
    service.admin_api_key = "admin-secret"
    service.viewer_api_key = "viewer-secret"
    app = create_app(service)
    listed = asyncio.run(request(
        app, "GET", "/api/v1/rules", headers={"X-Admin-Key": "viewer-secret"}
    ))
    denied = asyncio.run(request(
        app, "POST", "/api/v1/evaluations/run",
        headers={"X-Admin-Key": "viewer-secret"},
        json={"target_type": "baseline", "target_id": "baseline"},
    ))
    admin_denied = asyncio.run(request(app, "GET", "/api/v1/rules", headers={"X-Admin-Key": "bad"}))

    assert listed.status_code == 200
    assert denied.status_code == 403
    assert admin_denied.status_code == 401


def test_evaluation_compare_api_returns_deterministic_diff(tmp_path) -> None:
    from app.evaluation import EvaluationCaseResult, EvaluationRun, EvaluationStore

    service = StubService()
    service.evaluation_store = EvaluationStore(tmp_path / "platform.sqlite3")
    make_result = lambda case_id, passed: EvaluationCaseResult(
        case_id=case_id, passed=passed, behavior_passed=passed,
        tables_passed=True, values_passed=True, actual_behavior="success",
        duration_ms=10, request_id=f"qry_{case_id}",
    )
    service.evaluation_store.save_run(EvaluationRun(
        id="run_a", target_type="baseline", target_id="a", status="completed",
        total=1, passed=0, pass_rate=0, started_at="2026-01-01", results=[make_result("Q1", False)],
    ))
    service.evaluation_store.save_run(EvaluationRun(
        id="run_b", target_type="baseline", target_id="b", status="completed",
        total=1, passed=1, pass_rate=1, started_at="2026-01-02", results=[make_result("Q1", True)],
    ))
    app = create_app(service)
    response = asyncio.run(request(app, "GET", "/api/v1/evaluations/compare?baseline_run_id=run_a&candidate_run_id=run_b"))
    assert response.status_code == 200
    assert response.json()["fixed"] == 1
    assert response.json()["p50_duration_delta_ms"] == 0
    assert response.json()["total_tokens_delta"] is None


def test_unknown_evaluation_case_is_rejected_before_runner_call(tmp_path) -> None:
    from app.evaluation import EvaluationCase, EvaluationStore

    class CountingRunner:
        calls = 0

        async def run_cases(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("未知题号不应进入评测 Runner")

    service = StubService()
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    with store._connect() as connection:
        case = EvaluationCase(id="Q1", question="查询装机", expected_behavior="success")
        connection.execute("INSERT INTO evaluation_cases VALUES (?, ?)", (case.id, case.model_dump_json()))
    runner = CountingRunner()
    service.evaluation_store = store
    service.evaluation_runner = runner
    app = create_app(service)
    response = asyncio.run(request(
        app, "POST", "/api/v1/evaluations/run",
        json={"target_type": "baseline", "target_id": "selected", "case_ids": ["missing"]},
    ))
    assert response.status_code == 404
    assert runner.calls == 0


def test_golden_values_api_enforces_role_persists_and_audits(tmp_path) -> None:
    import sqlite3
    from app.evaluation import EvaluationCase, EvaluationStore

    service = StubService()
    service.admin_api_key = "admin-secret"
    service.viewer_api_key = "viewer-secret"
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    with store._connect() as connection:
        case = EvaluationCase(
            id="Q1", question="查询装机", expected_behavior="success",
            expected_tables=["stations"],
        )
        connection.execute(
            "INSERT INTO evaluation_cases VALUES (?, ?)", (case.id, case.model_dump_json())
        )
    service.evaluation_store = store
    app = create_app(service)

    viewer = asyncio.run(request(
        app, "PATCH", "/api/v1/evaluations/cases/Q1/golden-values",
        headers={"X-Admin-Key": "viewer-secret"}, json={"expected_values": {"total": 2}},
    ))
    admin = asyncio.run(request(
        app, "PATCH", "/api/v1/evaluations/cases/Q1/golden-values",
        headers={"X-Admin-Key": "admin-secret"}, json={"expected_values": {"total": 2}},
    ))
    listed = asyncio.run(request(
        app, "GET", "/api/v1/evaluations/cases",
        headers={"X-Admin-Key": "viewer-secret"},
    ))
    missing = asyncio.run(request(
        app, "PATCH", "/api/v1/evaluations/cases/missing/golden-values",
        headers={"X-Admin-Key": "admin-secret"}, json={"expected_values": {}},
    ))
    with sqlite3.connect(store.path) as connection:
        audit_count = connection.execute("SELECT COUNT(*) FROM evaluation_case_audit").fetchone()[0]

    assert viewer.status_code == 403
    assert admin.status_code == 200
    assert admin.json()["expected_values"] == {"total": 2}
    assert listed.json()[0]["expected_values"] == {"total": 2}
    assert missing.status_code == 404
    assert audit_count == 1


def test_bulk_golden_values_api_updates_all_cases_atomically(tmp_path) -> None:
    from app.evaluation import EvaluationCase, EvaluationStore

    service = StubService()
    service.admin_api_key = "admin-secret"
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    with store._connect() as connection:
        for case_id in ("Q1", "Q2"):
            case = EvaluationCase(id=case_id, question=case_id, expected_behavior="success")
            connection.execute("INSERT INTO evaluation_cases VALUES (?, ?)", (case_id, case.model_dump_json()))
    service.evaluation_store = store
    app = create_app(service)
    response = asyncio.run(request(
        app, "PATCH", "/api/v1/evaluations/cases/golden-values",
        headers={"X-Admin-Key": "admin-secret"},
        json={"cases": {"Q1": {"expected_values": {"total": 1}}, "Q2": {"expected_values": {"total": 2}}}},
    ))
    assert response.status_code == 200
    assert {item["id"]: item["expected_values"] for item in response.json()} == {
        "Q1": {"total": 1}, "Q2": {"total": 2}
    }


def test_query_run_replay_is_protected_and_returns_events(tmp_path) -> None:
    from app.platform_migrations import migrate_platform_database
    from app.run_events import RunEvent, RunEventStore

    path = tmp_path / "platform.sqlite3"
    migrate_platform_database(path)
    service = StubService()
    service.admin_api_key = "admin-secret"
    service.event_store = RunEventStore(path)
    service.event_store.append(RunEvent.create(
        request_id="qry_replay", sequence=1, stage="planning",
        status="completed", summary="规划完成", model="model-1",
        provider="primary", tool="llm", total_tokens=15,
        rule_versions=["managed:capacity:v2"],
    ))
    app = create_app(service)
    denied = asyncio.run(request(app, "GET", "/api/v1/query-runs/qry_replay/events"))
    replay = asyncio.run(request(
        app, "GET", "/api/v1/query-runs/qry_replay/events",
        headers={"X-Admin-Key": "admin-secret"},
    ))
    missing = asyncio.run(request(
        app, "GET", "/api/v1/query-runs/qry_missing/events",
        headers={"X-Admin-Key": "admin-secret"},
    ))
    assert denied.status_code == 401
    assert replay.status_code == 200
    assert replay.json()[0]["stage"] == "planning"
    assert replay.json()[0]["model"] == "model-1"
    assert replay.json()[0]["total_tokens"] == 15
    assert replay.json()[0]["rule_versions"] == ["managed:capacity:v2"]
    assert missing.status_code == 404


def test_default_service_validation_rejects_invalid_table_cards() -> None:
    main = importlib.import_module("app.main")

    class InvalidCatalog:
        def table_card_issues(self):
            return ["t04_filing_project.aliases.备案日期 引用未发布字段 filing_date"]

        def runtime_rule_issues(self):
            return []

    with pytest.raises(CatalogError, match="TableCard 配置无效"):
        main.ensure_valid_table_cards(InvalidCatalog())
