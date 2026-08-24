from app.run_events import RunEvent
from app.telemetry import TelemetryBridge


def test_telemetry_is_soft_when_endpoint_or_dependency_is_missing():
    bridge = TelemetryBridge()
    event = RunEvent.create(request_id="qry_test", sequence=1, stage="planning", status="started", summary="处理中")
    bridge.record(event)
    assert bridge.enabled is False


def test_telemetry_bridge_accepts_events_without_leaking_payload():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    bridge = TelemetryBridge(span_exporter=InMemorySpanExporter())
    started = RunEvent.create(request_id="qry_test", sequence=1, stage="planning", status="started", summary="处理中")
    completed = RunEvent.create(request_id="qry_test", sequence=2, stage="planning", status="completed", summary="完成", duration_ms=3, model="model-1", provider="primary", tool="llm", input_tokens=12, output_tokens=3, total_tokens=15, rule_versions=["managed:capacity:v2"])
    bridge.record(started)
    root = bridge._root_spans["qry_test"]
    stage = bridge._stage_spans[("qry_test", "planning")]
    assert root.get_span_context().trace_id == stage.get_span_context().trace_id
    assert set(stage.attributes) == {"agent.request_id", "agent.stage"}
    bridge.record(completed)
    assert stage.attributes["agent.model"] == "model-1"
    assert stage.attributes["agent.provider"] == "primary"
    assert stage.attributes["agent.tool"] == "llm"
    assert stage.attributes["agent.total_tokens"] == 15
    assert stage.attributes["agent.rule_versions"] == ("managed:capacity:v2",)
    bridge.finish("qry_test")
    bridge.shutdown()
    assert bridge._root_spans == {}
    assert bridge._stage_spans == {}
