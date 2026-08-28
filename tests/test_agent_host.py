import asyncio
import pathlib
import sqlite3
from datetime import UTC, datetime, timedelta

from app.agent.models import AgentAction
from app.agent.runtime import AgentRuntime
from app.agent.session_runtime import SessionAgentRuntime
from app.agent.session_store import AgentSessionStore, SessionConflict
from app.agent.tools import AgentToolRegistry
from app.platform_migrations import migrate_platform_database
from tests.test_agent_runtime import build_runtime_parts


class SuccessfulController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(tool_name="get_table_context", arguments={"table_hints": ["stations"]})
        if not state.evidence_ids:
            return AgentAction(tool_name="execute_readonly_query", arguments={"context_id": state.loaded_context_ids[0], "sql": "SELECT COUNT(*) AS station_count FROM stations"})
        if not state.approved_evidence_ids:
            return AgentAction(tool_name="review_evidence", arguments={"evidence_ids": [state.evidence_ids[-1]]})
        return AgentAction(tool_name="finalize_answer", arguments={"evidence_ids": [state.evidence_ids[-1]]})


def test_session_store_and_host_persist_transcript(tmp_path):
    runtime = build_runtime_parts(tmp_path, SuccessfulController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    host = SessionAgentRuntime(runtime, AgentSessionStore(database))
    session = host.create_session()

    async def run():
        first = await host.start_run(session.session_id, "统计场站数", client_message_id="cm_1")
        duplicate = await host.start_run(session.session_id, "统计场站数", client_message_id="cm_1")
        return first, duplicate

    first, duplicate = asyncio.run(run())
    assert first.status == "completed"
    assert duplicate.run_id == first.run_id
    messages = host.list_messages(session.session_id)
    assert [message.role for message in messages].count("tool") >= 4
    assert messages[-1].role == "assistant"
    assert host.list_events(first.run_id)[-1].event_type == "run_end"


def test_active_session_rejects_second_run(tmp_path):
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    store = AgentSessionStore(database)
    session = store.create_session()
    store.create_run(session.session_id)
    try:
        store.create_run(session.session_id)
    except SessionConflict:
        pass
    else:
        raise AssertionError("active session must reject a second run")


class ClarifyThenContinueController:
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            return AgentAction(tool_name="get_table_context", arguments={"table_hints": ["stations"]})
        if state.turns_used == 1:
            return AgentAction(tool_name="ask_user_question", arguments={"question": "请补充统计口径。"})
        assert any(message.get("content") == "按项目建设方统计" for message in state.messages)
        if not state.evidence_ids:
            return AgentAction(tool_name="execute_readonly_query", arguments={"context_id": state.loaded_context_ids[0], "sql": "SELECT COUNT(*) AS station_count FROM stations"})
        if not state.approved_evidence_ids:
            return AgentAction(tool_name="review_evidence", arguments={"evidence_ids": [state.evidence_ids[-1]]})
        return AgentAction(tool_name="finalize_answer", arguments={"evidence_ids": [state.evidence_ids[-1]]})


def test_checkpoint_restores_tool_state_after_process_restart(tmp_path):
    first_runtime = build_runtime_parts(tmp_path, ClarifyThenContinueController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    session = SessionAgentRuntime(first_runtime, AgentSessionStore(database)).create_session()
    first_host = SessionAgentRuntime(first_runtime, AgentSessionStore(database))
    paused = asyncio.run(first_host.start_run(session.session_id, "统计场站数"))
    assert paused.status == "waiting_user"
    run_before = first_host.get_run(paused.run_id)
    assert run_before.sql_count == 0

    # New registry and host intentionally have no in-memory contexts/evidence.
    restarted_runtime = AgentRuntime(
        first_runtime.catalog,
        ClarifyThenContinueController(),
        AgentToolRegistry(first_runtime.catalog, first_runtime.tools.guard, first_runtime.tools.executor),
    )
    restarted_host = SessionAgentRuntime(restarted_runtime, AgentSessionStore(database))
    result = asyncio.run(restarted_host.resume_run(paused.run_id, "按项目建设方统计"))
    assert result.status == "completed"
    assert result.sql_queries_used == 1
    messages = restarted_host.list_messages(session.session_id)
    assert any(message.content == "按项目建设方统计" for message in messages)


def test_event_replay_and_expired_clarification(tmp_path):
    runtime = build_runtime_parts(tmp_path, ClarifyThenContinueController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    host = SessionAgentRuntime(runtime, AgentSessionStore(database))
    session = host.create_session()
    paused = asyncio.run(host.start_run(session.session_id, "统计场站数"))
    events = host.list_events(paused.run_id)
    assert events
    assert host.list_events(paused.run_id, after_sequence=events[0].sequence) == events[1:]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE agent_sessions SET expires_at=? WHERE session_id=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session.session_id))
    try:
        asyncio.run(host.resume_run(paused.run_id, "补充条件"))
    except ValueError as exc:
        assert str(exc) == "CLARIFICATION_EXPIRED"
    else:
        raise AssertionError("expired clarification must not resume")
    assert host.get_run(paused.run_id).error_code == "CLARIFICATION_EXPIRED"


def test_start_run_creates_user_message_and_run_atomically_and_persists_evidence(tmp_path):
    runtime = build_runtime_parts(tmp_path, SuccessfulController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    host = SessionAgentRuntime(runtime, AgentSessionStore(database))
    session = host.create_session()

    result = asyncio.run(
        host.start_run(session.session_id, "统计场站数", client_message_id="cm_atomic")
    )

    messages = host.list_messages(session.session_id)
    first = messages[0]
    final = messages[-1]
    assert first.role == "user"
    assert first.run_id == result.run_id
    assert first.metadata["client_message_id"] == "cm_atomic"
    assert final.role == "assistant"
    assert final.metadata["evidence_ids"]


def test_new_host_marks_interrupted_run_paused_and_can_resume(tmp_path):
    runtime = build_runtime_parts(tmp_path, ClarifyThenContinueController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    store = AgentSessionStore(database)
    session = store.create_session()
    interrupted = store.create_run(session.session_id)
    store.append_message(
        session_id=session.session_id,
        run_id=interrupted.run_id,
        role="user",
        content="统计场站数",
    )

    # Constructing a new host simulates a fresh process seeing an in-flight run.
    host = SessionAgentRuntime(runtime, AgentSessionStore(database))
    paused = host.get_run(interrupted.run_id)
    assert paused.status == "paused"
    assert paused.error_code == "PROCESS_RESTARTED"


class SummaryAwareController(SuccessfulController):
    async def decide(self, state, planning_context):
        if state.turns_used == 0:
            assert any(
                message.get("role") == "system"
                and "历史摘要" in message.get("content", "")
                for message in state.messages
            )
        return await super().decide(state, planning_context)


def test_compacted_summary_is_used_by_the_next_run(tmp_path):
    runtime = build_runtime_parts(tmp_path, SummaryAwareController())
    database = tmp_path / "platform.sqlite3"
    migrate_platform_database(database)
    store = AgentSessionStore(database)
    host = SessionAgentRuntime(runtime, store)
    session = host.create_session()
    for index in range(40):
        store.append_message(
            session_id=session.session_id,
            role="user",
            content=f"历史问题 {index} " + ("内容 " * 400),
        )

    result = asyncio.run(host.start_run(session.session_id, "统计场站数"))
    assert result.status == "completed"
    assert host.get_session(session.session_id).summary
