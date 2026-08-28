from __future__ import annotations

import logging
from typing import Any

from app.run_events import RunEvent

logger = logging.getLogger(__name__)


class TelemetryBridge:
    """Optional OTel span bridge; absence or exporter failure never affects queries."""

    def __init__(
        self, endpoint: str = "", service_name: str = "resources-agent", *, span_exporter: Any | None = None
    ) -> None:
        self.enabled = False
        self._tracer = None
        self._provider = None
        self._trace = None
        self._root_spans: dict[str, Any] = {}
        self._stage_spans: dict[tuple[str, str], Any] = {}
        if not endpoint and span_exporter is None:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            exporter = span_exporter or OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("resources-agent")
            self._provider = provider
            self._trace = trace
            self.enabled = True
        except Exception as exc:  # optional dependency/exporter is a soft feature
            logger.warning("OTel 旁路追踪不可用，将继续运行查询服务: %s", type(exc).__name__)

    def record(self, event: RunEvent) -> None:
        if not self.enabled or self._tracer is None:
            return
        try:
            key = event.request_id
            if event.status == "started":
                root = self._root_spans.get(key)
                if root is None:
                    root = self._tracer.start_span(
                        "agent.query", attributes={"agent.request_id": event.request_id}
                    )
                    self._root_spans[key] = root
                context = self._trace.set_span_in_context(root)
                self._stage_spans[(key, event.stage)] = self._tracer.start_span(
                    f"agent.{event.stage}", attributes={
                        "agent.request_id": event.request_id,
                        "agent.stage": event.stage,
                    }, context=context,
                )
            else:
                span = self._stage_spans.pop((key, event.stage), None)
                if span is not None:
                    span.set_attribute("agent.status", event.status)
                    span.set_attribute("agent.duration_ms", event.duration_ms or 0)
                    for name in ("model", "provider", "tool", "input_tokens", "output_tokens", "total_tokens"):
                        value = getattr(event, name)
                        if value is not None:
                            span.set_attribute(f"agent.{name}", value)
                    if event.rule_versions:
                        span.set_attribute("agent.rule_versions", event.rule_versions)
                    if event.error_type:
                        span.set_attribute("agent.error_type", event.error_type)
                    span.end()
        except Exception:
            logger.debug("OTel event export failed", exc_info=True)

    def finish(self, request_id: str) -> None:
        if not self.enabled:
            return
        try:
            for key, span in list(self._stage_spans.items()):
                if key[0] == request_id:
                    span.end()
                    self._stage_spans.pop(key, None)
            root = self._root_spans.pop(request_id, None)
            if root is not None:
                root.end()
        except Exception:
            logger.debug("OTel trace finish failed", exc_info=True)

    def shutdown(self) -> None:
        provider = self._provider
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                logger.debug("OTel provider shutdown failed", exc_info=True)
