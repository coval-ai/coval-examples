"""
Pipecat voice agent with OpenTelemetry tracing for Coval simulation testing.

Cloud: async def bot(args) is called by Pipecat Cloud base image per session.
       args.room_url and args.token are injected by the platform.
Local: python bot.py — reads DAILY_ROOM_URL / DAILY_TOKEN from .env.local.

Tracing: DynamicCovalExporter buffers spans until simulation_id is known.
         Primary: X-Coval-Simulation-Id SIP header via on_dialin_connected event.
         Fallback: COVAL_SIMULATION_ID env var (set at startup for local testing).

Span schema (SIM-328 + SIM-329 attributes):
  stt          stt.transcription, metrics.ttfb, stt.confidence
    └── stt.provider.deepgram    stt.providerName, stt.confidence, metrics.ttfb
  llm          metrics.ttfb, llm.finish_reason
  tts          metrics.ttfb
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

import requests
from dotenv import load_dotenv
from loguru import logger

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
    FunctionCallsStartedFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.daily.transport import DailyDialinSettings, DailyParams, DailyTransport

load_dotenv(override=True)

SYSTEM_PROMPT = """You are a helpful healthcare voice assistant at Wellness Alliance medical practice.
Help patients with appointments, prescriptions, and lab results.
Keep responses concise and conversational. Be friendly and professional.
You have access to tools — use them when relevant:
- lookup_appointment: find the patient's upcoming appointment
- reschedule_appointment: change the date or time of an appointment
- check_prescription_status: check if a prescription is ready for pickup
- get_lab_results: retrieve lab test results (note: currently offline for maintenance)"""

COVAL_TRACES_ENDPOINT = "https://api.coval.dev/v1/traces"


# ── Tracing ────────────────────────────────────────────────────────────────────

def _span_to_otlp_json(span: ReadableSpan) -> dict:
    """Convert a ReadableSpan to OTLP JSON format (resourceSpans structure)."""

    def attrs(attributes) -> list:
        if not attributes:
            return []
        result = []
        for k, v in attributes.items():
            if isinstance(v, bool):
                result.append({"key": k, "value": {"boolValue": v}})
            elif isinstance(v, int):
                result.append({"key": k, "value": {"intValue": v}})
            elif isinstance(v, float):
                result.append({"key": k, "value": {"doubleValue": v}})
            else:
                result.append({"key": k, "value": {"stringValue": str(v)}})
        return result

    def hex_id(id_int: int, length: int) -> str:
        return format(id_int, f"0{length * 2}x") if id_int else ""

    context = span.context
    span_dict = {
        "traceId": hex_id(context.trace_id, 16) if context else "",
        "spanId": hex_id(context.span_id, 8) if context else "",
        "parentSpanId": hex_id(span.parent.span_id, 8) if span.parent else "",
        "name": span.name,
        "kind": span.kind.value,
        "startTimeUnixNano": str(span.start_time) if span.start_time else "0",
        "endTimeUnixNano": str(span.end_time) if span.end_time else "0",
        "attributes": attrs(span.attributes),
        "status": {"code": span.status.status_code.value, "message": span.status.description or ""},
        "events": [],
        "links": [],
    }

    resource_attrs = attrs(span.resource.attributes) if span.resource else []
    return {
        "resourceSpans": [{
            "resource": {"attributes": resource_attrs},
            "scopeSpans": [{
                "scope": {"name": span.instrumentation_scope.name if span.instrumentation_scope else ""},
                "spans": [span_dict],
            }],
        }]
    }


class _CovalJSONExporter(SpanExporter):
    """Sends spans as OTLP JSON via plain HTTP. Avoids protobuf binary encoding
    issues with API Gateway binary media type configuration."""

    def __init__(self, api_key: str, simulation_id: str, endpoint: str, timeout: int):
        self._api_key = api_key
        self._simulation_id = simulation_id
        self._endpoint = endpoint
        self._timeout = timeout

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            try:
                payload = _span_to_otlp_json(span)
                resp = requests.post(
                    self._endpoint,
                    json=payload,
                    headers={
                        "x-api-key": self._api_key,
                        "X-Simulation-Id": self._simulation_id,
                    },
                    timeout=self._timeout,
                )
                if not resp.ok:
                    logger.error(f"Coval trace export failed {resp.status_code}: {resp.text}")
                    return SpanExportResult.FAILURE
                else:
                    logger.debug(f"Exported span '{span.name}' → {resp.status_code}")
            except Exception as error:
                logger.error(f"Coval trace export exception: {error}")
                return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


class DynamicCovalExporter(SpanExporter):
    """OTLP span exporter that buffers spans until the Coval simulation ID is known.

    Configured before the pipeline starts so Pipecat's conversation root span is
    captured from the start. When set_simulation_id() is called (on SIP dialin),
    all buffered spans are flushed to Coval and subsequent spans export normally.

    reset() clears state between sessions on the same warm process instance.
    """

    def __init__(self, api_key: str, endpoint: str = COVAL_TRACES_ENDPOINT, timeout: int = 30):
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._inner: Optional[_CovalJSONExporter] = None
        self._buffer: list[ReadableSpan] = []

    def reset(self) -> None:
        """Clear state for a new session (called at the start of each bot() invocation)."""
        self._inner = None
        self._buffer.clear()

    def set_simulation_id(self, simulation_id: str) -> None:
        self._inner = _CovalJSONExporter(
            api_key=self._api_key,
            simulation_id=simulation_id,
            endpoint=self._endpoint,
            timeout=self._timeout,
        )
        if self._buffer:
            logger.info(f"Flushing {len(self._buffer)} buffered spans to Coval")
            result = self._inner.export(self._buffer)
            logger.info(f"Buffered span flush result: {result}")
            self._buffer.clear()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._inner:
            return self._inner.export(spans)
        logger.debug(f"Buffering {len(spans)} spans (no simulation_id yet)")
        self._buffer.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._inner:
            return self._inner.force_flush(timeout_millis)
        return True

    def shutdown(self) -> None:
        if self._inner:
            self._inner.shutdown()


# Tracing is set up once per process (Pipecat Cloud reuses warm instances across calls).
# reset() is called at the start of each bot() invocation to clear per-session state.
_coval_exporter: Optional[DynamicCovalExporter] = None


def _init_tracing() -> None:
    global _coval_exporter
    api_key = os.getenv("COVAL_API_KEY")
    if not api_key:
        logger.warning("COVAL_API_KEY not set — tracing disabled")
        return
    _coval_exporter = DynamicCovalExporter(api_key=api_key)
    resource = Resource.create({SERVICE_NAME: "pipecat-voice-agent"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_coval_exporter))
    otel_trace.set_tracer_provider(provider)
    logger.info("Coval tracing initialized")


_init_tracing()


# ── Coval OTel span processors ─────────────────────────────────────────────────


class STTSpanProcessor(FrameProcessor):
    """Emits Coval-standard 'stt' spans for each final STT transcription.

    Span attributes (SIM-328):
      stt.transcription  — the transcribed text (required for STT WER metric)
      metrics.ttfb       — seconds from user started speaking to first result
      stt.confidence     — ASR confidence score (0.0–1.0) from Deepgram result

    Provider sub-span (SIM-329):
      stt.provider.deepgram  — demonstrates per-provider attempt convention
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tracer = otel_trace.get_tracer("coval.stt")
        self._speech_start_time: Optional[float] = None
        self._first_result_time: Optional[float] = None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._speech_start_time = time.time()
            self._first_result_time = None

        elif isinstance(frame, InterimTranscriptionFrame):
            if self._first_result_time is None and self._speech_start_time:
                self._first_result_time = time.time()

        elif isinstance(frame, TranscriptionFrame) and frame.text and frame.text.strip():
            now = time.time()
            if self._first_result_time is None:
                self._first_result_time = now

            ttfb = 0.0
            if self._speech_start_time:
                ttfb = self._first_result_time - self._speech_start_time

            # Extract real confidence from Deepgram result; fall back to synthetic.
            try:
                confidence = float(frame.result.channel.alternatives[0].confidence)
            except (AttributeError, IndexError, TypeError):
                confidence = 0.95  # synthetic fallback

            ttfb_rounded = round(ttfb, 4)

            with self._tracer.start_as_current_span("stt") as span:
                span.set_attribute("stt.transcription", frame.text)
                span.set_attribute("metrics.ttfb", ttfb_rounded)
                span.set_attribute("stt.confidence", confidence)

                # SIM-329: nested provider sub-span demonstrating the convention
                # for agents that use multi-provider STT fallback chains.
                with self._tracer.start_as_current_span("stt.provider.deepgram") as p:
                    p.set_attribute("stt.providerName", "deepgram")
                    p.set_attribute("stt.confidence", confidence)
                    p.set_attribute("metrics.ttfb", ttfb_rounded)

            self._speech_start_time = None
            self._first_result_time = None

        await self.push_frame(frame, direction)


@dataclass
class _LLMTiming:
    """Shared mutable timing state passed between the two LLM span processors."""

    request_start: Optional[float] = None
    first_token_time: Optional[float] = None
    has_tool_call: bool = False  # set by FunctionCallsStartedFrame before LLMFullResponseEndFrame


class LLMPreSpanProcessor(FrameProcessor):
    """Records when an LLM context frame is dispatched (placed before the LLM service).

    Works in tandem with LLMPostSpanProcessor (placed after the LLM service) via a
    shared _LLMTiming instance to compute accurate TTFB.
    """

    def __init__(self, timing: _LLMTiming, **kwargs):
        super().__init__(**kwargs)
        self._timing = timing

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMMessagesFrame) and direction == FrameDirection.DOWNSTREAM:
            self._timing.request_start = time.time()
            self._timing.first_token_time = None
            self._timing.has_tool_call = False
        await self.push_frame(frame, direction)


class LLMPostSpanProcessor(FrameProcessor):
    """Emits Coval-standard 'llm' spans once each LLM turn completes (placed after LLM service).

    Span attributes (SIM-328):
      metrics.ttfb      — seconds from context dispatched to first response token
      llm.finish_reason — "tool_calls" if the LLM invoked tools, otherwise "stop"

    finish_reason is derived by observing FunctionCallsStartedFrame, which is broadcast
    downstream by the LLM service BEFORE LLMFullResponseEndFrame when tools are called.
    """

    def __init__(self, timing: _LLMTiming, **kwargs):
        super().__init__(**kwargs)
        self._timing = timing
        self._tracer = otel_trace.get_tracer("coval.llm")

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            if self._timing.request_start is not None and self._timing.first_token_time is None:
                self._timing.first_token_time = time.time()

        elif isinstance(frame, FunctionCallsStartedFrame):
            # LLM service broadcasts this BEFORE LLMFullResponseEndFrame when tools are invoked.
            self._timing.has_tool_call = True

        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._timing.request_start is not None:
                ttfb = (
                    (self._timing.first_token_time - self._timing.request_start)
                    if self._timing.first_token_time
                    else 0.0
                )
                finish_reason = "tool_calls" if self._timing.has_tool_call else "stop"
                with self._tracer.start_as_current_span("llm") as span:
                    span.set_attribute("metrics.ttfb", round(ttfb, 4))
                    span.set_attribute("llm.finish_reason", finish_reason)
                self._timing.request_start = None
                self._timing.first_token_time = None
                self._timing.has_tool_call = False

        await self.push_frame(frame, direction)


class TTSSpanProcessor(FrameProcessor):
    """Emits Coval-standard 'tts' spans around each TTS synthesis.

    Span attributes:
      metrics.ttfb  — seconds from text sent to first audio byte
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tracer = otel_trace.get_tracer("coval.tts")
        self._request_start: Optional[float] = None
        self._first_audio_time: Optional[float] = None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            # TTS service is about to start synthesising — record request time.
            self._request_start = time.time()
            self._first_audio_time = None

        elif isinstance(frame, TTSStoppedFrame):
            # TTS synthesis complete — emit the span.
            if self._request_start is not None:
                ttfb = (
                    (self._first_audio_time - self._request_start)
                    if self._first_audio_time
                    else 0.0
                )
                with self._tracer.start_as_current_span("tts") as span:
                    span.set_attribute("metrics.ttfb", round(ttfb, 4))
                self._request_start = None
                self._first_audio_time = None

        await self.push_frame(frame, direction)


# ── Tools ──────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_appointment",
            "description": "Look up a patient's upcoming appointment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "The patient's full name",
                    }
                },
                "required": ["patient_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Reschedule an existing appointment to a new date and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "The appointment ID to reschedule",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "The new date and time, e.g. 'Thursday, April 10th at 11:00 AM'",
                    },
                },
                "required": ["appointment_id", "new_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_prescription_status",
            "description": "Check whether a prescription is ready for pickup at the pharmacy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "medication_name": {
                        "type": "string",
                        "description": "The name of the medication",
                    }
                },
                "required": ["medication_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lab_results",
            "description": "Retrieve a patient's lab test results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "The patient's ID or name",
                    }
                },
                "required": ["patient_id"],
            },
        },
    },
]


async def tool_lookup_appointment(function_name, tool_call_id, args, llm, context, result_callback):
    import json
    await result_callback(json.dumps({
        "appointment_id": "APT-20240407-001",
        "provider": "Dr. Sarah Chen",
        "specialty": "Primary Care",
        "date": "Tuesday, April 7th",
        "time": "9:00 AM",
        "location": "Wellness Alliance — Main Campus, Room 214",
        "preparation": "Please bring your insurance card and arrive 15 minutes early.",
    }))


async def tool_reschedule_appointment(function_name, tool_call_id, args, llm, context, result_callback):
    import json
    appt_id = args.get("appointment_id", "APT-20240407-001")
    new_date = args.get("new_date", "Thursday, April 10th at 11:00 AM")
    await result_callback(json.dumps({
        "success": True,
        "appointment_id": appt_id,
        "new_date": new_date,
        "confirmation_number": "CONF-78412",
        "message": f"Your appointment has been rescheduled to {new_date}. You will receive a confirmation by text.",
    }))


async def tool_check_prescription_status(function_name, tool_call_id, args, llm, context, result_callback):
    import json
    medication = args.get("medication_name", "your medication")
    await result_callback(json.dumps({
        "medication": medication,
        "status": "Ready for pickup",
        "refills_remaining": 2,
        "pharmacy": "Wellness Alliance Pharmacy — Main Campus",
        "pharmacy_hours": "Monday–Friday 8 AM–6 PM, Saturday 9 AM–2 PM",
        "notes": "Prescription was approved by Dr. Chen and is available now.",
    }))


async def tool_get_lab_results(function_name, tool_call_id, args, llm, context, result_callback):
    import json
    # Intentionally returns an error — the agent should inform the patient results are
    # unavailable rather than fabricating values. Used to test Tool Usage Appropriateness.
    await result_callback(json.dumps({
        "error": "SERVICE_UNAVAILABLE",
        "message": "The lab results portal is currently offline for maintenance. Results cannot be retrieved at this time.",
        "retry_after": "2026-04-01T08:00:00Z",
    }))


# ── Bot entry point ────────────────────────────────────────────────────────────

async def bot(args: Any) -> None:
    """
    Called by Pipecat Cloud per session (args.room_url + args.token injected by platform).
    Also callable directly for local dev — pass any object with .room_url / .token.
    """
    logger.info(f"Session started: room={args.room_url}")

    if _coval_exporter:
        _coval_exporter.reset()

    # Extract dialin_settings from body (passed by PCC's pinless dial-in webhook)
    body = getattr(args, "body", None) or {}
    dialin_settings = None
    if isinstance(body, dict):
        raw = body.get("dialin_settings")
        if raw:
            dialin_settings = DailyDialinSettings(
                call_id=raw.get("callId") or raw.get("call_id", ""),
                call_domain=raw.get("callDomain") or raw.get("call_domain", ""),
            )
            logger.info(f"Dial-in session: call_id={dialin_settings.call_id}")
            # Daily does not forward custom SIP headers to on_dialin_connected.
            # Extract simulation_id from the sip_headers that the webhook received
            # from Daily's pinless dial-in and passed through in the request body.
            sip_headers_from_body = raw.get("sip_headers") or {}
            if isinstance(sip_headers_from_body, dict):
                sip_sim_id = (
                    sip_headers_from_body.get("X-Coval-Simulation-Id")
                    or sip_headers_from_body.get("x-coval-simulation-id")
                )
                if sip_sim_id and _coval_exporter:
                    _coval_exporter.set_simulation_id(sip_sim_id)
                    logger.info(f"Coval tracing active from body.dialin_settings.sip_headers: {sip_sim_id}")

    transport = DailyTransport(
        args.room_url,
        args.token,
        "Pipecat Healthcare Agent",
        DailyParams(
            api_key=os.getenv("DAILY_API_KEY", ""),
            audio_out_enabled=True,
            transcription_enabled=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            dialin_settings=dialin_settings,
        ),
    )

    # Extract simulation_id from the PCC start request body (Coval passes it in body.coval).
    # This is the primary path for Coval-initiated sessions.
    coval_body = body.get("coval", {}) if isinstance(body, dict) else {}
    body_simulation_id = coval_body.get("simulationOutputId") if isinstance(coval_body, dict) else None
    if body_simulation_id and _coval_exporter:
        _coval_exporter.set_simulation_id(body_simulation_id)
        logger.info(f"Coval tracing active from body.coval.simulationOutputId: {body_simulation_id}")

    # For local testing: if COVAL_SIMULATION_ID is set, activate tracing immediately
    # (on_dialin_connected won't fire for non-SIP connections like direct room joins)
    env_simulation_id = os.getenv("COVAL_SIMULATION_ID")
    if not body_simulation_id and env_simulation_id and _coval_exporter:
        _coval_exporter.set_simulation_id(env_simulation_id)
        logger.info(f"Coval tracing active from env var: simulation_id={env_simulation_id}")

    @transport.event_handler("on_dialin_connected")
    async def on_dialin_connected(transport, data):
        """Extract simulation_id from SIP headers on dial-in connections."""
        logger.info(f"Dialin connected — data: {data}")
        simulation_id = None
        sip_headers = data.get("sipHeaders") or data.get("sip_headers") or {}
        if isinstance(sip_headers, dict):
            simulation_id = (
                sip_headers.get("X-Coval-Simulation-Id")
                or sip_headers.get("x-coval-simulation-id")
            )
            if simulation_id:
                logger.info(f"Got simulation_id from SIP header: {simulation_id}")
        if not simulation_id:
            simulation_id = os.getenv("COVAL_SIMULATION_ID") or None
            if simulation_id:
                logger.info(f"Got simulation_id from env var fallback: {simulation_id}")
        if simulation_id and _coval_exporter:
            _coval_exporter.set_simulation_id(simulation_id)
            logger.info(f"Coval tracing active for simulation_id={simulation_id}")
        else:
            logger.warning("No simulation_id — spans will be discarded")

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-mini")

    llm.register_function("lookup_appointment", tool_lookup_appointment)
    llm.register_function("reschedule_appointment", tool_reschedule_appointment)
    llm.register_function("check_prescription_status", tool_check_prescription_status)
    llm.register_function("get_lab_results", tool_get_lab_results)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = OpenAILLMContext(messages, TOOLS)
    context_aggregator = llm.create_context_aggregator(context)

    llm_timing = _LLMTiming()
    pipeline = Pipeline([
        transport.input(),
        stt,
        STTSpanProcessor(),                       # Emits 'stt' spans: stt.transcription + metrics.ttfb + stt.confidence + stt.provider.deepgram
        context_aggregator.user(),
        LLMPreSpanProcessor(timing=llm_timing),   # Records LLM request start time
        llm,
        LLMPostSpanProcessor(timing=llm_timing),  # Emits 'llm' spans: metrics.ttfb + llm.finish_reason
        tts,
        TTSSpanProcessor(),                       # Emits 'tts' spans: metrics.ttfb
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
        enable_tracing=True,
        enable_turn_tracking=True,
    )

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"Participant joined: {participant.get('id')}")
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(f"Participant left: {participant.get('id')} reason={reason}")
        await task.queue_frames([EndFrame()])

    handle_sigint = getattr(args, "handle_sigint", False)
    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


# ── Local dev entrypoint ───────────────────────────────────────────────────────

if __name__ == "__main__":
    @dataclass
    class _LocalArgs:
        room_url: str
        token: str = ""
        body: Any = None
        session_id: Optional[str] = None
        handle_sigint: bool = True  # handle Ctrl+C locally

    room_url = os.getenv("DAILY_ROOM_URL", "")
    if not room_url:
        raise SystemExit("DAILY_ROOM_URL not set — add it to .env.local")

    _args = _LocalArgs(room_url=room_url, token=os.getenv("DAILY_TOKEN", ""))
    logger.info(f"Local dev — joining room: {room_url}")

    try:
        asyncio.run(bot(_args))
    finally:
        otel_trace.get_tracer_provider().shutdown()
