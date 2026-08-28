from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.contracts import AgentEvent, AgentMessage, AgentRun, AgentSession


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class SessionConflict(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


class AgentSessionStore:
    """SQLite-backed session/transcript store. It contains no model policy."""

    def __init__(self, path: str | Path, *, clarification_ttl_seconds: float = 900.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clarification_ttl_seconds = clarification_ttl_seconds

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def create_session(self, *, catalog_snapshot: str = "", rule_versions: list[str] | None = None) -> AgentSession:
        session_id = _id("ses")
        now = _now()
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO agent_sessions(session_id,status,catalog_snapshot,rule_versions_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (session_id, "active", catalog_snapshot, json.dumps(rule_versions or [], ensure_ascii=False), now, now),
            )
            db.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> AgentSession:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return AgentSession(
            session_id=row[0], status=row[1], title=row[2], summary=row[3], summary_version=row[4],
            catalog_snapshot=row[5], rule_versions=json.loads(row[6] or "[]"), active_run_id=row[7],
            created_at=datetime.fromisoformat(row[8]), updated_at=datetime.fromisoformat(row[9]), expires_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )

    def touch_session(self, session_id: str, *, status: str | None = None, active_run_id: str | None = None) -> None:
        self.get_session(session_id)
        with closing(self._connect()) as db:
            if status is None and active_run_id is None:
                db.execute("UPDATE agent_sessions SET updated_at=? WHERE session_id=?", (_now(), session_id))
            elif status is None:
                db.execute("UPDATE agent_sessions SET active_run_id=?,updated_at=? WHERE session_id=?", (active_run_id, _now(), session_id))
            else:
                db.execute("UPDATE agent_sessions SET status=?,active_run_id=?,updated_at=? WHERE session_id=?", (status, active_run_id, _now(), session_id))
            db.commit()

    def pause_incomplete_runs(self) -> int:
        """Mark runs left by a previous process as explicitly recoverable."""
        now = _now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT run_id FROM agent_runs WHERE status IN ('queued','running')"
            ).fetchall()
            if rows:
                db.execute(
                    "UPDATE agent_runs SET status='paused',error_code='PROCESS_RESTARTED' "
                    "WHERE status IN ('queued','running')"
                )
                db.execute(
                    "UPDATE agent_sessions SET status='active',updated_at=? "
                    "WHERE active_run_id IN (SELECT run_id FROM agent_runs WHERE status='paused')",
                    (now,),
                )
            db.commit()
        return len(rows)

    def create_run_with_user_message(
        self,
        session_id: str,
        text: str,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
        client_message_id: str | None = None,
    ) -> tuple[AgentRun, AgentMessage, bool]:
        """Atomically create the run and its first user message.

        The boolean is true when an idempotency key returned an existing run.
        """
        run_id, request_id, now = run_id or _id("run"), request_id or _id("qry"), _now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            session_row = db.execute(
                "SELECT status FROM agent_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session_row is None:
                raise SessionNotFound(session_id)
            if client_message_id:
                existing = db.execute(
                    "SELECT * FROM agent_messages WHERE session_id=? "
                    "AND json_extract(metadata_json,'$.client_message_id')=? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (session_id, client_message_id),
                ).fetchone()
                if existing and existing[2]:
                    existing_run = db.execute(
                        "SELECT * FROM agent_runs WHERE run_id=?", (existing[2],)
                    ).fetchone()
                    if existing_run:
                        db.commit()
                        return self._run(existing_run), self._message(existing), True
            active_id = db.execute(
                "SELECT active_run_id FROM agent_sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            if active_id:
                active = db.execute(
                    "SELECT status FROM agent_runs WHERE run_id=?", (active_id,)
                ).fetchone()
                if active and active[0] in {"queued", "running", "waiting_user", "paused"}:
                    raise SessionConflict(session_id)
            db.execute(
                "INSERT INTO agent_runs(run_id,session_id,request_id,status,started_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, session_id, request_id, "running", now),
            )
            db.execute(
                "UPDATE agent_sessions SET active_run_id=?,status='active',updated_at=? "
                "WHERE session_id=?",
                (run_id, now, session_id),
            )
            message_id = _id("msg")
            metadata = {"client_message_id": client_message_id} if client_message_id else {}
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM agent_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO agent_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, session_id, run_id, sequence, "user", text.strip(), "text",
                 None, None, None, "model", json.dumps(metadata, ensure_ascii=False), now),
            )
            db.commit()
            run_row = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            message_row = db.execute("SELECT * FROM agent_messages WHERE message_id=?", (message_id,)).fetchone()
        return self._run(run_row), self._message(message_row), False

    def append_message(self, *, session_id: str, role: str, content: str, content_type: str = "text",
                       run_id: str | None = None, tool_name: str | None = None,
                       tool_call_id: str | None = None, parent_message_id: str | None = None,
                       visibility: str = "model", metadata: dict[str, Any] | None = None,
                       message_id: str | None = None, client_message_id: str | None = None) -> AgentMessage:
        self.get_session(session_id)
        message_id = message_id or _id("msg")
        metadata = dict(metadata or {})
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        now = _now()
        with closing(self._connect()) as db:
            if client_message_id:
                existing = db.execute("SELECT * FROM agent_messages WHERE session_id=? AND json_extract(metadata_json,'$.client_message_id')=?", (session_id, client_message_id)).fetchone()
                if existing:
                    return self._message(existing)
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM agent_messages WHERE session_id=?", (session_id,)).fetchone()[0]
            db.execute("INSERT INTO agent_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, session_id, run_id, sequence, role, content, content_type, tool_name, tool_call_id, parent_message_id, visibility, json.dumps(metadata, ensure_ascii=False), now))
            db.commit()
            row = db.execute("SELECT * FROM agent_messages WHERE message_id=?", (message_id,)).fetchone()
        return self._message(row)

    def list_messages(self, session_id: str, *, after_sequence: int = 0, limit: int = 200) -> list[AgentMessage]:
        self.get_session(session_id)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM agent_messages WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?", (session_id, after_sequence, min(limit, 1000))).fetchall()
        return [self._message(row) for row in rows]

    def find_message_by_client_id(self, session_id: str, client_message_id: str) -> AgentMessage | None:
        self.get_session(session_id)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM agent_messages WHERE session_id=? AND json_extract(metadata_json,'$.client_message_id')=? ORDER BY sequence DESC LIMIT 1",
                (session_id, client_message_id),
            ).fetchone()
        return self._message(row) if row else None

    def set_summary(self, session_id: str, summary: str, *, source_sequence: int) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE agent_sessions SET summary=?,summary_version=summary_version+1,updated_at=? WHERE session_id=?",
                (summary, _now(), session_id),
            )
            db.commit()

    def compact_transcript(self, session_id: str, summary: str, *, keep_recent: int = 8) -> AgentMessage | None:
        messages = self.list_messages(session_id, limit=10_000)
        if len(messages) <= keep_recent + 1:
            return None
        cutoff = messages[-keep_recent].sequence
        summary_message = self.append_message(
            session_id=session_id, role="system", content=summary[:4000],
            content_type="summary", visibility="model",
            metadata={"source_sequence": cutoff - 1},
        )
        with closing(self._connect()) as db:
            db.execute("UPDATE agent_messages SET visibility='internal' WHERE session_id=? AND sequence<?", (session_id, cutoff))
            db.commit()
        self.set_summary(session_id, summary[:4000], source_sequence=cutoff - 1)
        return summary_message

    @staticmethod
    def _message(row: tuple[Any, ...]) -> AgentMessage:
        return AgentMessage(message_id=row[0], session_id=row[1], run_id=row[2], sequence=row[3], role=row[4], content=row[5], content_type=row[6], tool_name=row[7], tool_call_id=row[8], parent_message_id=row[9], visibility=row[10], metadata=json.loads(row[11] or "{}"), created_at=datetime.fromisoformat(row[12]))

    def create_run(self, session_id: str, *, request_id: str | None = None, run_id: str | None = None) -> AgentRun:
        run_id, request_id, now = run_id or _id("run"), request_id or _id("qry"), _now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT active_run_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise SessionNotFound(session_id)
            if row[0]:
                active = db.execute("SELECT status FROM agent_runs WHERE run_id=?", (row[0],)).fetchone()
                if active and active[0] in {"queued", "running", "waiting_user", "paused"}:
                    raise SessionConflict(session_id)
            db.execute("INSERT INTO agent_runs(run_id,session_id,request_id,status,started_at) VALUES(?,?,?,?,?)", (run_id, session_id, request_id, "running", now))
            db.execute("UPDATE agent_sessions SET active_run_id=?,status='active',updated_at=? WHERE session_id=?", (run_id, now, session_id))
            db.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> AgentRun:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise SessionNotFound(run_id)
        return self._run(row)

    @staticmethod
    def _run(row: tuple[Any, ...]) -> AgentRun:
        return AgentRun(run_id=row[0], session_id=row[1], request_id=row[2], status=row[3], turn_count=row[4], sql_count=row[5], llm_calls=row[6], total_tokens=row[7], error_code=row[8], checkpoint_sequence=row[9], started_at=datetime.fromisoformat(row[10]), ended_at=datetime.fromisoformat(row[11]) if row[11] else None)

    def update_run(self, run_id: str, **fields: Any) -> AgentRun:
        allowed = {"status", "turn_count", "sql_count", "llm_calls", "total_tokens", "error_code", "checkpoint_sequence", "ended_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_run(run_id)
        if updates.get("ended_at") is True:
            updates["ended_at"] = _now()
        with closing(self._connect()) as db:
            sql = ",".join(f"{key}=?" for key in updates)
            db.execute(f"UPDATE agent_runs SET {sql} WHERE run_id=?", (*updates.values(), run_id))
            if updates.get("status") in {"completed", "failed", "cancelled"}:
                db.execute("UPDATE agent_sessions SET active_run_id=NULL,status='active',expires_at=NULL,updated_at=? WHERE active_run_id=?", (_now(), run_id))
            elif updates.get("status") == "waiting_user":
                expires = (datetime.now(UTC) + timedelta(seconds=self.clarification_ttl_seconds)).isoformat()
                db.execute("UPDATE agent_sessions SET status='waiting_user',expires_at=?,updated_at=? WHERE active_run_id=?", (expires, _now(), run_id))
            db.commit()
        return self.get_run(run_id)

    def append_event(self, event: AgentEvent) -> AgentEvent:
        payload = event.model_dump(mode="json")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            maximum = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM agent_events WHERE run_id=?",
                (event.run_id,),
            ).fetchone()[0]
            if event.sequence <= maximum:
                event = event.model_copy(update={"sequence": maximum + 1})
                payload = event.model_dump(mode="json")
            db.execute("INSERT INTO agent_events(event_id,run_id,sequence,event_type,status,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (event.event_id, event.run_id, event.sequence, event.event_type, event.status, json.dumps(payload, ensure_ascii=False), _now()))
            db.commit()
        return event

    def list_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[AgentEvent]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload_json FROM agent_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (run_id, after_sequence, min(limit, 2000))).fetchall()
        return [AgentEvent.model_validate_json(row[0]) for row in rows]

    def save_checkpoint(self, run_id: str, sequence: int, state_json: dict[str, Any]) -> None:
        self.update_run(run_id, checkpoint_sequence=sequence)
        with closing(self._connect()) as db:
            db.execute("INSERT OR REPLACE INTO agent_checkpoints(run_id,sequence,state_json,saved_at) VALUES(?,?,?,?)", (run_id, sequence, json.dumps(state_json, ensure_ascii=False), _now()))
            db.commit()

    def expire_waiting_run(self, run_id: str) -> AgentRun:
        return self.update_run(run_id, status="failed", error_code="CLARIFICATION_EXPIRED", ended_at=True)

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT state_json FROM agent_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None
