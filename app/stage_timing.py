import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


class StageTimingRepository:
    """Append-only timing records for the Medium query pipeline."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @contextmanager
    def measure(self, request_id: str, stage: str) -> Iterator[None]:
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        status = "success"
        error_type = None
        try:
            yield
        except BaseException as exc:
            status = "error"
            error_type = type(exc).__name__
            raise
        finally:
            self.record(
                request_id=request_id,
                stage=stage,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                status=status,
                error_type=error_type,
            )

    def record_duration(
        self,
        request_id: str,
        stage: str,
        started: float,
        *,
        status: str,
    ) -> None:
        finished_at = datetime.now(timezone.utc)
        duration_ms = int((time.monotonic() - started) * 1000)
        self.record(
            request_id=request_id,
            stage=stage,
            started_at=finished_at - timedelta(milliseconds=duration_ms),
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
        )

    def record(
        self,
        *,
        request_id: str,
        stage: str,
        started_at: datetime | None,
        duration_ms: int,
        status: str,
        error_type: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        finished_at = finished_at or datetime.now(timezone.utc)
        payload = {
            "timestamp": finished_at.isoformat(),
            "request_id": request_id,
            "stage": stage,
            "status": status,
            "duration_ms": duration_ms,
        }
        if started_at is not None:
            payload["started_at"] = started_at.isoformat()
        if error_type is not None:
            payload["error_type"] = error_type
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
