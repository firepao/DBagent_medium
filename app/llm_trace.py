import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_trace_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "llm_trace_context", default=None
)


@contextmanager
def llm_trace_context(request_id: str, stage: str) -> Iterator[None]:
    token = _trace_context.set((request_id, stage))
    try:
        yield
    finally:
        _trace_context.reset(token)


class LLMTraceRepository:
    """Append-only server-side traces for LLM output diagnostics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record_output(self, model: str, output: str) -> None:
        self._record({"model": model, "event": "output", "output": output})

    def record_error(self, model: str, error_type: str) -> None:
        self._record({"model": model, "event": "error", "error_type": error_type})

    def _record(self, payload: dict[str, str]) -> None:
        context = _trace_context.get()
        if context is None:
            return
        request_id, stage = context
        serialized = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "stage": stage,
                **payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
