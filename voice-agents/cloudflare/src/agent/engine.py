"""One Coval simulation call: the WebSocket, the Pipecat pipeline, and the call's trace.

Coval connects with its WebSocket (Voice) agent type using the JSON audio preset: it sends
`{"simulation_id": ...}` first, then caller audio as `{"action": "audio_message",
"audio_bytes": <base64 16 kHz PCM16>}`. The agent answers with the same shape at 48 kHz.
"""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

from js import WebSocketPair
from pipecat.frames.frames import TTSSpeakFrame
from pyodide.ffi import create_proxy
from workers import Response, fetch

from agent.config import LLM_MODEL, MAX_CALL_SECONDS, TTS_MODEL, UTC_OFFSET_MINUTES
from agent.pipeline import provider_event, start_pipeline
from agent.resources import AgentResources
from server.observability import exception_fields, log
from services.flux import BYTES_PER_SECOND, FluxBridge
from services.stt import AudioInputBacklog
from tracing import CallTracer, redact_arguments
from verticals.base import CallRecord, CallTurn, ToolOutcome, ToolWindow

# Four seconds of 20 ms Coval audio frames, matching the recognizer's own queue limit.
MAX_QUEUED_FRAMES = 200
MAX_MESSAGE_CHARS = 256 * 1024
# How long a goodbye may take to play before the call hangs up regardless.
HANG_UP_GRACE_SECONDS = 15


class CovalEngine:
    def __init__(self, ctx, env, vertical, *, call_id, org):
        self.ctx, self.env, self.vertical = ctx, env, vertical
        self.call_id, self.org = call_id, org
        self.settings = SimpleNamespace(utc_offset_minutes=UTC_OFFSET_MINUTES)
        self.customer = {"id": org or "coval", "name": ""}
        self.resources = AgentResources(
            env=env, call_id=call_id, owner_id=org or "coval", customer_id=org or "coval"
        )
        self.log = log.bind(call_id=call_id, agent=vertical.slug)
        self.active = False
        self.closed = False
        self.pipeline_ready = False
        self.user_speaking = False
        self.socket = None
        self.listeners = []
        self.tasks = set()
        self.flux = self.worker = self.tts = self.runner = self.runner_task = None
        self.pipeline_input = None
        self.control = None
        self.stats = {}
        self.audio_in = asyncio.Queue(maxsize=MAX_QUEUED_FRAMES)
        self.simulation_id = None
        self.started_at = time.time()
        self.last_activity = time.monotonic()
        self.flux_bytes = 0
        self.hang_up_generation = None
        self.ended_reason = ""
        self.finish_task = None
        # Per-call scratch space for a vertical's failure-mode ground truth.
        self.tool_state = {}
        # What the trace is built from when the call ends.
        self.user_turn_started = None
        self.user_turns = []
        self.assistant_turns = []
        self.tool_calls = []
        self.first_audio = {}
        self.interruptions = 0

    # ---- Owner interface used by the pipeline, tools and output ------------------

    def event(self, kind, **values):
        events = self.stats.setdefault("events", [])
        events.append({"event": kind, "at": round(time.time() - self.started_at, 3)})
        del events[:-160]

    def notify(self, event):
        kind = event.get("type")
        now = time.monotonic()
        if kind == "audio_started":
            self.first_audio.setdefault(event.get("generation"), time.time())
            self.last_activity = now
        elif kind == "assistant_response_finished":
            self.last_activity = now
            if self.hang_up_generation is not None and event.get("generation") == (
                self.hang_up_generation
            ):
                self._stop_soon("assistant-ended-call")
        elif kind == "clear":
            self.interruptions += 1

    def fail_call(self, stage, exc, **details):
        self.log.error("voice_error", stage=stage, **{**exception_fields(exc), **details})
        self._stop_soon("agent-error")

    def background(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        settled = asyncio.get_running_loop().create_future()
        proxy = create_proxy(settled)
        self.ctx.waitUntil(proxy)

        def completed(result):
            self.tasks.discard(result)
            if not settled.done():
                settled.set_result(None)
            proxy.destroy()
            if not result.cancelled() and result.exception() is not None:
                self.fail_call(name, result.exception())

        task.add_done_callback(completed)
        return task

    def publish_pcm(self, pcm_mono48k, generation):
        if not self.active or self.socket is None or self.socket.readyState != 1:
            return False
        message = {
            "action": "audio_message",
            "audio_bytes": base64.b64encode(pcm_mono48k).decode(),
            "sender": "AI",
        }
        try:
            self.socket.send(json.dumps(message))
        except Exception as exc:
            self.fail_call("audio_send", exc)
            return False
        return True

    def on_start_of_turn(self):
        self.user_turn_started = time.time()
        self.last_activity = time.monotonic()

    def on_end_of_turn(self, text, payload):
        end = time.time()
        # Endpointing delay: audio streamed to Flux after the transcribed window closed.
        window_end = payload.get("audio_window_end")
        lag = None
        if isinstance(window_end, (int, float)):
            lag = max(0.0, self.flux_bytes / BYTES_PER_SECOND - window_end)
        if text:
            self.user_turns.append(
                {"text": text, "start": self.user_turn_started or end, "end": end, "lag": lag}
            )
        self.user_turn_started = None
        self.last_activity = time.monotonic()

    def on_user_turn(self, _text):
        self.last_activity = time.monotonic()

    def on_assistant_turn(self, text, interrupted, generation):
        self.assistant_turns.append(
            {"text": text, "interrupted": interrupted, "generation": generation, "end": time.time()}
        )

    def record_tool(self, name, tool_call_id, arguments, result, start, end):
        self.tool_calls.append(
            {
                "name": name,
                "id": tool_call_id,
                "arguments": arguments,
                "result": result,
                "start": start,
                "end": end,
                "generation": self.control.generation if self.control else 0,
            }
        )

    async def hang_up_after(self, text):
        generation = self.control.generation
        self.hang_up_generation = generation
        speech = TTSSpeakFrame(text, append_to_context=True)
        speech.workers_generation = generation
        await self.tts.queue_frame(speech)
        self.background(self._hang_up_deadline(generation), "hang-up-deadline")

    async def _hang_up_deadline(self, generation):
        await asyncio.sleep(HANG_UP_GRACE_SECONDS)
        if self.hang_up_generation == generation:
            self._stop_soon("assistant-ended-call")

    # ---- Coval WebSocket -------------------------------------------------------------

    def accept(self):
        client, server = WebSocketPair.new()
        server.accept()
        self.socket = server
        for name, callback in (
            ("message", self._on_message),
            ("close", lambda _event: self._stop_soon("customer-ended-call")),
            ("error", lambda _event: self._stop_soon("transport-error")),
        ):
            proxy = create_proxy(callback)
            server.addEventListener(name, proxy)
            self.listeners.append((name, proxy))
        self.background(self._start(), "coval-call-start")
        return Response(None, status=101, web_socket=client)

    def _on_message(self, event):
        if not self.active and self.flux is not None:
            return
        data = event.data
        if not isinstance(data, str) or len(data) > MAX_MESSAGE_CHARS:
            return
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        simulation_id = message.get("simulation_id") or message.get("simulation_output_id")
        if isinstance(simulation_id, str) and simulation_id and "{{" not in simulation_id:
            self.simulation_id = self.simulation_id or simulation_id
        audio = message.get("audio_bytes")
        if message.get("action") == "audio_message" and isinstance(audio, str) and audio:
            try:
                self.audio_in.put_nowait(base64.b64decode(audio))
            except asyncio.QueueFull:
                self.fail_call("audio_input_backlog", AudioInputBacklog("Coval input is full"))
            except ValueError:
                pass

    async def _receive_audio(self):
        while self.active:
            pcm = await self.audio_in.get()
            if len(pcm) % 2:
                pcm = pcm[:-1]
            if pcm:
                self.flux_bytes += len(pcm)
                await self.pipeline_input.queue_audio(pcm)

    async def _start(self):
        self.control = SimpleNamespace(
            generation=0, llm_pending=False, tts_pending=False, context_generations={}
        )
        self.stats = {"events": [], "errors": []}
        self.flux = FluxBridge(
            self.env,
            lambda event: provider_event(self, event),
            max_session_seconds=MAX_CALL_SECONDS,
            keyterms=self.vertical.keyterms,
        )
        self.active = True
        await start_pipeline(self)
        await self.flux.start()
        self.background(self._receive_audio(), "coval-audio-input")
        self.background(self._watch(), "coval-call-watchdog")
        # Coval's caller waits for the agent to speak first.
        greeting = TTSSpeakFrame(self.vertical.first_message, append_to_context=True)
        greeting.workers_generation = self.control.generation
        await self.tts.queue_frame(greeting)

    async def _watch(self):
        silence = self.vertical.silence_timeout_seconds
        while self.active:
            await asyncio.sleep(0.5)
            busy = self.user_speaking or self.control.llm_pending or self.control.tts_pending
            if busy:
                self.last_activity = time.monotonic()
            if time.time() - self.started_at > self.vertical.max_duration_seconds:
                self._stop_soon("exceeded-max-duration")
            elif time.monotonic() - self.last_activity > silence:
                self._stop_soon("silence-timed-out")
            elif not self.pipeline_ready or not self.runner_task or self.runner_task.done():
                self._stop_soon("pipeline-stopped")
            elif not self.flux or not self.flux.is_open:
                self._stop_soon("provider-disconnected")

    # ---- Ending the call ---------------------------------------------------------------

    def _stop_soon(self, reason):
        if self.closed or self.finish_task is not None:
            return
        self.ended_reason = reason
        self.finish_task = asyncio.create_task(self._finish(reason), name="coval-call-finish")
        settled = asyncio.get_running_loop().create_future()
        proxy = create_proxy(settled)
        self.ctx.waitUntil(proxy)
        self.finish_task.add_done_callback(lambda _task: settled.done() or settled.set_result(None))

    async def _finish(self, reason):
        self.active = False
        self.log.info("call_finished", reason=reason, simulation_id=self.simulation_id)
        if self.flux:
            try:
                async with asyncio.timeout(4):
                    await self.flux.stop()
            except Exception as exc:
                self.log.warning("provider_cleanup_failed", **exception_fields(exc))
        if self.runner and self.runner_task and not self.runner_task.done():
            try:
                async with asyncio.timeout(10):
                    await self.runner.cancel(reason=reason)
                    await asyncio.shield(self.runner_task)
            except Exception as exc:
                self.log.warning("pipeline_cleanup_failed", **exception_fields(exc))
        current = asyncio.current_task()
        for task in [t for t in self.tasks if t is not current and not t.done()]:
            task.cancel()
        try:
            if self.socket is not None and self.socket.readyState == 1:
                self.socket.close(1000, reason[:120])
        except Exception:
            pass
        for name, proxy in self.listeners:
            try:
                self.socket.removeEventListener(name, proxy)
            except Exception:
                pass
            proxy.destroy()
        self.listeners = []
        self.closed = True
        await self._export_trace()

    # ---- Trace -----------------------------------------------------------------------------

    def _tool_outcome(self, call) -> tuple[str, int, ToolOutcome]:
        result = call["result"]
        error = result.get("error") if isinstance(result, dict) else None
        error_type = error if isinstance(error, str) else ""
        dependency = int(error_type in self.vertical.dependency_errors)
        hook = self.vertical.on_tool_result
        outcome = (
            hook(call["name"], call["arguments"], result, self.tool_state) if hook else None
        ) or ToolOutcome()
        return error_type, dependency, outcome

    def build_trace(self) -> CallTracer:
        vertical = self.vertical
        tracer = CallTracer(
            f"cloudflare-{vertical.slug}-agent",
            **{
                "agent.name": vertical.agent_name,
                "agent.provider": "cloudflare-workers-ai",
                "customer.workflow": vertical.slug,
                "conversation.id": self.call_id,
                "session.id": self.call_id,
                "telephony.provider": "websocket",
                "coval.agent_type": "MODEL_TYPE_WEBSOCKET",
                "coval.correlation.method": "initialization_json",
                "coval.simulation_id": self.simulation_id,
                "llm.model": LLM_MODEL,
                "stt.model": "@cf/deepgram/flux",
                "tts.model": TTS_MODEL,
                "tts.voice": vertical.voice,
            },
        )
        tracer.root.start = self.started_at
        index = 0
        for turn in self.user_turns:
            span = tracer.record(
                "turn",
                turn["start"],
                turn["end"],
                **{
                    "turn.index": index,
                    "turn.role": "user",
                    "turn.user": turn["text"],
                    "turn.text_length": len(turn["text"]),
                    "turn.word_count": len(turn["text"].split()),
                    "turn.duration_seconds": turn["end"] - turn["start"],
                },
            )
            tracer.record(
                "stt",
                turn["start"],
                turn["end"],
                parent=span,
                transcript=turn["text"],
                **{"metrics.ttfb": turn["lag"], "stt.model": "@cf/deepgram/flux"},
            )
            index += 1

        llm_requests = self.stats.get("llm", {}).get("requests", [])
        tts_requests = self.stats.get("tts", {}).get("requests", [])
        by_generation = {}
        for turn in self.assistant_turns:
            by_generation.setdefault(turn["generation"], []).append(turn)
        generations = sorted(
            {r.get("generation") for r in llm_requests + tts_requests if "started_at" in r}
            | set(by_generation)
        )
        assistant_spans = {}
        for generation in generations:
            llm = [
                r for r in llm_requests if r.get("generation") == generation and "started_at" in r
            ]
            tts = [
                r for r in tts_requests if r.get("generation") == generation and "started_at" in r
            ]
            starts = [r["started_at"] for r in llm + tts]
            ends = [r["started_at"] + r.get("elapsed_ms", 0) / 1000 for r in llm + tts]
            texts = by_generation.get(generation, [])
            text = " ".join(t["text"] for t in texts if t["text"])
            start = min(starts) if starts else (texts[0]["end"] if texts else time.time())
            end = max(ends + [t["end"] for t in texts]) if ends or texts else start
            span = tracer.record(
                "turn",
                start,
                end,
                **{
                    "turn.index": index,
                    "turn.role": "assistant",
                    "turn.assistant": text,
                    "turn.text_length": len(text),
                    "turn.duration_seconds": end - start,
                    "turn.interrupted": any(t["interrupted"] for t in texts),
                },
            )
            assistant_spans[generation] = span
            index += 1
            for record in llm:
                elapsed = record.get("elapsed_ms", 0) / 1000
                usage = record.get("usage") or {}
                tracer.record(
                    "llm",
                    record["started_at"],
                    record["started_at"] + elapsed,
                    parent=span,
                    **{
                        "llm.model": LLM_MODEL,
                        "llm.finish_reason": "tool_calls"
                        if record.get("outcome") == "tools"
                        else "stop",
                        # Replies are not streamed, so the first byte is the whole reply.
                        "metrics.ttfb": elapsed,
                        "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
                        "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
                        "llm.outcome": record.get("outcome"),
                    },
                )
            for record in tts:
                first = record.get("first_audio_ms")
                tracer.record(
                    "tts",
                    record["started_at"],
                    record["started_at"] + record.get("elapsed_ms", 0) / 1000,
                    parent=span,
                    **{
                        "tts.text_length": record.get("text_characters"),
                        "metrics.ttfb": first / 1000 if isinstance(first, (int, float)) else None,
                        "tts.model": TTS_MODEL,
                    },
                )

        failures = dependency_blocked = fallback_used = 0
        windows = []
        for call in self.tool_calls:
            error_type, dependency, outcome = self._tool_outcome(call)
            failures += int(bool(error_type))
            dependency_blocked = max(dependency_blocked, dependency)
            if isinstance(call["result"], dict) and call["result"].get("hint") == (
                "stt_mistranscription_likely"
            ):
                fallback_used = 1
            parent = assistant_spans.get(call["generation"])
            common = {
                "tool_call_id": call["id"],
                "tool.error": int(bool(error_type)),
                "tool.result.count": 1,
                "tool.dependency_unavailable": dependency,
            }
            # A vertical's enrichment overrides the defaults (e.g. its own result count).
            span = tracer.record(
                "llm_tool_call",
                call["start"],
                call["end"],
                parent=parent,
                **{
                    **common,
                    "function.name": call["name"],
                    "function.arguments": redact_arguments(
                        call["arguments"], vertical.sensitive_argument_markers
                    ),
                    "tool.latency_ms": (call["end"] - call["start"]) * 1000,
                    **outcome.span_attributes,
                },
            )
            workflow = tracer.record(
                f"{vertical.workflow_span_prefix}.{call['name']}",
                call["start"],
                call["end"],
                parent=parent,
                **{**common, **outcome.span_attributes, "workflow.step": call["name"]},
            )
            if error_type:
                span.fail(error_type)
                workflow.fail(error_type)
            for name, attributes in outcome.root_events:
                tracer.root.event(name, attributes)
            windows.append(
                ToolWindow(
                    call["name"],
                    call["start"],
                    call["end"],
                    error_type,
                    call["arguments"],
                    call["result"],
                )
            )

        blocked = int(bool(dependency_blocked and not fallback_used))
        root = {
            "call.ended_reason": self.ended_reason,
            "call.duration_seconds": time.time() - self.started_at,
            "transcript.turn.count": index,
            "tool.call.count": len(self.tool_calls),
            "tool.failure.count": failures,
            "user.interruption.count": self.interruptions,
            "workflow.completed": int(bool(self.tool_calls) and not blocked),
            "workflow.dependency_blocked": blocked,
            "workflow.fallback_used": fallback_used,
        }
        if vertical.finalize:
            turns = [CallTurn("user", t["text"], t["start"], t["end"]) for t in self.user_turns]
            for turn in self.assistant_turns:
                start = self.first_audio.get(turn["generation"], turn["end"])
                turns.append(CallTurn("assistant", turn["text"], start, turn["end"]))
            record = CallRecord(
                turns, windows, lambda name, attrs=None: tracer.root.event(name, attrs)
            )
            root.update(vertical.finalize(self.tool_state, record) or {})
        tracer.root.set(**root)
        tracer.root.event("conversation_end", {"reason": self.ended_reason})
        tracer.root.finish()
        return tracer

    async def _export_trace(self):
        keys = getattr(self.env, "COVAL_API_KEYS", None)
        try:
            api_key = json.loads(keys).get(self.org) if keys else None
        except ValueError:
            api_key = None
        if not self.simulation_id or not api_key:
            self.log.info(
                "trace_export_skipped", has_simulation=bool(self.simulation_id), org=self.org
            )
            return
        try:
            tracer = self.build_trace()
            async with asyncio.timeout(20):
                response = await fetch(
                    f"{self.env.COVAL_API_BASE}/traces",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "X-Simulation-Id": self.simulation_id,
                    },
                    body=tracer.body(),
                )
            self.log.info(
                "trace_exported",
                status=int(response.status),
                spans=len(tracer.spans),
                simulation_id=self.simulation_id,
            )
        except Exception as exc:
            self.log.warning("trace_export_failed", **exception_fields(exc))
