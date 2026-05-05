"""
LiveKit voice agent with OpenTelemetry tracing for Coval simulation testing.

Architecture: LiveKit Agents SDK with SIP dispatch. Each call is a LiveKit room
joined by the agent via a dispatch rule keyed on agent_name="livekit-voice-agent".

Tracing: see `coval_tracing.py`. The Coval simulation ID arrives mid-session
as the SIP header `X-Coval-Simulation-Id`, surfaced via participant.attributes
when the caller joins the room. `DynamicCovalExporter` buffers spans until
`set_simulation_id()` is called, then flushes via the standard
`OTLPSpanExporter`. Fallback: `COVAL_SIMULATION_ID` env var (for local testing).

Span schema (Coval conventions):
  stt          stt.transcription, metrics.ttfb, stt.confidence
    └── stt.provider.deepgram    stt.providerName, stt.confidence, metrics.ttfb
  llm          metrics.ttfb, llm.finish_reason, gen_ai.usage.input_tokens,
               gen_ai.usage.output_tokens
  tts          metrics.ttfb

Notes:
  stt.confidence  — synthetic 0.95. LiveKit's metrics API does not expose per-
                    utterance ASR confidence. Real confidence is available if
                    you hook directly into the Deepgram websocket response.
  llm.finish_reason — derived by observing function_tools_executed before the
                      next LLMMetrics event. The pending span approach buffers
                      each LLM span until we know whether tools were called.
"""

import json
import os
import time

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, room_io
from livekit.agents import metrics as agent_metrics
from livekit.agents.voice.events import (
    FunctionToolsExecutedEvent,
    MetricsCollectedEvent,
    UserInputTranscribedEvent,
)
from livekit.plugins import deepgram, noise_cancellation, openai as livekit_openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from opentelemetry import trace as otel_trace

from coval_tracing import setup_coval_tracing

load_dotenv()

# ── Tracing ────────────────────────────────────────────────────────────────────

_coval_exporter = setup_coval_tracing(service_name="livekit-voice-agent")

_stt_tracer = otel_trace.get_tracer("coval.stt")
_llm_tracer = otel_trace.get_tracer("coval.llm")
_tts_tracer = otel_trace.get_tracer("coval.tts")


# ── Healthcare tools ───────────────────────────────────────────────────────────

class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a helpful healthcare voice assistant at Wellness Alliance medical practice.
Help patients with appointments, prescriptions, and lab results.
Keep responses concise and conversational. Be friendly and professional.
You have access to tools — use them when relevant:
- lookup_appointment: find the patient's upcoming appointment
- reschedule_appointment: change the date or time of an appointment
- check_prescription_status: check if a prescription is ready for pickup
- get_lab_results: retrieve lab test results (note: currently offline for maintenance)""",
        )

    # These are mock implementations returning hardcoded data.
    # Replace with your real business logic.
    @function_tool()
    async def lookup_appointment(self, patient_name: str) -> str:
        """Look up a patient's upcoming appointment.

        Args:
            patient_name: The patient's full name
        """
        return json.dumps({
            "appointment_id": "APT-20240407-001",
            "provider": "Dr. Sarah Chen",
            "specialty": "Primary Care",
            "date": "Tuesday, April 7th",
            "time": "9:00 AM",
            "location": "Wellness Alliance — Main Campus, Room 214",
            "preparation": "Please bring your insurance card and arrive 15 minutes early.",
        })

    @function_tool()
    async def reschedule_appointment(self, appointment_id: str, new_date: str) -> str:
        """Reschedule an existing appointment to a new date and time.

        Args:
            appointment_id: The appointment ID to reschedule
            new_date: The new date and time, e.g. 'Thursday, April 10th at 11:00 AM'
        """
        return json.dumps({
            "success": True,
            "appointment_id": appointment_id,
            "new_date": new_date,
            "confirmation_number": "CONF-78412",
            "message": f"Your appointment has been rescheduled to {new_date}. You will receive a confirmation by text.",
        })

    @function_tool()
    async def check_prescription_status(self, medication_name: str) -> str:
        """Check whether a prescription is ready for pickup at the pharmacy.

        Args:
            medication_name: The name of the medication
        """
        return json.dumps({
            "medication": medication_name,
            "status": "Ready for pickup",
            "refills_remaining": 2,
            "pharmacy": "Wellness Alliance Pharmacy — Main Campus",
            "pharmacy_hours": "Monday–Friday 8 AM–6 PM, Saturday 9 AM–2 PM",
            "notes": "Prescription was approved by Dr. Chen and is available now.",
        })

    @function_tool()
    async def get_lab_results(self, patient_id: str) -> str:
        """Retrieve a patient's lab test results.

        Note: Currently offline for maintenance — returns SERVICE_UNAVAILABLE.
        The agent should inform the patient results are unavailable rather than
        fabricating values. Used to test Tool Usage Appropriateness.

        Args:
            patient_id: The patient's ID or name
        """
        return json.dumps({
            "error": "SERVICE_UNAVAILABLE",
            "message": "The lab results portal is currently offline for maintenance. Results cannot be retrieved at this time.",
            "retry_after": "2026-04-01T08:00:00Z",
        })


# ── Agent session setup ────────────────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="livekit-voice-agent")
async def my_agent(ctx: agents.JobContext):
    """
    Called per SIP call. The Coval simulator calls the agent's SIP URI and injects
    X-Coval-Simulation-Id as a SIP header, which LiveKit surfaces as a participant
    attribute on the incoming SIP participant.
    """
    if _coval_exporter:
        _coval_exporter.reset()

    # Check env var first (for local dev / non-SIP testing)
    env_sim_id = os.getenv("COVAL_SIMULATION_ID")
    if env_sim_id and _coval_exporter:
        _coval_exporter.set_simulation_id(env_sim_id)
        print(f"[coval] tracing active from env var: {env_sim_id}")

    def _extract_sim_id_from_participant(participant: rtc.RemoteParticipant) -> None:
        """Extract simulation ID from a participant (SIP or otherwise) and activate tracing."""
        is_sip = (
            participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            or participant.identity.startswith("sip_")
        )
        print(f"[coval] participant joined: identity={participant.identity} kind={participant.kind} is_sip={is_sip}")
        if not is_sip:
            return
        attrs = participant.attributes or {}
        print(f"[coval] SIP participant attrs: {dict(attrs)}")
        sim_id = (
            attrs.get("sip.h.X-Coval-Simulation-Id")
            or attrs.get("X-Coval-Simulation-Id")
            or attrs.get("x-coval-simulation-id")
            or attrs.get("sip.X-Coval-Simulation-Id")
            or attrs.get("sip.x-coval-simulation-id")
        )
        if sim_id and _coval_exporter:
            _coval_exporter.set_simulation_id(sim_id)
            print(f"[coval] tracing active from SIP participant attr: {sim_id}")
        else:
            print(f"[coval] SIP participant joined but no simulation ID found in attrs: {list(attrs.keys())}")

    # Check participants already in the room (SIP caller joins before agent connects).
    print(f"[coval] existing participants: {[p.identity for p in ctx.room.remote_participants.values()]}")
    for _p in ctx.room.remote_participants.values():
        _extract_sim_id_from_participant(_p)

    # Also listen for participants who join after the agent.
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        _extract_sim_id_from_participant(participant)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=livekit_openai.LLM(model="gpt-4o-mini"),
        tts=deepgram.TTS(model="aura-asteria-en"),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    # ── OTel span emission via LiveKit session events ──────────────────────────
    #
    # LiveKit Agents emits metrics via session.on("metrics_collected") for each
    # STT/LLM/TTS service call. We correlate STT timing with transcripts from
    # session.on("user_input_transcribed") and track tool calls via
    # session.on("function_tools_executed") to determine llm.finish_reason.

    _last_transcript: dict = {"text": "", "ts": 0.0}
    _pending_llm: dict = {"ttfb": None, "input_tokens": 0, "output_tokens": 0}

    def _emit_stt_span(ttfb: float, transcript: str) -> None:
        confidence = 0.95  # synthetic — LiveKit metrics don't expose per-utterance confidence
        with _stt_tracer.start_as_current_span("stt") as span:
            span.set_attribute("stt.transcription", transcript)
            span.set_attribute("metrics.ttfb", round(ttfb, 4))
            span.set_attribute("stt.confidence", confidence)
            with _stt_tracer.start_as_current_span("stt.provider.deepgram") as p:
                p.set_attribute("stt.providerName", "deepgram")
                p.set_attribute("stt.confidence", confidence)
                p.set_attribute("metrics.ttfb", round(ttfb, 4))

    def _emit_llm_span(ttfb: float, finish_reason: str, input_tokens: int, output_tokens: int) -> None:
        with _llm_tracer.start_as_current_span("llm") as span:
            span.set_attribute("metrics.ttfb", round(ttfb, 4))
            span.set_attribute("llm.finish_reason", finish_reason)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

    def _flush_pending_llm(finish_reason: str) -> None:
        if _pending_llm["ttfb"] is not None:
            _emit_llm_span(
                _pending_llm["ttfb"],
                finish_reason,
                _pending_llm["input_tokens"],
                _pending_llm["output_tokens"],
            )
            _pending_llm["ttfb"] = None
            _pending_llm["input_tokens"] = 0
            _pending_llm["output_tokens"] = 0

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final:
            _last_transcript["text"] = ev.transcript
            _last_transcript["ts"] = time.time()

    @session.on("metrics_collected")
    def on_metrics(ev: MetricsCollectedEvent) -> None:
        m = ev.metrics

        if isinstance(m, agent_metrics.STTMetrics):
            transcript = _last_transcript.get("text", "")
            _last_transcript["text"] = ""  # clear after use to prevent reuse across turns
            _emit_stt_span(ttfb=m.duration, transcript=transcript)

        elif isinstance(m, agent_metrics.LLMMetrics):
            # Flush any pending LLM span as "stop" before buffering this new one.
            # (If the previous turn called tools, function_tools_executed already
            # flushed it with finish_reason="tool_calls".)
            _flush_pending_llm("stop")
            # Buffer this span — defer emit until we know whether tools follow.
            _pending_llm["ttfb"] = m.ttft
            _pending_llm["input_tokens"] = m.prompt_tokens
            _pending_llm["output_tokens"] = m.completion_tokens

        elif isinstance(m, agent_metrics.TTSMetrics):
            # TTS is playing — the pending LLM span had no tool calls.
            _flush_pending_llm("stop")
            with _tts_tracer.start_as_current_span("tts") as span:
                span.set_attribute("metrics.ttfb", round(m.ttfb, 4))

    @session.on("function_tools_executed")
    def on_function_tools_executed(ev: FunctionToolsExecutedEvent) -> None:
        _flush_pending_llm("tool_calls")

    @session.on("close")
    def on_close(_ev) -> None:
        _flush_pending_llm("stop")

    def _choose_noise_cancellation(params):
        """Select noise cancellation and extract simulation ID from SIP participant."""
        is_sip = (
            params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            or params.participant.identity.startswith("sip_")
        )
        if is_sip:
            attrs = params.participant.attributes or {}
            print(f"[coval] audio input SIP participant attrs: {dict(attrs)}")
            sim_id = (
                attrs.get("sip.h.X-Coval-Simulation-Id")
                or attrs.get("X-Coval-Simulation-Id")
                or attrs.get("x-coval-simulation-id")
                or attrs.get("sip.X-Coval-Simulation-Id")
                or attrs.get("sip.x-coval-simulation-id")
            )
            if sim_id and _coval_exporter:
                _coval_exporter.set_simulation_id(sim_id)
                print(f"[coval] tracing active from audio input SIP attr: {sim_id}")
            return noise_cancellation.BVCTelephony()
        return noise_cancellation.BVC()

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=_choose_noise_cancellation,
            ),
        ),
    )

    await session.generate_reply(
        instructions="Greet the patient warmly and ask how you can help them today."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
