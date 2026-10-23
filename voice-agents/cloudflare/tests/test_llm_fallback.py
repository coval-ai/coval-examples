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


def test_request_without_question_mark_counts_as_a_question():
    assert llm.REQUEST.search("Could you please provide the address for your Honda CR-V.")
    assert llm.REQUEST.search("What is your policy number.")
    assert not llm.REQUEST.search("I can help with that.")


def test_end_markers_are_not_spoken():
    marked = "Could you confirm the phone number on file for you?--- End of conversation ---"
    assert (
        llm.STAGE_DIRECTION.sub("", marked) == "Could you confirm the phone number on file for you?"
    )


def test_hotel_tools_refuse_past_check_in_dates():
    from verticals import hospitality

    assert hospitality.check_availability({"check_in": "2025-05-06"}, {})["error"] == "DATE_IN_PAST"
    assert hospitality.past_date_error("2099-05-06") is None


def test_speech_normalization_round_five():
    assert llm.spell_codes("Call 1-eight-zero-zero now.") == "Call 1-eight-zero-zero now."
    assert llm.spell_codes("Card ending in 4412.") == "Card ending in 4 4 1 2."
    assert llm.spell_codes("Check-in 2027-07-04.") == "Check-in July 4, 2027."
    assert llm.spell_codes("123 Main Ave NE") == "123 Main Ave Northeast"
    assert llm.spell_codes("ID BWD-20261023-1234.") == (
        "ID B W D dash 2 0 2 6 1 0 2 3 dash 1 2 3 4."
    )
    spoken = 'Have a safe trip. {"tool":"end_call","arguments":{}}'
    assert llm.STAGE_DIRECTION.sub("", spoken).strip() == "Have a safe trip."
