import asyncio
import json
from types import SimpleNamespace

import services.llm as llm
from services.models import TextReply, get_model


class _Bytes(bytes):
    def to_bytes(self):
        return bytes(self)


class _Reader:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.cancelled = False

    async def read(self):
        if not self.chunks:
            return SimpleNamespace(done=True, value=None)
        return SimpleNamespace(done=False, value=_Bytes(self.chunks.pop(0)))

    async def cancel(self):
        self.cancelled = True


def _sse(text):
    chunk = {"choices": [{"delta": {"content": text}}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


def _service(spoken):
    service = llm.WorkersAILLM.__new__(llm.WorkersAILLM)
    service.env = SimpleNamespace(AI=None)
    service.model = get_model("@cf/openai/gpt-oss-120b")
    service.fallback_model = get_model("@cf/openai/gpt-oss-20b")
    service._request_options = {}
    service._closing = False
    service.control = SimpleNamespace(generation=1)
    service.speech_history = SimpleNamespace(set_text=lambda *_: None)
    service.log = SimpleNamespace(
        bind=lambda **_: service.log, warning=lambda *_, **__: None, info=lambda *_, **__: None
    )

    async def speak(piece, _generation):
        spoken.append(piece)

    service._speak = speak
    return service


async def test_slow_first_token_uses_fallback_model(monkeypatch):
    monkeypatch.setattr(llm, "FIRST_TOKEN_SECONDS", 0.05)
    requested = []

    async def run_inference(_ai, model_id, *_args, **_kwargs):
        requested.append(model_id)
        if model_id == "@cf/openai/gpt-oss-120b":
            await asyncio.sleep(1)
        return SimpleNamespace(
            body=SimpleNamespace(getReader=lambda: _Reader([_sse("Hello there, how can I help?")]))
        )

    monkeypatch.setattr(llm, "run_inference", run_inference)
    spoken, record = [], {}
    reply, _usage = await _service(spoken)._stream_with_fallback({}, 1, record)

    assert requested == ["@cf/openai/gpt-oss-120b", "@cf/openai/gpt-oss-20b"]
    assert record["model"] == record["fallback_model"] == "@cf/openai/gpt-oss-20b"
    assert reply == TextReply("Hello there, how can I help?")
    assert spoken == ["Hello there, how can I help?"]


async def test_prompt_first_token_keeps_primary_model(monkeypatch):
    monkeypatch.setattr(llm, "FIRST_TOKEN_SECONDS", 0.5)
    requested = []

    async def run_inference(_ai, model_id, *_args, **_kwargs):
        requested.append(model_id)
        return SimpleNamespace(
            body=SimpleNamespace(getReader=lambda: _Reader([_sse("Sure, one moment.")]))
        )

    monkeypatch.setattr(llm, "run_inference", run_inference)
    record = {}
    await _service([])._stream_with_fallback({}, 1, record)

    assert requested == ["@cf/openai/gpt-oss-120b"]
    assert record["model"] == "@cf/openai/gpt-oss-120b"
    assert "fallback_model" not in record


def test_codes_are_spelled_out_for_speech():
    assert llm.spell_codes("Your code is RX-CONF-99231.") == (
        "Your code is R X dash C O N F dash 9 9 2 3 1."
    )
    assert llm.spell_codes("Call 555-339-2121.") == "Call 5 5 5, 3 3 9, 2 1 2 1."
    unchanged = "In 2026 on I-280 at 10:00AM, $48.60 for 1,250 points on October 29th."
    assert llm.spell_codes(unchanged) == unchanged
