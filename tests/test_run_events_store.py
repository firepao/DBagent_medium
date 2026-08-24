from app.run_events import RunEvent, RunEventStore


def test_run_events_are_persisted_and_replayed_in_sequence(tmp_path):
    store = RunEventStore(tmp_path / "platform.sqlite3")
    import sqlite3
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE run_events (request_id TEXT, sequence INTEGER, payload_json TEXT, PRIMARY KEY(request_id, sequence))")
    store.append(RunEvent.create(request_id="qry_1", sequence=2, stage="execution", status="completed", summary="完成"))
    store.append(RunEvent.create(request_id="qry_1", sequence=1, stage="planning", status="started", summary="处理中"))
    events = store.list("qry_1")
    assert [event.sequence for event in events] == [1, 2]


def test_legacy_run_event_json_remains_compatible():
    event = RunEvent.model_validate_json(
        '{"request_id":"qry_old","sequence":1,"stage":"planning",'
        '"status":"completed","timestamp":"2026-08-21T00:00:00+00:00",'
        '"duration_ms":1,"summary":"完成","error_type":null}'
    )
    assert event.model is None
    assert event.total_tokens is None
    assert event.rule_versions == []


def test_run_event_serialization_contains_only_sanitized_metadata():
    event = RunEvent.create(
        request_id="qry_1",
        sequence=1,
        stage="planning",
        status="completed",
        summary="完成",
        model="model-1",
        provider="primary",
        tool="llm",
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
        rule_versions=["managed:capacity:v2"],
    )
    payload = event.model_dump_json()
    assert "managed:capacity:v2" in payload
    for sensitive_name in ("sql", "prompt", "api_key", "result_rows"):
        assert sensitive_name not in payload
