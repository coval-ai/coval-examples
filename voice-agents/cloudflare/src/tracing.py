"""OTLP/JSON spans for one call, exported to Coval's /v1/traces when the call ends.

Span names and attributes follow the other voice agent examples in this repo (conversation, turn,
stt, llm, tts, llm_tool_call, <vertical>.workflow.<tool>), so Coval's latency, token, tool and
trace-aware metrics read them the same way.
"""

import json
import re
import secrets
import time
from typing import Any

SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
STATUS_OK = 1
STATUS_ERROR = 2

# Integer-looking values of these keys still export as doubles.
DOUBLE_KEY = re.compile(r"(^metrics\.ttfb$|_ms$|_seconds$|\.cost_usd$|_usd$|latency)")


def _ns(epoch_seconds: float) -> str:
    return str(int(epoch_seconds * 1_000_000_000))


def _value(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int) and not DOUBLE_KEY.search(key):
        return {"intValue": value}
    if isinstance(value, (int, float)):
        return {"doubleValue": float(value)}
    return {"stringValue": str(value)}


def _attributes(attributes: dict) -> list[dict]:
    return [
        {"key": key, "value": _value(key, value)}
        for key, value in attributes.items()
        if value is not None
    ]


class Span:
    def __init__(self, name, trace_id, parent_id, start, kind=SPAN_KIND_INTERNAL, **attributes):
        self.name, self.trace_id, self.parent_id = name, trace_id, parent_id
        self.span_id = secrets.token_hex(8)
        self.start, self.end, self.kind = start, None, kind
        self.attributes = {k: v for k, v in attributes.items() if v is not None}
        self.events: list[tuple[str, float, dict]] = []
        self.error: str | None = None

    def set(self, **attributes):
        self.attributes.update({k: v for k, v in attributes.items() if v is not None})
        return self

    def event(self, name: str, attributes: dict | None = None):
        self.events.append((name, time.time(), attributes or {}))

    def fail(self, error_type: str):
        self.error = error_type
        self.attributes["error.type"] = error_type
        return self

    def finish(self, end: float | None = None):
        if self.end is None:
            self.end = end if end is not None else time.time()
        return self

    def otlp(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_id or "",
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": _ns(self.start),
            "endTimeUnixNano": _ns(self.end if self.end is not None else time.time()),
            "attributes": _attributes(self.attributes),
            "status": {"code": STATUS_ERROR, "message": self.error}
            if self.error
            else {"code": STATUS_OK, "message": ""},
            "events": [
                {"timeUnixNano": _ns(at), "name": name, "attributes": _attributes(attrs)}
                for name, at, attrs in self.events
            ],
            "links": [],
        }


class CallTracer:
    def __init__(self, service_name: str, **root_attributes):
        self.service_name = service_name
        self.trace_id = secrets.token_hex(16)
        self.root = Span(
            "conversation", self.trace_id, None, time.time(), SPAN_KIND_SERVER, **root_attributes
        )
        self.spans = [self.root]

    def record(self, name, start, end, parent: Span | None = None, **attributes) -> Span:
        span = Span(name, self.trace_id, (parent or self.root).span_id, start, **attributes)
        span.finish(end)
        self.spans.append(span)
        return span

    def payload(self) -> dict:
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _attributes(
                            {"service.name": self.service_name, "service.namespace": "coval-demo"}
                        )
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "coval.cloudflare-voice-agent"},
                            "spans": [span.otlp() for span in self.spans],
                        }
                    ],
                }
            ]
        }

    def body(self) -> str:
        for span in self.spans:
            span.finish()
        return json.dumps(self.payload())


def redact_arguments(arguments: dict, markers: tuple[str, ...]) -> str:
    if not markers:
        return json.dumps(arguments)[:2000]
    redacted = {
        key: "<redacted>" if any(m in key.lower() for m in markers) else arguments[key]
        for key in sorted(arguments)
    }
    return json.dumps(redacted)[:500]
