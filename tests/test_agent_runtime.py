import asyncio
import json
import sqlite3

from app.agent.models import AgentAction
from app.agent.runtime import AgentRuntime
from app.agent.tools import AgentToolRegistry
from app.catalog import MetadataCatalog
from app.executor import SQLiteExecutor
from app.sql_guard import SqlGuard


def build_runtime_parts(tmp_path, controller, *, max_turns=8, max_sql_queries=3):
    db_path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE stations (id INTEGER, county TEXT, status TEXT, capacity_mw REAL)"
        )
        connection.executemany(
            "INSERT INTO stations VALUES (?, ?, ?, ?)",
            [
                (1, "张北县", "已运行", 100.0),
                (2, "张北县", "在建", 50.0),
                (3, "康保县", "已运行", 80.0),
            ],
        )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "datasets": [
                    {
                        "table": "stations",
                        "dataset": "能源场站",
                        "version": "test-v1",
                        "data_as_of": "2026-08-20",
                        "description": "测试场站数据",
                        "aliases": {
                            "county": "区县",
                            "status": "运行状态",
                            "capacity_mw": "装机容量",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    examples_path = tmp_path / "examples.json"
    examples_path.write_text("[]", encoding="utf-8")
    catalog = MetadataCatalog(db_path, catalog_path, examples_path)
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(db_path, timeout_seconds=2, max_rows=100)
    tools = AgentToolRegistry(catalog, guard, executor)
    return AgentRuntime(
        catalog,
        controller,
        tools,
        max_turns=max_turns,
        max_sql_queries=max_sql_queries,
    )


class RepairController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            assert "stations" in planning_context
            return AgentAction(
                tool_name="get_table_context",
                arguments={"table_hints": ["stations"]},
            )
        context_id = state.loaded_context_ids[0]
        if state.turns_used == 1:
            return AgentAction(
                tool_name="execute_readonly_query",
                arguments={
                    "context_id": context_id,
                    "sql": "SELECT county, SUM(missing_capacity) AS total_mw FROM stations GROUP BY county",
                },
            )
        if not state.evidence_ids:
            assert state.observations[-1].status == "revision_required"
            assert state.observations[-1].model_payload["error_code"] == "SQL_VALIDATION_FAILED"
            return AgentAction(
                tool_name="execute_readonly_query",
                arguments={
                    "context_id": context_id,
                    "sql": "SELECT county, SUM(capacity_mw) AS total_mw FROM stations GROUP BY county ORDER BY total_mw DESC",
                },
            )
        evidence_id = state.evidence_ids[-1]
        if evidence_id not in state.approved_evidence_ids:
            return AgentAction(
                tool_name="review_evidence",
                arguments={"evidence_ids": [evidence_id]},
            )
        return AgentAction(
            tool_name="finalize_answer",
            arguments={"evidence_ids": [evidence_id]},
        )


def test_agent_repairs_sql_from_real_guard_observation_and_finalizes(tmp_path):
    runtime = build_runtime_parts(tmp_path, RepairController())
    events = []

    response = asyncio.run(
        runtime.run("统计各区县装机容量", event_sink=events.append)
    )

    assert response.status == "completed"
    assert response.sql_queries_used == 2
    assert [item["tool"] for item in response.tool_trace] == [
        "get_table_context",
        "execute_readonly_query",
        "execute_readonly_query",
        "review_evidence",
        "finalize_answer",
    ]
    assert response.tool_trace[1]["status"] == "revision_required"
    query_events = [
        item for item in events if item["tool"] == "execute_readonly_query"
    ]
    assert [item["status"] for item in query_events] == ["failed", "completed"]
    assert query_events[0]["stage"] != query_events[1]["stage"]
    assert "继续修正" in query_events[0]["summary"]
    assert response.response is not None and response.response.success is True
    assert response.response.data.rows == [
        {"county": "张北县", "total_mw": 150.0},
        {"county": "康保县", "total_mw": 80.0},
    ]
    assert "来源：能源场站" in response.answer
    assert "2026-08-20" in response.answer


class PrematureFinalizeController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(
                tool_name="get_table_context",
                arguments={"table_hints": ["stations"]},
            )
        if not state.evidence_ids:
            return AgentAction(
                tool_name="execute_readonly_query",
                arguments={
                    "context_id": state.loaded_context_ids[0],
                    "sql": "SELECT COUNT(*) AS station_count FROM stations",
                },
            )
        evidence_id = state.evidence_ids[0]
        if not any(item.tool_name == "finalize_answer" for item in state.observations):
            return AgentAction(
                tool_name="finalize_answer",
                arguments={"evidence_ids": [evidence_id]},
            )
        if evidence_id not in state.approved_evidence_ids:
            return AgentAction(
                tool_name="review_evidence",
                arguments={"evidence_ids": [evidence_id]},
            )
        return AgentAction(
            tool_name="finalize_answer",
            arguments={"evidence_ids": [evidence_id]},
        )


def test_finalize_requires_approved_evidence(tmp_path):
    runtime = build_runtime_parts(tmp_path, PrematureFinalizeController())

    response = asyncio.run(runtime.run("场站总数是多少"))

    assert response.status == "completed"
    finalize_events = [
        item for item in response.tool_trace if item["tool"] == "finalize_answer"
    ]
    assert [item["status"] for item in finalize_events] == ["blocked", "ok"]
    assert response.response.data.rows == [{"station_count": 3}]


class ProfileAfterEmptyController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(
                tool_name="get_table_context",
                arguments={"table_hints": ["stations"]},
            )
        context_id = state.loaded_context_ids[0]
        if state.turns_used == 1:
            return AgentAction(
                tool_name="execute_readonly_query",
                arguments={
                    "context_id": context_id,
                    "sql": "SELECT county FROM stations WHERE status = '投运'",
                },
            )
        if not any(item.tool_name == "inspect_field_profile" for item in state.observations):
            assert state.observations[-1].status == "no_match"
            return AgentAction(
                tool_name="inspect_field_profile",
                arguments={"context_id": context_id, "table": "stations", "field": "status"},
            )
        if len(state.evidence_ids) == 1:
            profile = state.observations[-1].model_payload
            assert profile["allowed_values"] == ["在建", "已运行"]
            return AgentAction(
                tool_name="execute_readonly_query",
                arguments={
                    "context_id": context_id,
                    "sql": "SELECT COUNT(*) AS station_count FROM stations WHERE status = '已运行'",
                },
            )
        evidence_id = state.evidence_ids[-1]
        if evidence_id not in state.approved_evidence_ids:
            return AgentAction(
                tool_name="review_evidence", arguments={"evidence_ids": [evidence_id]}
            )
        return AgentAction(
            tool_name="finalize_answer", arguments={"evidence_ids": [evidence_id]}
        )


def test_empty_result_causes_profile_observation_and_enum_revision(tmp_path):
    runtime = build_runtime_parts(tmp_path, ProfileAfterEmptyController())

    response = asyncio.run(runtime.run("已运行场站有多少"))

    assert response.status == "completed"
    assert response.response.data.rows == [{"station_count": 2}]
    assert any(
        item["tool"] == "inspect_field_profile" and item["status"] == "ok"
        for item in response.tool_trace
    )


class UnsafeController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(
                tool_name="get_table_context",
                arguments={"table_hints": ["stations"]},
            )
        return AgentAction(
            tool_name="execute_readonly_query",
            arguments={
                "context_id": state.loaded_context_ids[0],
                "sql": "DELETE FROM stations",
            },
        )


def test_unsafe_sql_cannot_bypass_guard_and_duplicate_calls_are_blocked(tmp_path):
    runtime = build_runtime_parts(tmp_path, UnsafeController(), max_turns=3)

    response = asyncio.run(runtime.run("删除场站"))

    assert response.status == "failed"
    assert response.sql_queries_used == 1
    assert response.tool_trace[1]["status"] == "revision_required"
    assert response.tool_trace[2]["status"] == "blocked"
    with sqlite3.connect(runtime.tools.catalog.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 3


class SqlBudgetController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(
                tool_name="get_table_context",
                arguments={"table_hints": ["stations"]},
            )
        sql = (
            "SELECT missing FROM stations"
            if state.sql_queries_used == 0
            else "SELECT COUNT(*) AS station_count FROM stations"
        )
        return AgentAction(
            tool_name="execute_readonly_query",
            arguments={"context_id": state.loaded_context_ids[0], "sql": sql},
        )


def test_sql_query_budget_is_enforced_before_second_execution(tmp_path):
    runtime = build_runtime_parts(
        tmp_path, SqlBudgetController(), max_turns=5, max_sql_queries=1
    )

    response = asyncio.run(runtime.run("场站总数"))

    assert response.status == "failed"
    assert response.sql_queries_used == 1
    assert response.tool_trace[-1]["status"] == "blocked"
    assert "预算已耗尽" in response.tool_trace[-1]["summary"]


class MutatingController:
    async def decide(self, state, planning_context):
        state.original_question = "被修改的问题"
        return AgentAction(
            tool_name="get_table_context",
            arguments={"table_hints": ["stations"]},
        )


def test_original_question_is_immutable_across_agent_turns(tmp_path):
    runtime = build_runtime_parts(tmp_path, MutatingController())

    response = asyncio.run(runtime.run("原始问题"))

    assert response.status == "failed"
    assert response.error_code == "ORIGINAL_QUESTION_CHANGED"
    assert response.turns_used == 0
