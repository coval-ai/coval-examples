import json
import time
from types import SimpleNamespace

from agent.engine import CovalEngine
from tracing import CallTracer, redact_arguments
from verticals.catalog import VERTICALS


def _span(payload, name):
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    return [span for span in spans if span["name"] == name]


def _attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def test_otlp_types_and_parenting():
    tracer = CallTracer("test", **{"agent.name": "Avery"})
    turn = tracer.record("turn", 1.0, 2.0, **{"turn.index": 0})
    tracer.record(
        "llm", 1.0, 1.5, parent=turn, **{"metrics.ttfb": 1, "gen_ai.usage.input_tokens": 9}
    )
    payload = json.loads(tracer.body())
    llm = _span(payload, "llm")[0]
    values = {item["key"]: item["value"] for item in llm["attributes"]}
    assert values["metrics.ttfb"] == {"doubleValue": 1.0}
    assert values["gen_ai.usage.input_tokens"] == {"intValue": 9}
    assert llm["parentSpanId"] == _span(payload, "turn")[0]["spanId"]
    assert _span(payload, "conversation")[0]["parentSpanId"] == ""


def test_redaction():
    exported = json.loads(
        redact_arguments({"account_number": "1001234", "amount": 5}, ("account",))
    )
    assert exported == {"account_number": "<redacted>", "amount": 5}


def _engine(slug="retail"):
    engine = CovalEngine(
        SimpleNamespace(waitUntil=lambda _p: None),
        SimpleNamespace(),
        VERTICALS[slug],
        call_id="call-1",
        org="demo",
    )
    engine.control = SimpleNamespace(generation=1)
    engine.simulation_id = "sim-1"
    return engine


def test_build_trace_covers_turns_models_and_tools():
    engine = _engine()
    now = time.time()
    engine.user_turns = [{"text": "Is BM-2241 in stock?", "start": now, "end": now + 2, "lag": 0.4}]
    engine.stats = {
        "llm": {
            "requests": [
                {
                    "generation": 1,
                    "started_at": now + 2,
                    "elapsed_ms": 700,
                    "outcome": "tools",
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 30},
                },
                {"generation": 1, "started_at": now + 3, "elapsed_ms": 600, "outcome": "emitted"},
            ]
        },
        "tts": {
            "requests": [
                {
                    "generation": 1,
                    "started_at": now + 3.6,
                    "elapsed_ms": 400,
                    "first_audio_ms": 180,
                    "text_characters": 40,
                }
            ]
        },
    }
    engine.assistant_turns = [
        {"text": "It shows in stock.", "interrupted": False, "generation": 1, "end": now + 5}
    ]
    result = {"sku": "BM-2241", "stock_status": "low_confidence", "in_stock": True}
    engine.record_tool("check_inventory", "tc-1", {"sku": "BM-2241"}, result, now + 2.7, now + 2.8)
    engine.record_tool(
        "place_order",
        "tc-2",
        {"sku": "BM-2241", "fulfillment": "ship_to_home"},
        {"success": True},
        now + 4,
        now + 4.1,
    )
    engine.ended_reason = "assistant-ended-call"

    payload = json.loads(engine.build_trace().body())
    assert _attributes(_span(payload, "stt")[0])["metrics.ttfb"] == 0.4
    assert [_attributes(s)["llm.finish_reason"] for s in _span(payload, "llm")] == [
        "tool_calls",
        "stop",
    ]
    assert _attributes(_span(payload, "tts")[0])["metrics.ttfb"] == 0.18
    tools = {
        _attributes(s)["function.name"]: _attributes(s) for s in _span(payload, "llm_tool_call")
    }
    assert tools["check_inventory"]["inventory.oversold"] == 1
    assert tools["place_order"]["inventory.phantom_promise"] == 1
    assert len(_span(payload, "retail.workflow.place_order")) == 1
    root = _attributes(_span(payload, "conversation")[0])
    assert root["coval.simulation_id"] == "sim-1"
    assert root["tool.call.count"] == 2
    assert root["inventory.phantom_promise.count"] == 1
    assert root["call.ended_reason"] == "assistant-ended-call"


def test_dependency_outage_blocks_workflow():
    engine = _engine()
    now = time.time()
    engine.stats = {}
    engine.record_tool(
        "check_inventory",
        "tc-1",
        {"sku": "BM-2241"},
        {"error": "SERVICE_UNAVAILABLE"},
        now,
        now + 0.1,
    )
    root = _attributes(_span(json.loads(engine.build_trace().body()), "conversation")[0])
    assert root["workflow.dependency_blocked"] == 1
    assert root["workflow.completed"] == 0
    assert root["tool.failure.count"] == 1


def test_vertical_enrichment_overrides_defaults():
    engine = _engine("retail")
    engine.stats = {}
    engine.vertical = type(engine.vertical)(**{**engine.vertical.__dict__})
    engine.vertical.on_tool_result = lambda *_: __import__("verticals.base").base.ToolOutcome(
        {"tool.result.count": 4, "tool.dependency_unavailable": 1}
    )
    now = time.time()
    engine.record_tool("find_store", "tc-1", {"zip_code": "98101"}, {"store_id": "98101"}, now, now)
    tool = _attributes(_span(json.loads(engine.build_trace().body()), "llm_tool_call")[0])
    assert tool["tool.result.count"] == 4
    assert tool["tool.dependency_unavailable"] == 1
