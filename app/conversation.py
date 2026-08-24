from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingClarification:
    question: str
    expires_at: float


class ConversationStore:
    def __init__(self, ttl_seconds: float = 900.0, max_sessions: int = 1000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._pending: dict[str, PendingClarification] = {}

    def health_summary(self) -> dict[str, object]:
        """Expose deployment limits without exposing pending question content."""
        return {
            "backend": "memory",
            "multi_replica_supported": False,
            "ttl_seconds": self.ttl_seconds,
            "max_sessions": self.max_sessions,
            "pending_sessions": len(self._pending),
        }

    def session_id(self, value: str | None) -> str:
        return value or f"ses_{uuid.uuid4().hex}"

    def resolve(self, session_id: str, message: str) -> str:
        self._prune()
        pending = self._pending.pop(session_id, None)
        if pending is None:
            return message
        return f"原问题：{pending.question[:1200]}\n用户补充：{message[:760]}"

    def require_clarification(self, session_id: str, effective_question: str) -> None:
        self._prune()
        if len(self._pending) >= self.max_sessions:
            oldest = min(self._pending, key=lambda key: self._pending[key].expires_at)
            self._pending.pop(oldest, None)
        self._pending[session_id] = PendingClarification(
            question=effective_question,
            expires_at=time.monotonic() + self.ttl_seconds,
        )

    def clear(self, session_id: str) -> None:
        self._pending.pop(session_id, None)

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, value in self._pending.items() if value.expires_at <= now]
        for key in expired:
            self._pending.pop(key, None)
