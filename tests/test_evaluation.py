import asyncio
import json

import pytest

from app.evaluation import (
    EvaluationCase, EvaluationCaseResult, EvaluationRun, EvaluationRunner, EvaluationStore,
    GoldenValuesUpdate, BulkGoldenValuesUpdate, EvaluationRunRequest,
)
from app.models import ErrorInfo, QueryData, ToolResponse
from app.rule_store import RuleInput, RuleStore


class FakeService:
    async def query(self, request, evaluation_mode=False):
        if "补充" in request.question:
            return ToolResponse.failure(
                request_id="qry_clarify", code="CLARIFICATION_REQUIRED",
                message="请补充范围", retryable=False,
                diagnostics={"plan": {"table_hints": ["stations"]}},
            )
        if "不支持" in request.question:
            return ToolResponse.failure(
                request_id="qry_unsupported", code="QUERY_NOT_SUPPORTED",
                message="超出范围", retryable=False,
                diagnostics={"plan": {"table_hints": []}},
            )
        return ToolResponse(
            success=True, request_id="qry_success",
            data=QueryData(rows=[{"total": 2}], summary={"total": 2}),
            diagnostics={"plan": {"table_hints": ["stations"]}},
        )


class Catalog:
    allowed_tables = {"stations"}

    @staticmethod
    def allowed_columns(_table):
        return {"capacity_mw"}


def rule_payload():
    return RuleInput(
        rule_key="station_capacity", name="装机口径", description="按并网容量统计装机容量。",
        business_objects=["电站"], metric="装机容量", scope_tables=["stations"],
        required_fields={"stations": ["capacity_mw"]}, calculation="SUM(capacity_mw)", unit="MW",
    )


def test_evaluation_scores_behavior_tables_and_persists(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    runner = EvaluationRunner(FakeService(), store)
    cases = [
        EvaluationCase(id="ok", question="查询装机", expected_behavior="success", expected_tables=["stations"]),
        EvaluationCase(id="clarify", question="请补充范围", expected_behavior="clarification"),
        EvaluationCase(id="unsupported", question="不支持的问题", expected_behavior="unsupported"),
    ]
    run = asyncio.run(runner.run_cases(cases, target_type="baseline", target_id="baseline"))
    assert run.passed == 3
    assert run.value_accuracy is None
    assert store.get_run(run.id).pass_rate == 1
    assert run.p50_duration_ms >= 0
    assert run.p95_duration_ms >= run.p50_duration_ms


def test_value_accuracy_is_reported_only_for_golden_cases(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    runner = EvaluationRunner(FakeService(), store)
    cases = [EvaluationCase(
        id="golden", question="查询装机", expected_behavior="success",
        expected_tables=["stations"], expected_values={"total": 2},
    )]
    run = asyncio.run(runner.run_cases(cases, target_type="baseline", target_id="golden"))
    assert run.value_cases_total == 1
    assert run.value_cases_passed == 1
    assert run.value_accuracy == 1


def test_readiness_blocks_release_until_goldens_and_complete_run_exist(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    case = EvaluationCase(id="Q1", question="查询装机", expected_behavior="success")
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO evaluation_cases VALUES (?, ?)",
            (case.id, case.model_dump_json()),
        )

    readiness = store.readiness()

    assert readiness.ready_for_release is False
    assert readiness.golden_cases == 0
    assert readiness.latest_run_id is None
    assert any("黄金值" in reason for reason in readiness.blocking_reasons)


def test_evaluation_aggregates_only_real_complete_run_event_usage(tmp_path):
    from app.run_events import RunEvent, RunEventStore
    from app.platform_migrations import migrate_platform_database

    path = tmp_path / "platform.sqlite3"
    migrate_platform_database(path)
    store = EvaluationStore(path)
    event_store = RunEventStore(path)

    class UsageService(FakeService):
        def __init__(self):
            self.event_store = event_store

        async def query(self, request, evaluation_mode=False):
            response = await super().query(request, evaluation_mode)
            event_store.append(RunEvent.create(
                request_id=response.request_id, sequence=1, stage="planning",
                status="completed", summary="完成", tool="llm", total_tokens=10,
            ))
            event_store.append(RunEvent.create(
                request_id=response.request_id, sequence=2, stage="execution_primary",
                status="completed", summary="完成", tool="sqlite_readonly",
            ))
            return response

    run = asyncio.run(EvaluationRunner(UsageService(), store).run_cases(
        [EvaluationCase(id="usage", question="查询装机", expected_behavior="success")],
        target_type="baseline", target_id="usage",
    ))
    assert run.model_calls == 1
    assert run.total_tokens == 10
    assert run.results[0].model_calls == 1
    assert run.results[0].total_tokens == 10
    assert store.get_run(run.id).total_tokens == 10


def test_evaluation_does_not_estimate_missing_usage(tmp_path):
    from app.run_events import RunEvent, RunEventStore
    from app.platform_migrations import migrate_platform_database

    path = tmp_path / "platform.sqlite3"
    migrate_platform_database(path)
    store = EvaluationStore(path)
    event_store = RunEventStore(path)

    class MissingUsageService(FakeService):
        def __init__(self): self.event_store = event_store
        async def query(self, request, evaluation_mode=False):
            response = await super().query(request, evaluation_mode)
            event_store.append(RunEvent.create(
                request_id=response.request_id, sequence=1, stage="planning",
                status="completed", summary="完成", tool="llm",
            ))
            return response

    run = asyncio.run(EvaluationRunner(MissingUsageService(), store).run_cases(
        [EvaluationCase(id="missing", question="查询装机", expected_behavior="success")],
        target_type="baseline", target_id="missing",
    ))
    assert run.model_calls == 1
    assert run.total_tokens is None


def test_golden_values_are_audited_and_survive_case_reimport(tmp_path):
    import json
    import sqlite3

    store = EvaluationStore(tmp_path / "platform.sqlite3")
    source = tmp_path / "cases.json"
    source.write_text(json.dumps({"cases": [{
        "id": "Q1", "question": "查询装机", "status": "supported",
        "scope_tables": ["stations"],
    }]}), encoding="utf-8")
    store.import_validation_cases(source)
    store.update_golden_values("Q1", GoldenValuesUpdate(expected_values={"total": 2}))
    store.import_validation_cases(source)
    assert store.list_cases()[0].expected_values == {"total": 2}
    with sqlite3.connect(store.path) as connection:
        audit = connection.execute(
            "SELECT before_json, after_json FROM evaluation_case_audit WHERE case_id = 'Q1'"
        ).fetchone()
    assert json.loads(audit[0]) == {}
    assert json.loads(audit[1]) == {"total": 2}


def test_bulk_golden_values_are_atomic_and_audited(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    source = tmp_path / "cases.json"
    source.write_text(json.dumps({"cases": [
        {"id": "Q1", "question": "一", "status": "supported"},
        {"id": "Q2", "question": "二", "status": "supported"},
    ]}), encoding="utf-8")
    store.import_validation_cases(source)
    store.update_golden_values_bulk(BulkGoldenValuesUpdate(cases={
        "Q1": {"expected_values": {"total": 1}},
        "Q2": {"expected_values": {"total": 2}},
    }))
    assert {case.id: case.expected_values for case in store.list_cases()} == {
        "Q1": {"total": 1}, "Q2": {"total": 2}
    }
    with pytest.raises(KeyError):
        store.update_golden_values_bulk(BulkGoldenValuesUpdate(cases={
            "Q1": {"expected_values": {"total": 3}}, "missing": {"expected_values": {}}
        }))
    assert store.list_cases()[0].expected_values == {"total": 1}


def test_candidate_rule_is_scoped_and_failed_gate_blocks_publish(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog(), require_evaluation=True)
    draft = store.create_draft(rule_payload())
    assert store.published_rules() == []
    try:
        store.publish(draft.id)
    except ValueError as exc:
        assert "评测" in str(exc)
    else:
        raise AssertionError("未经评测的规则不应发布")
    store.record_evaluation_gate(draft.id, "eval_1", True, 1.0)
    assert store.publish(draft.id).status == "published"


def test_latest_failed_gate_overrides_an_earlier_pass(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog(), require_evaluation=True)
    draft = store.create_draft(rule_payload())
    store.record_evaluation_gate(draft.id, "eval_passed", True, 1.0)
    store.record_evaluation_gate(draft.id, "eval_failed", False, 0.75)

    with pytest.raises(ValueError, match="尚未通过沙箱评测"):
        store.publish(draft.id)
    latest = store.evaluation_gates()
    assert len(latest) == 1
    assert latest[0].evaluation_run_id == "eval_failed"
    assert latest[0].passed is False


def test_compare_runs_reports_fixed_and_regressed_cases(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")

    def result(case_id, passed, duration):
        return EvaluationCaseResult(
            case_id=case_id, passed=passed, behavior_passed=passed,
            tables_passed=True, values_passed=True, actual_behavior="success",
            duration_ms=duration, request_id=f"qry_{case_id}",
        )

    store.save_run(EvaluationRun(
        id="baseline", target_type="baseline", target_id="v1", status="completed",
        total=2, passed=1, pass_rate=.5, started_at="2026-01-01T00:00:00Z",
        p50_duration_ms=100, p95_duration_ms=120, model_calls=8, total_tokens=1000,
        results=[result("fixed", False, 100), result("regressed", True, 100)],
    ))
    store.save_run(EvaluationRun(
        id="candidate", target_type="baseline", target_id="v2", status="completed",
        total=2, passed=1, pass_rate=.5, started_at="2026-01-02T00:00:00Z",
        p50_duration_ms=80, p95_duration_ms=90, model_calls=6, total_tokens=700,
        results=[result("fixed", True, 80), result("regressed", False, 80)],
    ))

    comparison = store.compare_runs("baseline", "candidate")

    assert comparison.fixed == 1
    assert comparison.regressed == 1
    assert comparison.pass_rate_delta == 0
    assert comparison.average_duration_delta_ms == -20
    assert comparison.p50_duration_delta_ms == -20
    assert comparison.p95_duration_delta_ms == -30
    assert comparison.model_calls_delta == -2
    assert comparison.total_tokens_delta == -300


def test_evaluation_percentiles_use_deterministic_nearest_rank():
    assert EvaluationRunner._percentile([10, 20, 30, 40], 0.50) == 20
    assert EvaluationRunner._percentile([10, 20, 30, 40], 0.95) == 40
    assert EvaluationRunner._percentile([], 0.95) == 0


def test_evaluation_case_subset_preserves_request_order_and_validates_ids(tmp_path):
    store = EvaluationStore(tmp_path / "platform.sqlite3")
    source = tmp_path / "cases.json"
    source.write_text(json.dumps({"cases": [
        {"id": "Q1", "question": "一", "status": "supported"},
        {"id": "Q2", "question": "二", "status": "supported"},
    ]}), encoding="utf-8")
    store.import_validation_cases(source)
    assert [case.id for case in store.select_cases(["Q2", "Q1"])] == ["Q2", "Q1"]
    with pytest.raises(KeyError):
        store.select_cases(["missing"])
    with pytest.raises(ValueError):
        EvaluationRunRequest(case_ids=["Q1", "Q1"])
