"""Authenticated Prism to Coval bridge for synthetic voice evaluations."""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from collections import Counter, OrderedDict

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from fixture import INSTRUCTIONS, TOOLS, execute_tool
from telemetry import Trace

app = FastAPI()
SESSIONS = OrderedDict()
ACTIVE = set()
ENDPOINT = os.getenv("PRISM_ENDPOINT", "wss://prism.api.rime.ai/v1/realtime")
AUDIO_TICK_SECONDS = 0.04  # 1280 bytes of 16 kHz PCM16


def session_limit_seconds():
    """Bounded per-call cap; hardship calls routinely run past four minutes."""
    try:
        value = int(os.getenv("BRIDGE_MAX_SESSION_SECONDS", "900"))
    except ValueError:
        value = 900
    return min(max(value, 60), 3600)


class PacedClock:
    """Absolute send schedule: tick n is due at start + n * period.

    Sleeping for "period minus elapsed" never repays oversleep, so the caller's audio
    fell 0.5-0.9% further behind real time every tick. Here a late tick shortens the
    next one. After a stall longer than resync_after the schedule restarts instead of
    sending a long burst of padding silence.
    """

    def __init__(self, period, now, resync_after=1.0):
        self.period, self.now, self.resync_after = period, now, resync_after
        self.start = now()
        self.ticks = 0
        self.resyncs = 0

    def next_delay(self):
        self.ticks += 1
        delay = self.start + self.ticks * self.period - self.now()
        if delay < -self.resync_after:
            self.start = self.now() - self.ticks * self.period
            self.resyncs += 1
            return 0.0
        return max(0.0, delay)


def authorized(header):
    token = os.getenv("BRIDGE_TOKEN", "")
    return bool(token) and secrets.compare_digest(header or "", "Bearer " + token)


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": "prism",
        "source_revision": os.getenv("SOURCE_REVISION", "local"),
        "fixture_sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
        "active_sessions": len(ACTIVE),
        "max_session_seconds": session_limit_seconds(),
    }


@app.get("/sessions/{simulation_id}")
def session_report(
    simulation_id: str, authorization: str | None = Header(default=None)
):
    if not authorized(authorization):
        raise HTTPException(401)
    if simulation_id not in SESSIONS:
        raise HTTPException(404)
    return SESSIONS[simulation_id]


class Bridge:
    def __init__(self, client, upstream, simulation_id, request_id=None):
        self.client, self.upstream = client, upstream
        self.simulation_id = simulation_id
        self.request_id = request_id  # Rime's upgrade x-request-id; support asks for it
        self.session_id = ""
        self.trace = Trace(simulation_id)
        self.input_audio = bytearray()
        self.responses = {}
        self.tool_calls = {}
        self.result_acks = set()
        self.pending_marks = {}
        self.playback_generation = 0
        self.playing = None
        self.last_speech_stop = None
        self.last_speech_start = None
        self.turn_epoch = 0
        self.report = {
            "simulation_id": simulation_id,
            "source_revision": os.getenv("SOURCE_REVISION", "local"),
            "rime_request_id": request_id,
            "events": [],
            "event_counts": {},
            "tools": [],
            "turns": [],
            "errors": [],
            "playback_acks": 0,
            "clears": 0,
            "trace_exports": self.trace.export_results,
        }
        self.counts = Counter()
        self.ready = asyncio.Event()
        self.export_lock = asyncio.Lock()
        self.tasks = set()

    async def send(self, event):
        event.setdefault("event_id", "evt_" + secrets.token_hex(8))
        await self.upstream.send(json.dumps(event))

    async def client_send(self, event):
        if self.session_id:
            event["session_id"] = self.session_id
        await self.client.send_json(event)

    def record(self, event):
        kind = event.get("type", "unknown")
        self.counts[kind] += 1
        self.report["event_counts"] = dict(self.counts)
        if kind not in {
            "response.audio.delta",
            "response.text.delta",
            "conversation.item.input_audio_transcription.delta",
        }:
            self.report["events"].append({"at_ns": time.time_ns(), **event})
            self.report["events"] = self.report["events"][-1000:]

    def response(self, response_id):
        return self.responses.setdefault(
            response_id,
            {
                "id": response_id,
                "start_ns": time.time_ns(),
                "epoch": self.turn_epoch,
                "audio_bytes": 0,
                "played_ms": 0,
                "text": "",
                "item_id": None,
                "done": False,
                "continued": False,
                "first_audio_ns": None,
            },
        )

    async def flush(self):
        async with self.export_lock:
            await self.trace.export()

    async def run(self):
        receiver = asyncio.create_task(self.receive_prism())
        workers = [receiver]
        try:
            await asyncio.wait_for(self.ready.wait(), 20)
            await self.client_send({"type": "session_ready"})
            await self.send(
                {
                    "type": "response.create",
                    "response": {
                        "prism_instruction": "Greet the caller briefly as Riley from Cedar Demo Lending, say this is a demo, and ask how you can help.",
                        "metadata": {"prism_cause": "proactive"},
                    },
                }
            )
            workers.extend(
                [
                    asyncio.create_task(self.receive_client()),
                    asyncio.create_task(self.send_audio()),
                ]
            )
            done, _ = await asyncio.wait(
                workers,
                timeout=session_limit_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
            if not done:
                self.report["errors"].append({"code": "bridge_max_duration"})
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # Let bounded in-flight exports finish before ending the root; cancellation
            # could lose a batch already removed from the pending buffer.
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            self.trace.span(
                "conversation",
                self.trace.started,
                attrs={
                    "rime.session_id": self.session_id,
                    "rime.request_id": self.request_id,
                    "test.synthetic": True,
                    "prism.model_version_available": False,
                    "bridge.audio_clock_resyncs": self.report.get(
                        "audio_clock_resyncs", 0
                    ),
                },
                parent="",
                span_id=self.trace.root_id,
            )
            await self.flush()
            self.report["complete"] = True

    async def receive_client(self):
        while True:
            event = await self.client.receive_json()
            if event.get("type") == "audio_chunk":
                audio = base64.b64decode(event.get("data", ""), validate=True)
                if len(audio) % 2 or len(self.input_audio) + len(audio) > 32000 * 5:
                    raise ValueError("invalid_or_excessive_input_audio")
                self.input_audio.extend(audio)
            elif event.get("type") == "mark":
                mark = self.pending_marks.pop(event.get("mark", {}).get("name"), None)
                if not mark or mark[0] != self.playback_generation:
                    continue  # Clear echoes pending marks too: it does not mean played.
                _, response_id, played_ms, drain_token = mark
                response = self.response(response_id)
                response["played_ms"] = max(response["played_ms"], played_ms)
                self.report["playback_acks"] += 1
                if drain_token:
                    await self.send(
                        {"type": "prism.playback.drained", "drain_token": drain_token}
                    )
                    response["drained"] = True
                    if self.playing == response_id:
                        self.playing = None
            elif event.get("type") in ("utterance_started", "utterance_end"):
                self.report["events"].append(
                    {
                        "type": "coval." + event["type"],
                        "at_ns": time.time_ns(),
                        "utterance_id": event.get("utterance_id"),
                    }
                )

    async def send_audio(self, clock=None):
        # Silence and caller audio continue during replies. Tool/export work must not
        # stop this clock, and the schedule must not drift behind the caller.
        clock = clock or PacedClock(AUDIO_TICK_SECONDS, asyncio.get_running_loop().time)
        while True:
            chunk = bytes(self.input_audio[:1280])
            del self.input_audio[:1280]
            chunk += bytes(1280 - len(chunk))
            await self.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }
            )
            await asyncio.sleep(clock.next_delay())
            self.report["audio_clock_resyncs"] = clock.resyncs

    async def mark(self, response_id, drain_token=None):
        response = self.response(response_id)
        name = secrets.token_hex(8)
        self.pending_marks[name] = (
            self.playback_generation,
            response_id,
            response["audio_bytes"] / 48,
            drain_token,
        )
        await self.client_send({"type": "mark", "mark": {"name": name}})

    async def interrupt(self):
        self.turn_epoch += 1
        if self.playing is None:
            return
        response = self.response(self.playing)
        self.playback_generation += 1
        self.pending_marks.clear()
        await self.client_send({"type": "clear"})
        self.report["clears"] += 1
        if response["item_id"]:
            await self.send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": response["item_id"],
                    "content_index": 0,
                    "audio_end_ms": int(response["played_ms"]),
                }
            )
        response["interrupted"] = True
        self.playing = None

    async def maybe_continue(self, response_id):
        response = self.response(response_id)
        calls = [c for c in self.tool_calls.values() if c["response_id"] == response_id]
        if not response["done"] or response["continued"] or not calls:
            return
        if any(c["result_event_id"] not in self.result_acks for c in calls):
            return
        if (
            response["epoch"] != self.turn_epoch
            or response.get("status") != "completed"
        ):
            return
        response["continued"] = True
        await self.send(
            {
                "type": "response.create",
                "response": {
                    "metadata": {
                        "prism_cause": "tool_continuation",
                        "prism_parent_response_id": response_id,
                    }
                },
            }
        )

    async def tool(self, event):
        call_id = event["call_id"]
        if call_id in self.tool_calls:
            return
        start = time.time_ns()
        result_event_id = "result_" + secrets.token_hex(8)
        self.tool_calls[call_id] = {**event, "result_event_id": result_event_id}
        try:
            args = json.loads(event["arguments"])
            result = execute_tool(event["name"], args)
        except (ValueError, TypeError):
            args, result = {}, {"ok": False, "error": "invalid_arguments"}
        self.report["tools"].append(
            {
                "call_id": call_id,
                "name": event["name"],
                "arguments": args,
                "result": result,
            }
        )
        self.trace.span(
            "llm_tool_call",
            start,
            attrs={
                "function.name": event["name"],
                "function.arguments": json.dumps(args),
                "function.result": json.dumps(result),
                "tool_call_id": call_id,
                "rime.response_id": event["response_id"],
                "test.synthetic": True,
            },
            failed=not result.get("ok"),
        )
        await self.send(
            {
                "type": "conversation.item.create",
                "event_id": result_event_id,
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            }
        )

    async def receive_prism(self):
        async for raw in self.upstream:
            event = json.loads(raw)
            self.record(event)
            kind = event["type"]
            now = time.time_ns()
            response_id = event.get("response_id") or event.get("response", {}).get(
                "id"
            )
            if kind == "session.created":
                self.session_id = event["session"]["id"]
                self.report["rime_session_id"] = self.session_id
                await self.send(
                    {
                        "type": "session.update",
                        "session": {
                            "voice": "astra",
                            "instructions": INSTRUCTIONS,
                            "tools": TOOLS,
                            "turn_detection": {"interrupt_response": True},
                            "prism_client_caps": ["playback_drained"],
                        },
                    }
                )
            elif kind == "session.updated":
                self.ready.set()
            elif kind == "input_audio_buffer.speech_started":
                self.last_speech_start = now
                await self.interrupt()
            elif kind == "input_audio_buffer.speech_stopped":
                self.last_speech_stop = now
                self.trace.span(
                    "vad",
                    self.last_speech_start or now,
                    now,
                    {
                        "rime.item_id": event.get("item_id"),
                        "rime.audio_end_ms": event.get("audio_end_ms"),
                        "telemetry.timing_basis": "provider_event_receipt",
                    },
                )
            elif kind == "conversation.item.input_audio_transcription.completed":
                self.trace.span(
                    "prism.input_transcription",
                    now,
                    attrs={
                        "transcript": event["transcript"],
                        "rime.item_id": event.get("item_id"),
                        "prism.transcript_role": "display_only_not_reasoning_input",
                    },
                )
            elif kind == "response.created":
                response = self.response(response_id)
                response["start_ns"] = now
                response["speech_stop_ns"] = self.last_speech_stop
            elif (
                kind == "response.output_item.added"
                and event.get("item", {}).get("type") == "message"
            ):
                self.response(response_id)["item_id"] = event["item"]["id"]
            elif kind == "response.text.delta":
                self.response(response_id)["text"] += event["delta"]
            elif kind == "response.audio.delta":
                response = self.response(response_id)
                if response.get("interrupted"):
                    continue
                chunk = base64.b64decode(event["delta"], validate=True)
                if response["first_audio_ns"] is None:
                    response["first_audio_ns"] = now
                self.playing = response_id
                for offset in range(0, len(chunk), 4800):
                    piece = chunk[offset : offset + 4800]
                    response["audio_bytes"] += len(piece)
                    await self.client_send(
                        {
                            "type": "audio_chunk",
                            "data": base64.b64encode(piece).decode(),
                        }
                    )
                    await self.mark(response_id)
            elif kind == "response.function_call_arguments.done":
                await self.tool(event)
            elif kind == "conversation.item.created":
                self.result_acks.add(event.get("prism_request_event_id"))
                for rid in list(self.responses):
                    await self.maybe_continue(rid)
            elif kind == "response.done":
                response = self.response(response_id)
                response.update(done=True, status=event["response"]["status"])
                token = event["response"].get("metadata", {}).get("prism_drain_token")
                if token and not response.get("interrupted"):
                    await self.mark(response_id, token)
                attrs = {
                    "rime.response_id": response_id,
                    "rime.response_status": response["status"],
                    "rime.status_reason": event["response"]
                    .get("status_details", {})
                    .get("reason"),
                    "output": response["text"],
                    "prism.audio_duration_ms": response["audio_bytes"] / 48,
                    "prism.usage_available": False,
                }
                if response["first_audio_ns"]:
                    attrs["prism.response_to_first_audio_ms"] = (
                        response["first_audio_ns"] - response["start_ns"]
                    ) / 1e6
                    if response.get("speech_stop_ns"):
                        attrs["prism.speech_stop_to_first_audio_ms"] = (
                            response["first_audio_ns"] - response["speech_stop_ns"]
                        ) / 1e6
                self.trace.span(
                    "prism.response",
                    response["start_ns"],
                    now,
                    attrs,
                    failed=response["status"] == "failed",
                    cancelled_reason=(attrs["rime.status_reason"] or "unknown")
                    if response["status"] == "cancelled"
                    else None,
                )
                self.report["turns"].append(attrs)
                await self.maybe_continue(response_id)
                task = asyncio.create_task(self.flush())
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            elif kind == "error":
                error = event.get("error", {})
                self.report["errors"].append(error)
                self.trace.span(
                    "prism.error",
                    now,
                    attrs={
                        "error.type": error.get("code"),
                        "error.message": error.get("message"),
                        "rime.correlation_id": error.get("correlation_id"),
                        "rime.scope": error.get("scope"),
                        "rime.session_id": self.session_id,
                        "rime.request_id": self.request_id,
                    },
                    failed=True,
                )
                if error.get("scope") == "session" or not self.ready.is_set():
                    raise RuntimeError("prism_session_error:" + str(error.get("code")))


@app.websocket("/ws")
async def websocket(client: WebSocket):
    if not authorized(client.headers.get("authorization")):
        await client.close(code=1008)
        return
    if len(ACTIVE) >= 3:
        await client.close(code=1013)
        return
    await client.accept()
    simulation_id = None
    registered = False
    bridge = None
    try:
        init = await asyncio.wait_for(client.receive_json(), 10)
        simulation_id = init.get("simulation_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", simulation_id):
            raise ValueError("simulation_id_required")
        if simulation_id in ACTIVE:
            raise ValueError("simulation_already_active")
        ACTIVE.add(simulation_id)
        registered = True
        async with connect(
            ENDPOINT,
            additional_headers={
                "Authorization": "Bearer " + os.environ["RIME_API_KEY"]
            },
            open_timeout=15,
            max_size=1024 * 1024,
        ) as upstream:
            bridge = Bridge(
                client,
                upstream,
                simulation_id,
                request_id=upstream.response.headers.get("x-request-id"),
            )
            SESSIONS[simulation_id] = bridge.report
            while len(SESSIONS) > 50:
                SESSIONS.popitem(last=False)
            await bridge.run()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 -- isolate one external WebSocket session
        if bridge:
            bridge.report["errors"].append(
                {"code": "bridge_error", "class": type(exc).__name__}
            )
        print(
            json.dumps(
                {"simulation_id": simulation_id, "error_class": type(exc).__name__}
            ),
            flush=True,
        )
        with contextlib.suppress(Exception):
            await client.send_json({"type": "error", "error": type(exc).__name__})
    finally:
        if registered:
            ACTIVE.discard(simulation_id)
        with contextlib.suppress(Exception):
            await client.close()
