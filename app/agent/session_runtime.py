from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable

from app.agent.contracts import AgentEvent, AgentMessage, AgentRun
from app.agent.models import AgentRunResponse, AgentRunState
from app.agent.runtime import AgentRuntime
from app.agent.session_store import AgentSessionStore
from app.agent.context import ContextAssembler


class SessionAgentRuntime:
    """Durable host around the bounded domain AgentRuntime loop."""

    def __init__(self, runtime: AgentRuntime, store: AgentSessionStore, *, catalog_snapshot: str = "") -> None:
        self.runtime = runtime
        self.store = store
        self.catalog_snapshot = catalog_snapshot or self._snapshot(runtime)
        self.rule_versions = self._rule_versions(runtime)
        self._event_sequences: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self.context_assembler = ContextAssembler(max_messages=80, max_tokens=12000)
        # A process restart cannot safely continue an in-flight model call.
        # Mark it recoverable so the UI can offer an explicit resume action.
        self.store.pause_incomplete_runs()

    @staticmethod
    def _snapshot(runtime: AgentRuntime) -> str:
        catalog = runtime.catalog
        raw = "\n".join(
            [
                catalog.build_planning_context(),
                json.dumps(
                    getattr(catalog, "runtime_rule_summaries", lambda: [])(),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _rule_versions(runtime: AgentRuntime) -> list[str]:
        summaries = getattr(runtime.catalog, "runtime_rule_summaries", lambda: [])()
        versions: list[str] = []
        for item in summaries:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or item.get("rule_key") or "").strip()
            version = str(item.get("version") or "").strip()
            if identifier:
                versions.append(f"{identifier}@{version}" if version else identifier)
        return sorted(set(versions))

    def create_session(self):
        return self.store.create_session(
            catalog_snapshot=self.catalog_snapshot,
            rule_versions=self.rule_versions,
        )

    def get_session(self, session_id: str):
        return self.store.get_session(session_id)

    def list_messages(self, session_id: str, **kwargs):
        return self.store.list_messages(session_id, **kwargs)

    def get_run(self, run_id: str):
        return self.store.get_run(run_id)

    def list_events(self, run_id: str, **kwargs):
        self.store.get_run(run_id)
        return self.store.list_events(run_id, **kwargs)

    def cancel_run(self, run_id: str):
        run = self.store.get_run(run_id)
        if run.status in {"queued", "running", "waiting_user", "paused"}:
            self._cancelled.add(run_id)
            return self.store.update_run(run_id, status="cancelled", ended_at=True, error_code="CANCELLED_BY_USER")
        return run

    async def start_run(self, session_id: str, user_text: str, *, client_message_id: str | None = None,
                        event_sink: Callable[[dict[str, Any]], None] | None = None) -> AgentRunResponse:
        session = self.store.get_session(session_id)
        if session.status == "archived":
            raise ValueError("SESSION_ARCHIVED")
        history = self.store.list_messages(session_id, limit=80)
        existing_user = next((m for m in reversed(history) if m.metadata.get("client_message_id") == client_message_id), None) if client_message_id else None
        if existing_user and existing_user.run_id:
            run = self.store.get_run(existing_user.run_id)
            return self._response_from_run(run)
        run, user_message, duplicate = self.store.create_run_with_user_message(
            session_id, user_text, client_message_id=client_message_id
        )
        if duplicate:
            return self._response_from_run(run)
        self._maybe_compact(session_id)
        # Compaction appends a summary and hides older messages. Re-read both
        # objects so this run observes the compacted model context immediately.
        session = self.store.get_session(session_id)
        history = self.store.list_messages(session_id, limit=80)
        self._dispatch_event(run, {"event_type": "session_start", "status": "started", "summary": "Agent 会话已载入"}, event_sink)
        self._dispatch_event(run, {"event_type": "run_start", "status": "started", "summary": "Agent 运行已启动"}, event_sink)
        persisted = self.context_assembler.assemble(
            history, session_summary=session.summary, snapshot=session.catalog_snapshot
        ).messages
        return await self._execute(run, user_text, persisted, event_sink=event_sink, original_question=user_text)

    def _maybe_compact(self, session_id: str) -> None:
        messages = self.store.list_messages(session_id, limit=10_000)
        context = self.context_assembler.assemble(messages)
        if context.estimated_tokens >= int(self.context_assembler.max_tokens * 0.7) or len(messages) > 32:
            self.store.compact_transcript(session_id, self.context_assembler.make_summary(messages), keep_recent=8)

    async def resume_run(self, run_id: str, user_text: str | None = None, *, event_sink=None) -> AgentRunResponse:
        run = self.store.get_run(run_id)
        if run.status not in {"waiting_user", "paused"}:
            raise ValueError("RUN_NOT_RESUMABLE")
        session = self.store.get_session(run.session_id)
        if session.expires_at is not None and session.expires_at <= datetime.now(UTC):
            self.store.expire_waiting_run(run_id)
            raise ValueError("CLARIFICATION_EXPIRED")
        history = self.store.list_messages(run.session_id, limit=80)
        if user_text:
            self.store.append_message(session_id=run.session_id, run_id=run_id, role="user", content=user_text.strip(), parent_message_id=history[-1].message_id if history else None)
            history = self.store.list_messages(run.session_id, limit=80)
        self.store.update_run(run_id, status="running", ended_at=None)
        original = next((item.content for item in history if item.run_id == run_id and item.role == "user"), user_text or "")
        checkpoint = self.store.load_checkpoint(run_id)
        if checkpoint:
            self.runtime.tools.restore_checkpoint(checkpoint.get("tool_runtime") or {})
        restored = AgentRunState.model_validate({key: value for key, value in (checkpoint or {}).items() if key != "tool_runtime"}) if checkpoint else None
        return await self._execute(
            run,
            user_text or "",
            self.context_assembler.assemble(
                history, session_summary=session.summary, snapshot=session.catalog_snapshot
            ).messages,
            event_sink=event_sink,
            original_question=original,
            include_current_question=False,
            existing_state=restored,
        )

    async def _execute(self, run: AgentRun, question: str, history: list[dict[str, Any]], *, event_sink=None, original_question: str | None = None, include_current_question: bool = True, existing_state: AgentRunState | None = None) -> AgentRunResponse:
        previous_len = len(existing_state.messages) if existing_state is not None else len(history)
        def on_state(state: AgentRunState):
            nonlocal previous_len
            new_items = state.messages[previous_len:]
            for item in new_items:
                if item.get("role") == "assistant" and item.get("tool_call"):
                    call = item["tool_call"]
                    self.store.append_message(session_id=run.session_id, run_id=run.run_id, role="assistant", content="已选择下一步工具", content_type="tool_call", tool_name=call.get("name"), metadata={"arguments": self._safe_args(call.get("arguments", {}))})
                elif item.get("role") == "tool":
                    self.store.append_message(session_id=run.session_id, run_id=run.run_id, role="tool", content=str(item.get("content", "")), content_type="tool_result", tool_name=item.get("name"), visibility="model", metadata={"status": item.get("status"), "error_code": item.get("payload", {}).get("error_code")})
            previous_len = len(state.messages)
            checkpoint = state.model_dump(mode="json")
            checkpoint["tool_runtime"] = self.runtime.tools.export_checkpoint(state)
            self.store.save_checkpoint(run.run_id, state.turns_used, checkpoint)
        def runtime_sink(item: dict[str, Any]) -> None:
            self._dispatch_event(run, item, event_sink)
        result = await self.runtime.run(question, event_sink=runtime_sink, initial_messages=history, run_id=run.run_id, request_id=run.request_id, original_question=original_question, include_current_question=include_current_question, cancel_check=lambda: run.run_id in self._cancelled, existing_state=existing_state, state_callback=on_state)
        # The cancel endpoint may run while a tool or model call is in flight.
        # Do not let a late successful result overwrite the user's cancellation.
        if run.run_id in self._cancelled and result.status not in {"cancelled", "waiting_user"}:
            result = result.model_copy(
                update={"status": "cancelled", "error_code": "CANCELLED_BY_USER", "answer": None, "response": None}
            )
        status = result.status
        checkpoint_after = self.store.load_checkpoint(run.run_id) or {}
        self.store.update_run(run.run_id, status=status, turn_count=result.turns_used, sql_count=result.sql_queries_used, llm_calls=result.llm_calls_used, total_tokens=int(checkpoint_after.get("total_tokens_used") or 0), error_code=result.error_code, ended_at=True if status in {"completed", "failed", "cancelled"} else None)
        if result.answer:
            evidence_ids = [
                evidence_id
                for item in result.tool_trace
                if item.get("tool") == "finalize_answer"
                for evidence_id in item.get("evidence_ids", [])
            ]
            self.store.append_message(session_id=run.session_id, run_id=run.run_id, role="assistant", content=result.answer, metadata={"evidence_ids": evidence_ids, "response": result.response.model_dump(mode="json") if result.response else None})
        elif result.clarification_question:
            self.store.append_message(session_id=run.session_id, run_id=run.run_id, role="assistant", content=result.clarification_question, metadata={"clarification": True})
        self._dispatch_event(run, {"event_type": "clarification_required" if result.clarification_question else "run_end", "status": "waiting" if result.clarification_question else ("failed" if status == "failed" else "completed"), "summary": result.clarification_question or f"Agent 运行{('失败' if status == 'failed' else '完成')}"}, event_sink)
        return result

    def _response_from_run(self, run: AgentRun) -> AgentRunResponse:
        messages = self.store.list_messages(run.session_id)
        answer_message = next((m for m in reversed(messages) if m.run_id == run.run_id and m.role == "assistant" and m.metadata.get("clarification") is not True), None)
        answer = answer_message.content if answer_message else None
        clarification = next((m.content for m in reversed(messages) if m.run_id == run.run_id and m.metadata.get("clarification")), None)
        response = self.runtime.tools.final_responses.get(run.run_id)
        if response is None and answer_message and answer_message.metadata.get("response"):
            from app.models import ToolResponse
            response = ToolResponse.model_validate(answer_message.metadata["response"])
        status = "waiting_user" if run.status == "waiting_user" else (run.status if run.status in {"completed", "failed", "cancelled"} else "failed")
        return AgentRunResponse(run_id=run.run_id, request_id=run.request_id, status=status, answer=answer, clarification_question=clarification, response=response, error_code=run.error_code, turns_used=run.turn_count, llm_calls_used=run.llm_calls, sql_queries_used=run.sql_count)

    @staticmethod
    def _history_for_model(messages: list[AgentMessage]) -> list[dict[str, Any]]:
        output = []
        for message in messages:
            if message.visibility not in {"model", "user"}:
                continue
            if message.role == "tool":
                output.append({"role": "tool", "name": message.tool_name, "status": message.metadata.get("status"), "content": message.content, "payload": {"error_code": message.metadata.get("error_code")} if message.metadata.get("error_code") else {}})
            else:
                output.append({"role": message.role, "content": message.content})
        return output

    @staticmethod
    def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"keys": sorted(arguments)[:20]}

    @staticmethod
    def _tokens() -> int:
        return 0

    def _dispatch_event(self, run: AgentRun, item: dict[str, Any], sink) -> None:
        sequence = self._event_sequences.get(run.run_id)
        if sequence is None:
            sequence = max((event.sequence for event in self.store.list_events(run.run_id)), default=0)
        sequence += 1
        self._event_sequences[run.run_id] = sequence
        stage = str(item.get("stage") or "")
        is_decision = "_decision" in stage
        event_type = str(item.get("event_type") or ("tool_call" if is_decision else ("tool_result" if item.get("tool") else "turn_start")))
        allowed = {"session_start","run_start","turn_start","assistant_delta","assistant_end","tool_call","tool_result","clarification_required","run_paused","run_end","error"}
        if event_type not in allowed:
            event_type = "tool_result"
        status = str(item.get("status") or "completed")
        if status not in {"started", "completed", "failed", "waiting"}:
            status = "failed" if status == "error" else "completed"
        event_tool = item.get("action_tool") if is_decision else item.get("tool")
        event = AgentEvent(run_id=run.run_id, sequence=sequence, event_id=f"evt_{__import__('uuid').uuid4().hex}", event_type=event_type, status=status, summary=str(item.get("summary") or "Agent 正在处理"), tool_name=event_tool, duration_ms=item.get("duration_ms"), error_code=item.get("error_type"), metadata={key: value for key, value in item.items() if key in {"turn","stage","request_id","action_tool"}}, created_at=datetime.now(UTC))
        event = self.store.append_event(event)
        self._event_sequences[run.run_id] = event.sequence
        if sink is not None:
            public = event.model_dump(mode="json")
            public["request_id"] = run.request_id
            sink(public)
