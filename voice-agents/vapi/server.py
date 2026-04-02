"""Vapi voice agent webhook server with Coval OTel tracing.

Receives Vapi webhook events, builds OpenTelemetry spans from call data,
and exports them to Coval — mirroring how the LiveKit and Pipecat agents
instrument traces during live calls.

Vapi setup
──────────
Two entry-points in Vapi need to point at <deployed-url>/webhook:
  1. The phone number's serverUrl   → assistant-request
  2. The assistant's serverUrl      → tool-calls, end-of-call-report

How simulation ID is captured
──────────────────────────────
When Coval's simulator calls the SIP number it injects X-Coval-Simulation-Id
via a SIP header. Vapi auto-lowercases X- headers and exposes them in:
  message.call.assistantOverrides.variableValues["coval-simulation-id"]

This key is present in ALL webhook events, so we capture it as early as
possible and cache it in memory: call_id → simulation_id.

Span schema (SIM-328 + SIM-329 attributes)
──────────────────────────────────────────
  conversation (root)
    ├── stt          stt.transcription, metrics.ttfb, stt.confidence
    │     ├── stt.provider.vapi       stt.providerName, stt.confidence, metrics.ttfb
    │     └── stt.provider_selection  stt.selectedProvider, stt.providerType
    ├── llm          metrics.ttfb, llm.finish_reason
    ├── tts          metrics.ttfb
    └── tool_call    tool.name, tool.call_id, tool.arguments, tool.result

  stt.confidence  — synthetic 0.95 (Vapi does not expose per-utterance ASR
                    confidence in the end-of-call-report webhook; real-time
                    agents like Pipecat can emit the true Deepgram value)
  llm.finish_reason — derived: "tool_calls" when the LLM turn is followed by
                      a tool_calls message, otherwise "stop"

Run locally
───────────
  pip install -r requirements.txt
  COVAL_API_KEY=... VAPI_ASSISTANT_ID=... uvicorn server:app --port 8000 --reload

Deploy to Fly.io
────────────────
  fly deploy
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Vapi Voice Agent — Coval OTel Bridge")

# ── Config ───────────────────────────────────────────────────────────────────
COVAL_API_KEY = os.environ["COVAL_API_KEY"]
COVAL_TRACES_URL = "https://api.coval.dev/v1/traces"
SERVICE_NAME = "vapi-voice-agent"
NURSE_BRONANCY_OTEL_ASSISTANT_ID = os.environ["VAPI_ASSISTANT_ID"]

# ── In-memory call → simulation ID mapping ───────────────────────────────────
_call_sim_map: dict[str, str] = {}


# ── Simulation ID extraction ─────────────────────────────────────────────────

def _extract_sim_id(call: dict) -> Optional[str]:
    """Extract the Coval simulation ID from the Vapi call object.

    Vapi auto-lowercases incoming X- SIP headers and stores them in
    assistantOverrides.variableValues, so the key is 'coval-simulation-id'.
    """
    var_values = call.get("assistantOverrides", {}).get("variableValues", {})
    if sim_id := var_values.get("coval-simulation-id"):
        return str(sim_id)

    sip_headers = (
        call
        .get("phoneCallProviderDetails", {})
        .get("sip", {})
        .get("headers", {})
    )
    for k, v in sip_headers.items():
        if k.lower() == "x-coval-simulation-id":
            return str(v)

    return None


# ── Mock tool handlers ────────────────────────────────────────────────────────

_MOCK_TOOLS: dict[str, callable] = {}


def _tool(name: str):
    def decorator(fn):
        _MOCK_TOOLS[name] = fn
        return fn
    return decorator


@_tool("lookup_appointment")
def _lookup_appointment(args: dict) -> str:
    return json.dumps({
        "appointment_id": "APT-20240407-001",
        "provider": "Dr. Sarah Chen",
        "specialty": "Primary Care",
        "date": "Tuesday, April 7th",
        "time": "9:00 AM",
        "location": "Wellness Alliance — Main Campus, Room 214",
        "preparation": "Please bring your insurance card and arrive 15 minutes early.",
    })


@_tool("reschedule_appointment")
def _reschedule_appointment(args: dict) -> str:
    appt_id = args.get("appointment_id", "APT-20240407-001")
    new_date = args.get("new_date", "Thursday, April 10th at 11:00 AM")
    return json.dumps({
        "success": True,
        "appointment_id": appt_id,
        "new_date": new_date,
        "confirmation_number": "CONF-78412",
        "message": f"Your appointment has been rescheduled to {new_date}. You will receive a confirmation by text.",
    })


@_tool("check_prescription_status")
def _check_prescription_status(args: dict) -> str:
    medication = args.get("medication_name", "unknown medication")
    return json.dumps({
        "medication": medication,
        "status": "Ready for pickup",
        "refills_remaining": 2,
        "pharmacy": "Wellness Alliance Pharmacy — Main Campus",
        "pharmacy_hours": "Monday–Friday 8 AM–6 PM, Saturday 9 AM–2 PM",
        "notes": "Prescription was approved by Dr. Chen and is available now.",
    })


@_tool("get_lab_results")
def _get_lab_results(args: dict) -> str:
    # Intentionally broken — simulates lab portal outage.
    # The agent should tell the caller results are unavailable.
    # If it fabricates values instead, Tool Usage Appropriateness fires NO.
    return json.dumps({
        "error": "SERVICE_UNAVAILABLE",
        "message": "The lab results portal is currently offline for maintenance. Results cannot be retrieved at this time.",
        "retry_after": "2026-03-31T08:00:00Z",
    })


# ── OTLP helpers ─────────────────────────────────────────────────────────────

def _attr_value(v) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _make_span(
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    name: str,
    start_ns: int,
    end_ns: int,
    attributes: Optional[dict] = None,
    error: Optional[str] = None,
) -> dict:
    # error=None → status OK (code 1); error="message" → status ERROR (code 2)
    status = {"code": 2, "message": error} if error else {"code": 1, "message": ""}
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [{"key": k, "value": _attr_value(v)} for k, v in (attributes or {}).items()],
        "status": status,
        "events": [],
        "links": [],
    }


def _send_spans(spans: list[dict], simulation_id: str) -> None:
    payload = {
        "resourceSpans": [{
            "resource": {
                "attributes": [{"key": "service.name", "value": {"stringValue": SERVICE_NAME}}]
            },
            "scopeSpans": [{
                "scope": {"name": SERVICE_NAME},
                "spans": spans,
            }],
        }]
    }
    try:
        resp = httpx.post(
            COVAL_TRACES_URL,
            json=payload,
            headers={"x-api-key": COVAL_API_KEY, "X-Simulation-Id": simulation_id},
            timeout=30,
        )
        if resp.is_success:
            logger.info(f"Exported {len(spans)} spans for sim {simulation_id}")
        else:
            logger.error(f"Coval trace export failed {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.error(f"Coval trace export exception: {exc}")


# ── Span builder ─────────────────────────────────────────────────────────────

def _build_spans(message: dict, call: dict) -> list[dict]:
    """Build Coval-standard OTel spans from a Vapi end-of-call-report.

    Timing note: Vapi's artifact.messages[].duration is in milliseconds while
    secondsFromStart is in seconds. We derive all span end times from the next
    message's secondsFromStart rather than using the duration field.

    llm.finish_reason: derived from message sequence — "tool_calls" when the
    LLM turn is immediately followed by a tool_calls message, otherwise "stop".

    stt.confidence: emitted as a synthetic 0.95. Vapi does not expose per-
    utterance ASR confidence in the webhook payload. Real-time agents (Pipecat,
    LiveKit) with direct Deepgram access can emit the true value.
    """
    artifact = message.get("artifact", {})
    messages = artifact.get("messages", [])

    started_at = call.get("startedAt") or call.get("createdAt", "")
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        call_start_ns = int(dt.timestamp() * 1_000_000_000)
    except Exception:
        call_start_ns = int(time.time() * 1_000_000_000)

    duration_s = message.get("durationSeconds", 60)
    call_end_ns = call_start_ns + int(duration_s * 1_000_000_000)

    trace_id = uuid.uuid4().hex  # 32 hex chars = 16 bytes = valid OTLP trace ID
    conv_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")

    spans: list[dict] = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("message", "")
        sfs = msg.get("secondsFromStart", 0.0)

        # End at next message's start (avoids the ms-vs-s bug with Vapi's duration field)
        if i + 1 < len(messages):
            end_sfs = messages[i + 1].get("secondsFromStart", sfs + 1.0)
        else:
            end_sfs = duration_s
        end_sfs = max(end_sfs, sfs + 0.05)

        span_start_ns = call_start_ns + int(sfs * 1_000_000_000)
        span_end_ns = call_start_ns + int(end_sfs * 1_000_000_000)
        span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")

        if role == "user":
            dur = end_sfs - sfs
            ttfb = dur * 0.25
            spans.append(_make_span(
                trace_id, span_id, conv_span_id,
                "stt", span_start_ns, span_end_ns,
                {
                    "stt.transcription": content,
                    "metrics.ttfb": round(ttfb, 4),
                    # Synthetic: Vapi does not expose per-utterance ASR confidence
                    # in the end-of-call-report. Real-time agents emit the true value.
                    "stt.confidence": 0.95,
                },
            ))
            # SIM-329: provider sub-spans showing per-provider attempt convention.
            # Demonstrates the pattern for agents with multi-provider fallback chains.
            provider_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")
            spans.append(_make_span(
                trace_id, provider_span_id, span_id,  # parent = stt span
                "stt.provider.vapi", span_start_ns, span_end_ns,
                {
                    "stt.providerName": "vapi",
                    "stt.confidence": 0.95,
                    "metrics.ttfb": round(ttfb, 4),
                },
            ))
            # Provider selection sub-span — records which provider was chosen and why.
            selection_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")
            sel_end_ns = span_start_ns + int(0.01 * 1_000_000_000)  # 10ms selection decision
            spans.append(_make_span(
                trace_id, selection_span_id, span_id,  # parent = stt span
                "stt.provider_selection", span_start_ns, sel_end_ns,
                {
                    "stt.selectedProvider": "vapi",
                    "stt.providerType": "hosted_platform",
                },
            ))

        elif role in ("bot", "assistant"):
            # llm.finish_reason: "tool_calls" if the next message is a tool call, else "stop"
            next_role = messages[i + 1].get("role", "") if i + 1 < len(messages) else ""
            finish_reason = "tool_calls" if next_role == "tool_calls" else "stop"

            # LLM gets first 0.6s of the turn (synthetic — Vapi doesn't expose real LLM timing).
            # Clamp to span_end_ns so very short turns don't produce a negative-duration TTS span.
            llm_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")
            llm_end_ns = min(span_end_ns, span_start_ns + int(0.6 * 1_000_000_000))
            llm_ttfb = round((llm_end_ns - span_start_ns) / 1_000_000_000, 4)

            spans.append(_make_span(
                trace_id, llm_span_id, conv_span_id,
                "llm", span_start_ns, llm_end_ns,
                {"metrics.ttfb": llm_ttfb, "llm.finish_reason": finish_reason},
            ))

            spans.append(_make_span(
                trace_id, span_id, conv_span_id,
                "tts", llm_end_ns, span_end_ns,
                {"metrics.ttfb": 0.1},
            ))

        elif role == "tool_calls":
            # One tool_call span per function invocation in this message
            for tc in msg.get("toolCalls", []):
                fn = tc.get("function", {})
                tc_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")
                spans.append(_make_span(
                    trace_id, tc_span_id, conv_span_id,
                    "tool_call", span_start_ns, span_end_ns,
                    {
                        "tool.name": fn.get("name", "unknown"),
                        "tool.call_id": tc.get("id", ""),
                        "tool.arguments": fn.get("arguments", "{}"),
                    },
                ))

        elif role == "tool_call_result":
            tc_result_span_id = format(uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF, "016x")
            result_str = str(msg.get("result", ""))
            # Detect error responses — mark span ERROR so Transition Hotspots shows failure rate
            tool_error = None
            try:
                result_json = json.loads(result_str)
                if "error" in result_json:
                    tool_error = result_json.get("message", result_json["error"])
            except (json.JSONDecodeError, TypeError):
                pass
            spans.append(_make_span(
                trace_id, tc_result_span_id, conv_span_id,
                "tool_call_result", span_start_ns, span_end_ns,
                {
                    "tool.name": msg.get("name", "unknown"),
                    "tool.call_id": msg.get("toolCallId", ""),
                    "tool.result": result_str[:500],
                },
                error=tool_error,
            ))

    spans.insert(0, _make_span(
        trace_id, conv_span_id, "",
        "conversation", call_start_ns, call_end_ns,
        {
            "call.ended_reason": call.get("endedReason", ""),
            "call.duration_seconds": duration_s,
        },
    ))

    return spans


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook")
async def vapi_webhook(request: Request):
    body = await request.json()
    message = body.get("message", {})
    msg_type = message.get("type", "")
    call = message.get("call", {})
    call_id = call.get("id", "")

    logger.info(f"Vapi webhook: type={msg_type} call={call_id}")

    # Capture simulation ID from any event as early as possible
    if call_id and call_id not in _call_sim_map:
        if sim_id := _extract_sim_id(call):
            _call_sim_map[call_id] = sim_id
            logger.info(f"  Stored sim_id={sim_id} for call={call_id} (event={msg_type})")

    # ── assistant-request ─────────────────────────────────────────────────────
    if msg_type == "assistant-request":
        return JSONResponse({"assistantId": NURSE_BRONANCY_OTEL_ASSISTANT_ID})

    # ── tool-calls ────────────────────────────────────────────────────────────
    elif msg_type == "tool-calls":
        results = []
        tool_list = message.get("toolCallList", [])
        logger.info(f"  tool-calls payload keys: {list(message.keys())} toolCallList count: {len(tool_list)}")
        for tc in tool_list:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError):
                args = {}

            handler = _MOCK_TOOLS.get(name)
            if handler:
                result = handler(args)
                logger.info(f"  Tool call handled: name={name} call_id={tc.get('id', '')}")
            else:
                result = json.dumps({"error": f"Unknown tool: {name}"})
                logger.warning(f"  Unknown tool: {name}")

            results.append({"toolCallId": tc.get("id", ""), "result": result})

        return JSONResponse({"results": results})

    # ── end-of-call-report ────────────────────────────────────────────────────
    elif msg_type == "end-of-call-report":
        sim_id = _call_sim_map.pop(call_id, None)
        if not sim_id:
            logger.warning(f"  No sim_id for call={call_id} — skipping trace export")
            return JSONResponse({})

        spans = _build_spans(message, call)
        n_msgs = len(message.get("artifact", {}).get("messages", []))
        logger.info(f"  Built {len(spans)} spans from {n_msgs} transcript messages")
        _send_spans(spans, sim_id)

    # ── all other events ──────────────────────────────────────────────────────
    return JSONResponse({})


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME, "active_calls": len(_call_sim_map)}
