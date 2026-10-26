import asyncio
import random
from datetime import date

import pytest

from remy import hospitality
from remy.prompt import date_context, system_prompt
from remy.speech import speakable
from remy.workers_ai import FALLBACK_LLM_MODEL, LLM_MODEL, WorkersAILLMService, trim_leading_silence


def test_codes_and_dates_are_spoken_a_character_at_a_time():
    assert speakable("Your reservation is BWD-20261026-4821.") == (
        "Your reservation is B W D dash 2 0 2 6 1 0 2 6 dash 4 8 2 1."
    )
    assert speakable("Check-in is 2026-11-03.") == "Check-in is November 3, 2026."
    assert speakable("The card ending in 4242 is on file.") == "The card ending in 4 2 4 2 is on file."
    assert speakable("That's 2 nights in 2026.") == "That's 2 nights in 2026."
    assert speakable("Saturday\u202fOct\u202f31, call 555\u2011339\u20112121.") == (
        "Saturday Oct 31, call 5 5 5, 3 3 9, 2 1 2 1."
    )


def test_stage_directions_and_tool_json_are_not_spoken():
    assert speakable('Goodbye! *call ends* {"tool": "end_call", "arguments": {}}') == "Goodbye!"
    assert speakable("**Deluxe King** it is. [pause]") == "Deluxe King it is."


def test_prompt_includes_voice_rules_and_today():
    prompt = system_prompt()
    assert prompt.startswith(hospitality.PROMPT.strip())
    assert "call the end_call tool" in prompt
    assert "The next 14 days: Tue Oct 27" in date_context(date(2026, 10, 26))


def test_past_dates_are_refused():
    assert hospitality.book_room({"check_in": "2020-01-01"})["error"] == "DATE_IN_PAST"


@pytest.mark.parametrize("seed", range(20))
def test_book_room_failure_modes_keep_their_ground_truth(seed):
    random.seed(seed)
    args = {"room_type": "suite", "check_in": "2099-07-10", "check_out": "2099-07-12"}
    result = hospitality.book_room(args)
    attributes = hospitality.outcome_attributes("book_room", args, result)
    if not result["success"]:
        assert result["error"] == "ROOM_INVENTORY_SYNC_FAILED"
        assert attributes == {}
    else:
        downgraded = result["room_type"] != "suite"
        assert attributes["reservation.room_downgraded"] == int(downgraded)
        if downgraded:
            assert result["room_type"] == "deluxe king"


def test_failure_mode_rates_match_the_cloudflare_agent():
    random.seed(1)
    args = {"room_type": "king", "check_in": "2099-07-10", "check_out": "2099-07-12"}
    results = [hospitality.book_room(args) for _ in range(4000)]
    failed = sum(not r["success"] for r in results) / len(results)
    downgraded = sum(r["success"] and r["room_type"] == "queen" for r in results) / len(results)
    assert failed == pytest.approx(0.25, abs=0.03)
    assert downgraded == pytest.approx(0.75 * 0.4, abs=0.03)


class _Stream:
    def __init__(self, chunks, delay):
        self._chunks, self._delay, self.closed = list(chunks), delay, False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(self._delay)
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("primary_delay, expected", [(0.0, LLM_MODEL), (0.2, FALLBACK_LLM_MODEL)])
def test_slow_first_token_falls_back_to_the_smaller_model(monkeypatch, primary_delay, expected):
    monkeypatch.setattr("remy.workers_ai.FIRST_TOKEN_SECONDS", 0.1)
    llm = WorkersAILLMService(account_id="account", api_token="token")
    streams = []

    async def fake_get(self, context):
        stream = _Stream([self._settings.model], primary_delay if not streams else 0.0)
        streams.append(stream)
        return stream

    monkeypatch.setattr(
        "pipecat.services.openai.base_llm.BaseOpenAILLMService.get_chat_completions", fake_get
    )

    async def collect():
        return [chunk async for chunk in await llm.get_chat_completions(None)]

    assert asyncio.run(collect()) == [expected]
    assert streams[0].closed
    assert llm._settings.model == LLM_MODEL


def test_aura_leading_silence_is_trimmed_to_120_ms():
    click = (700).to_bytes(2, "little", signed=True) * 80
    silence = click + bytes(16000 - len(click))  # 0.5 s at 16 kHz, starting with Aura's click
    speech = (4000).to_bytes(2, "little", signed=True) * 160

    async def chunks():
        for offset in range(0, len(silence), 333):  # odd-sized chunks split samples
            yield silence[offset : offset + 333]
        yield speech

    async def collect():
        return b"".join([chunk async for chunk in trim_leading_silence(chunks(), 16000)])

    audio = asyncio.run(collect())
    assert audio == bytes(3840) + speech
