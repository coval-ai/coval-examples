"""OTLP JSON spans with explicit observation boundaries; no invented internal stages."""

import os
import secrets
import time

import httpx


def attributes(values):
    def value(v):
        if isinstance(v, bool):
            return {"boolValue": v}
        if isinstance(v, int):
            return {"intValue": str(v)}
        if isinstance(v, float):
            return {"doubleValue": v}
        return {"stringValue": str(v)}

    return [{"key": k, "value": value(v)} for k, v in values.items() if v is not None]


class Trace:
    def __init__(self, simulation_id):
        self.simulation_id = simulation_id
        self.trace_id = secrets.token_hex(16)
        self.root_id = secrets.token_hex(8)
        self.started = time.time_ns()
        self.spans = []
        self.export_results = []

    def span(
        self,
        name,
        start,
        end=None,
        attrs=None,
        failed=False,
        parent=None,
        span_id=None,
        cancelled_reason=None,
    ):
        # Coval's trace evidence marks a span as a cancellation when its status message
        # says "cancelled". A cancelled reply is neither a success nor a failure: UNSET.
        if failed:
            status = {"code": 2}
        elif cancelled_reason:
            status = {"code": 0, "message": "cancelled: " + cancelled_reason}
        else:
            status = {"code": 1}
        self.spans.append(
            {
                "traceId": self.trace_id,
                "spanId": span_id or secrets.token_hex(8),
                "parentSpanId": self.root_id if parent is None else parent,
                "name": name,
                "kind": 1,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(end or time.time_ns()),
                "attributes": attributes(
                    {
                        "simulation.id": self.simulation_id,
                        "gen_ai.system": "rime",
                        "gen_ai.request.model": "prism",
                        "telemetry.source": "coval-rime-bridge",
                        "telemetry.timing_basis": "client_observed",
                        **(attrs or {}),
                    }
                ),
                "status": status,
            }
        )

    async def export(self):
        if not self.spans or not os.environ.get("COVAL_API_KEY"):
            return
        pending, self.spans = self.spans, []
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": attributes(
                            {
                                "service.name": "coval-rime-prism-test-agent",
                                "service.version": os.getenv(
                                    "SOURCE_REVISION", "local"
                                ),
                                "deployment.environment": os.getenv(
                                    "DEPLOYMENT_ENVIRONMENT", "development"
                                ),
                                "test.synthetic": True,
                            }
                        )
                    },
                    "scopeSpans": [
                        {"scope": {"name": "coval.rime.prism"}, "spans": pending}
                    ],
                }
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    os.environ["COVAL_TRACES_ENDPOINT"],
                    json=payload,
                    headers={
                        "x-api-key": os.environ["COVAL_API_KEY"],
                        "X-Simulation-Id": self.simulation_id,
                    },
                )
            self.export_results.append(
                {"status": response.status_code, "spans": len(pending)}
            )
            if response.is_error:
                self.spans = (pending + self.spans)[:500]
        except httpx.HTTPError as exc:
            self.export_results.append(
                {"error": type(exc).__name__, "spans": len(pending)}
            )
            self.spans = (pending + self.spans)[:500]
