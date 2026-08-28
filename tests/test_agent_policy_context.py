from datetime import UTC, datetime
import time

from app.agent.answers import AnswerClaim, AnswerDraft, AnswerVerifier
from app.agent.context import ContextAssembler
from app.agent.contracts import AgentMessage
from app.agent.models import AgentAction, AgentRunState
from app.agent.policy import BudgetPolicy


def test_budget_policy_blocks_duplicate_and_sql_budget():
    state = AgentRunState(run_id="run_x", request_id="qry_x", original_question="q", original_question_hash="h", sql_queries_used=1)
    policy = BudgetPolicy(max_sql_queries=1)
    action = AgentAction(tool_name="execute_readonly_query", arguments={"sql": "SELECT 1"})
    blocked = policy.check(action, state, set())
    assert blocked and blocked.model_payload["error_code"] == "SQL_QUERY_BUDGET_EXHAUSTED"
    other = AgentAction(tool_name="get_table_context", arguments={"table_hints": ["t"]})
    key = policy.action_key(other, state)
    duplicate = policy.check(other, state, {key})
    assert duplicate and duplicate.model_payload["error_code"] == "DUPLICATE_TOOL_CALL"


def test_context_assembler_is_bounded_and_excludes_internal_messages():
    messages = [
        AgentMessage(message_id=f"m{i}", session_id="s", sequence=i, role="user", content="x" * 100, content_type="text", created_at=datetime.now(UTC))
        for i in range(1, 8)
    ]
    messages.append(AgentMessage(message_id="internal", session_id="s", sequence=8, role="system", content="secret", content_type="text", visibility="internal", created_at=datetime.now(UTC)))
    context = ContextAssembler(max_messages=3, max_tokens=20).assemble(messages)
    assert len(context.messages) <= 3
    assert all(item.get("content") != "secret" for item in context.messages)
    assert context.context_hash


def test_answer_verifier_requires_approved_evidence_and_known_fields():
    verifier = AnswerVerifier()
    draft = AnswerDraft(text="共 3 个", claims=[AnswerClaim(text="共 3 个", evidence_ids=["ev_1"], fields=["station_count"])])
    ok, code = verifier.verify(draft, approved_evidence_ids={"ev_1"}, evidence_payloads={"ev_1": {"columns": [{"name": "station_count"}], "rows": [{"station_count": 3}], "row_count": 1}})
    assert ok and code is None
    bad, code = verifier.verify(draft, approved_evidence_ids=set(), evidence_payloads={})
    assert not bad and code == "ANSWER_NOT_GROUNDED"


def test_wall_time_budget_is_scoped_to_the_run_not_process_start():
    state = AgentRunState(
        run_id="run_x",
        request_id="qry_x",
        original_question="q",
        original_question_hash="h",
        started_at_monotonic=time.monotonic(),
    )
    policy = BudgetPolicy(max_wall_time_seconds=0.1)
    policy.started_at = time.monotonic() - 10
    action = AgentAction(tool_name="get_table_context", arguments={"table_hints": ["t"]})

    assert policy.check(action, state, set()) is None
