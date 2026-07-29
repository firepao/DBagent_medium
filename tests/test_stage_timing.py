import json
import time

import pytest

from app.stage_timing import StageTimingRepository


def test_stage_timing_records_success_and_duration(tmp_path) -> None:
    path = tmp_path / "stage_timing.jsonl"
    repository = StageTimingRepository(path)

    with repository.measure("qry_test", "planning"):
        time.sleep(0.002)

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["request_id"] == "qry_test"
    assert entry["stage"] == "planning"
    assert entry["status"] == "success"
    assert entry["duration_ms"] >= 1
    assert entry["started_at"]
    assert entry["timestamp"]


def test_stage_timing_records_error_without_swallowing_it(tmp_path) -> None:
    path = tmp_path / "stage_timing.jsonl"
    repository = StageTimingRepository(path)

    with pytest.raises(ValueError):
        with repository.measure("qry_test", "sql_generation_initial"):
            raise ValueError("invalid output")

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["status"] == "error"
    assert entry["error_type"] == "ValueError"


def test_request_total_includes_derived_start_time(tmp_path) -> None:
    path = tmp_path / "stage_timing.jsonl"
    repository = StageTimingRepository(path)
    started = time.monotonic()

    time.sleep(0.002)
    repository.record_duration(
        "qry_test", "request_total", started, status="success"
    )

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["stage"] == "request_total"
    assert entry["started_at"]
    assert entry["timestamp"]
    assert entry["duration_ms"] >= 1
