"""
Coval OpenTelemetry tracing for LiveKit voice agents.

Two pieces of glue between the standard OTel SDK and Coval's `/v1/traces`
endpoint:

1. `DynamicCovalExporter` — wraps the standard `OTLPSpanExporter` and buffers
   spans until the Coval simulation ID is known. The simulation ID arrives
   mid-session via the SIP header `X-Coval-Simulation-Id` (surfaced as a
   participant attribute when the caller joins), so spans emitted before
   the participant connects would otherwise have no target. Once
   `set_simulation_id()` is called, buffered spans flush and subsequent
   spans export immediately.

2. `setup_coval_tracing()` — installs a tracer provider with that exporter.
   Call once at process start.

This module is intentionally small. If you need multi-simulation routing,
contextvar-based propagation, or buffer-per-simulation, look at the
`@coval/wizard`-generated module instead — that handles those cases.
"""

import os
from typing import Optional, Sequence

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

COVAL_TRACES_ENDPOINT = "https://api.coval.dev/v1/traces"


class DynamicCovalExporter(SpanExporter):
    """OTLP span exporter that buffers spans until the Coval simulation ID is known.

    Wraps the standard `OTLPSpanExporter` from `opentelemetry-exporter-otlp-proto-http`.
    All OTLP serialization, retries, and HTTP transport are handled by the standard
    exporter; this class only adds the buffer-until-simulation-id behavior LiveKit's
    SIP-based dispatch flow requires.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = COVAL_TRACES_ENDPOINT,
        timeout_seconds: int = 30,
    ):
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._inner: Optional[OTLPSpanExporter] = None
        self._buffer: list[ReadableSpan] = []

    def reset(self) -> None:
        """Clear state between sessions when the agent process is reused."""
        if self._inner:
            self._inner.shutdown()
        self._inner = None
        self._buffer.clear()

    def set_simulation_id(self, simulation_id: str) -> None:
        """Activate tracing for `simulation_id` and flush any buffered spans."""
        self._inner = OTLPSpanExporter(
            endpoint=self._endpoint,
            headers={
                "X-API-Key": self._api_key,
                "X-Simulation-Id": simulation_id,
            },
            timeout=self._timeout_seconds,
        )
        if self._buffer:
            spans_to_flush = list(self._buffer)
            self._buffer.clear()
            print(f"[coval] flushing {len(spans_to_flush)} buffered spans")
            self._inner.export(spans_to_flush)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._inner:
            return self._inner.export(spans)
        print(f"[coval] buffering {len(spans)} spans (no simulation_id yet)")
        self._buffer.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._inner:
            return self._inner.force_flush(timeout_millis)
        return True

    def shutdown(self) -> None:
        if self._inner:
            self._inner.shutdown()


def setup_coval_tracing(service_name: str) -> Optional[DynamicCovalExporter]:
    """Install a tracer provider that exports to Coval. Returns the exporter so the
    caller can attach it to the per-call lifecycle (`reset()` and `set_simulation_id()`).

    Reads `COVAL_API_KEY` from the environment. Returns None if it's not set,
    which leaves OTel uninstalled (effectively disables tracing).
    """
    api_key = os.getenv("COVAL_API_KEY")
    if not api_key:
        print("[coval] COVAL_API_KEY not set — tracing disabled")
        return None

    exporter = DynamicCovalExporter(api_key=api_key)
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    print("[coval] tracing initialized")
    return exporter
