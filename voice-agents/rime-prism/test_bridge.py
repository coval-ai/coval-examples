import asyncio
import json
import re
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fixture import execute_tool
from server import Bridge, PacedClock, app, session_limit_seconds


def bridge():
    return Bridge(AsyncMock(), AsyncMock(), "test_sim_7942")


async def test_tool_continuation_waits_for_ack_and_done_and_only_once():
    b = bridge()
    await b.tool(
        {
            "call_id": "c1",
            "response_id": "r1",
            "name": "lookup_demo_account",
            "arguments": '{"loan_code":"DEMO-1042"}',
        }
    )
    result_id = b.tool_calls["c1"]["result_event_id"]
    await b.maybe_continue("r1")
    assert b.upstream.send.await_count == 1
    b.response("r1").update(done=True, status="completed")
    await b.maybe_continue("r1")
    assert b.upstream.send.await_count == 1
    b.result_acks.add(result_id)
    await b.maybe_continue("r1")
    await b.maybe_continue("r1")
    assert b.upstream.send.await_count == 2
    assert (
        json.loads(b.upstream.send.call_args.args[0])["response"]["metadata"][
            "prism_parent_response_id"
        ]
        == "r1"
    )


async def test_new_speech_prevents_stale_tool_continuation():
    b = bridge()
    await b.tool(
        {
            "call_id": "c1",
            "response_id": "r1",
            "name": "lookup_demo_account",
            "arguments": "{}",
        }
    )
    b.response("r1").update(done=True, status="completed")
    b.result_acks.add(b.tool_calls["c1"]["result_event_id"])
    await b.interrupt()
    await b.maybe_continue("r1")
    assert b.upstream.send.await_count == 1


async def test_clear_discards_marks_and_reports_acknowledged_audio_only():
    b = bridge()
    b.response("r1").update(item_id="item1", audio_bytes=48000, played_ms=200)
    b.playing = "r1"
    await b.mark("r1", "token1")
    old_mark = b.client.send_json.call_args.args[0]
    await b.interrupt()
    assert not b.pending_marks
    sent = json.loads(b.upstream.send.call_args.args[0])
    assert sent["audio_end_ms"] == 200
    b.client.receive_json.side_effect = [old_mark, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await b.receive_client()
    assert b.report["playback_acks"] == 0
    assert b.upstream.send.await_count == 1


async def test_tool_replay_is_idempotent_and_bad_arguments_are_a_result():
    b = bridge()
    event = {
        "call_id": "c1",
        "response_id": "r1",
        "name": "lookup_demo_account",
        "arguments": "bad json",
    }
    await b.tool(event)
    await b.tool(event)
    assert len(b.report["tools"]) == 1
    assert b.report["tools"][0]["result"]["error"] == "invalid_arguments"
    assert b.upstream.send.await_count == 1


def test_synthetic_handoff_requires_explicit_consent_enum():
    for consent in (None, True, False, "True", "true", "not_confirmed", "yes"):
        assert not execute_tool("create_demo_handoff", {"consent": consent})["ok"]
    assert (
        execute_tool("create_demo_handoff", {"consent": "confirmed"})["account_changed"]
        is False
    )
    assert (
        execute_tool("lookup_demo_account", {"loan_code": "DEMO-500"})["error"]
        == "demo_service_unavailable"
    )


def test_endpoint_and_diagnostics_require_auth(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-secret")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/sessions/unknown").status_code == 401
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
            pass


class FakeUpstream:
    def __init__(self, events):
        self.events = [json.dumps(event) for event in events]
        self.send = AsyncMock()

    def __aiter__(self):
        return self._replay()

    async def _replay(self):
        for event in self.events:
            yield event


def span_attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_paced_clock_repays_oversleep():
    # Every sleep overshoots by 3 ms. The old "period minus elapsed" loop would fall
    # 4.5 s behind over 1500 ticks; the absolute schedule stays within one tick.
    now = [0.0]
    clock = PacedClock(0.04, lambda: now[0])
    for _ in range(1500):
        now[0] += clock.next_delay() + 0.003
    assert abs(now[0] - 1500 * 0.04) < 0.04
    assert clock.resyncs == 0


def test_paced_clock_resyncs_after_long_stall():
    now = [0.0]
    clock = PacedClock(0.04, lambda: now[0], resync_after=1.0)
    now[0] += clock.next_delay()
    now[0] += 2.0  # the event loop stalled
    assert clock.next_delay() == 0.0
    assert clock.resyncs == 1
    assert abs(clock.next_delay() - 0.04) < 1e-9


def test_session_limit_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv("BRIDGE_MAX_SESSION_SECONDS", raising=False)
    assert session_limit_seconds() == 900
    for raw, expected in (("600", 600), ("5", 60), ("99999", 3600), ("x", 900)):
        monkeypatch.setenv("BRIDGE_MAX_SESSION_SECONDS", raw)
        assert session_limit_seconds() == expected


async def test_reply_status_reaches_trace_and_cancellation_is_marked(monkeypatch):
    monkeypatch.delenv("COVAL_API_KEY", raising=False)
    events = []
    for rid, status, reason in (
        ("r1", "completed", "stop"),
        ("r2", "cancelled", "turn_detected"),
        ("r3", "failed", "turn_failed"),
    ):
        events.append({"type": "response.created", "response": {"id": rid}})
        events.append(
            {
                "type": "response.done",
                "response": {
                    "id": rid,
                    "status": status,
                    "status_details": {"reason": reason},
                    "metadata": {},
                },
            }
        )
    b = Bridge(AsyncMock(), FakeUpstream(events), "test_sim_7942")
    await b.receive_prism()
    await asyncio.gather(*list(b.tasks))
    replies = [s for s in b.trace.spans if s["name"] == "prism.response"]
    assert [s["status"] for s in replies] == [
        {"code": 1},
        {"code": 0, "message": "cancelled: turn_detected"},
        {"code": 2},
    ]
    # Coval's trace evidence classifies cancellations with this pattern.
    assert re.search(r"\bcancelled\b", replies[1]["status"]["message"])


async def test_request_id_reaches_report_and_error_spans(monkeypatch):
    monkeypatch.delenv("COVAL_API_KEY", raising=False)
    error = {
        "type": "error",
        "error": {
            "code": "invalid_request",
            "message": "m",
            "scope": "response",
            "correlation_id": "evt_1",
        },
    }
    b = Bridge(AsyncMock(), FakeUpstream([error]), "test_sim_7942", request_id="req-1")
    b.ready.set()
    await b.receive_prism()
    assert b.report["rime_request_id"] == "req-1"
    attrs = span_attrs(next(s for s in b.trace.spans if s["name"] == "prism.error"))
    assert attrs["rime.request_id"] == "req-1"
    assert attrs["rime.correlation_id"] == "evt_1"
