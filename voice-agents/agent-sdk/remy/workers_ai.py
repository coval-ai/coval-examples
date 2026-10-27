"""Pipecat services for the Workers AI models the Cloudflare demo agents use.

Each is a thin adapter over a stock Pipecat service, pointed at Workers AI's REST and WebSocket
APIs with a Cloudflare API token:

| Stage | Model | Adapter over |
|-------|-------|--------------|
| Speech-to-text and turn detection | @cf/deepgram/flux | DeepgramFluxSTTService |
| Language model with tools | @cf/openai/gpt-oss-120b (gpt-oss-20b fallback) | OpenAILLMService |
| Text-to-speech | @cf/deepgram/aura-2-en | DeepgramHttpTTSService |
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

from loguru import logger
from openai.types.chat import ChatCompletionChunk
from pipecat.frames.frames import ErrorFrame, Frame
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.tts import DeepgramHttpTTSService
from pipecat.services.openai.llm import OpenAILLMService

API = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai"

STT_MODEL = "@cf/deepgram/flux"
LLM_MODEL = "@cf/openai/gpt-oss-120b"
# Workers AI capacity varies by hour. When LLM_MODEL sends nothing for FIRST_TOKEN_SECONDS, the
# reply is requested again from this smaller model in the same family.
FALLBACK_LLM_MODEL = "@cf/openai/gpt-oss-20b"
FIRST_TOKEN_SECONDS = 2.0
# The Cloudflare agent's request options. gpt-oss spends part of each reply's token budget on
# reasoning before any spoken text; with the endpoint's smaller default, a reply can run out of
# tokens before it says anything.
REQUEST_OPTIONS = {"max_completion_tokens": 800, "temperature": 0.3, "reasoning_effort": "low"}
TTS_MODEL = "@cf/deepgram/aura-2-en"
# Aura starts each clip with a click and 0.3 to 0.4 s of low-level noise before speech. Pipecat
# synthesizes a reply one sentence at a time, so that padding becomes a pause between sentences.
# It is trimmed to the KEEP_LEADING_SILENCE_SECONDS before speech, which keeps soft consonants.
KEEP_LEADING_SILENCE_SECONDS = 0.12
SPEECH_THRESHOLD = 1000  # 16-bit sample magnitude, about -30 dBFS; the click stays below it


class WorkersAIFluxSTTService(DeepgramFluxSTTService):
    """Deepgram Flux through Workers AI, which speaks the same WebSocket protocol."""

    def __init__(self, *, account_id: str, api_token: str, **kwargs):
        url = API.format(account_id=account_id).replace("https://", "wss://")
        super().__init__(api_key=api_token, url=f"{url}/run/{STT_MODEL}", **kwargs)

    async def _websocket_connect(self, uri: str, **kwargs):
        # Workers AI takes a bearer token where Deepgram takes "Token <key>".
        kwargs["additional_headers"] = {"Authorization": f"Bearer {self._api_key}"}
        return await super()._websocket_connect(uri, **kwargs)


class WorkersAILLMService(OpenAILLMService):
    """gpt-oss through Workers AI's OpenAI-compatible endpoint, with a slow-first-token fallback."""

    def __init__(self, *, account_id: str, api_token: str, system_instruction: str | None = None, **kwargs):
        super().__init__(
            api_key=api_token,
            base_url=f"{API.format(account_id=account_id)}/v1",
            settings=OpenAILLMService.Settings(model=LLM_MODEL, system_instruction=system_instruction),
            **kwargs,
        )

    def build_chat_completion_params(self, params_from_context) -> dict:
        params = {**super().build_chat_completion_params(params_from_context), **REQUEST_OPTIONS}
        # Workers AI rejects an assistant message without string content, which Pipecat omits
        # when the model only called tools.
        params["messages"] = [
            {**message, "content": message.get("content") or ""}
            if message.get("role") == "assistant" and message.get("tool_calls")
            else message
            for message in params["messages"]
        ]
        return params

    async def get_chat_completions(self, context) -> AsyncIterator[ChatCompletionChunk]:
        stream = await super().get_chat_completions(context)
        chunks = stream.__aiter__()
        try:
            first = await asyncio.wait_for(chunks.__anext__(), FIRST_TOKEN_SECONDS)
        except TimeoutError:
            await stream.close()
            logger.info(f"{LLM_MODEL} sent nothing for {FIRST_TOKEN_SECONDS}s; using {FALLBACK_LLM_MODEL}")
            self._settings.model = FALLBACK_LLM_MODEL
            try:
                return await super().get_chat_completions(context)
            finally:
                self._settings.model = LLM_MODEL
        except StopAsyncIteration:
            first = None
        return _prepend(first, chunks, stream)


async def _prepend(first, chunks, stream) -> AsyncGenerator[ChatCompletionChunk, None]:
    try:
        if first is not None:
            yield first
        async for chunk in chunks:
            yield chunk
    finally:
        await stream.close()


class WorkersAIAuraTTSService(DeepgramHttpTTSService):
    """Deepgram Aura-2 through Workers AI. The request body differs from Deepgram's own API."""

    def __init__(self, *, account_id: str, api_token: str, voice: str, **kwargs):
        super().__init__(
            api_key=api_token,
            base_url=API.format(account_id=account_id),
            settings=DeepgramHttpTTSService.Settings(voice=voice),
            **kwargs,
        )

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        body = {
            "text": text,
            "speaker": self._settings.voice,
            "encoding": "linear16",
            "sample_rate": self.sample_rate,
            "container": "none",
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            await self.start_ttfb_metrics()
            async with self._session.post(
                f"{self._base_url}/run/{TTS_MODEL}", headers=headers, json=body
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}: {(await response.text())[:200]}")
                await self.start_tts_usage_metrics(text)
                audio = trim_leading_silence(response.content.iter_chunked(self.chunk_size), self.sample_rate)
                async for frame in self._stream_audio_frames_from_iterator(audio, context_id=context_id):
                    await self.stop_ttfb_metrics()
                    yield frame
        except Exception as e:
            yield ErrorFrame(f"Error getting audio: {e}")


async def trim_leading_silence(chunks: AsyncIterator[bytes], sample_rate: int) -> AsyncIterator[bytes]:
    """Drops PCM16 silence before the first speech, keeping KEEP_LEADING_SILENCE_SECONDS of it."""
    keep = int(sample_rate * KEEP_LEADING_SILENCE_SECONDS) * 2
    pending = bytearray()
    speaking = False
    async for chunk in chunks:
        if speaking:
            yield chunk
            continue
        pending.extend(chunk)
        samples = memoryview(pending)[: len(pending) - len(pending) % 2].cast("h")
        onset = next((i for i, sample in enumerate(samples) if abs(sample) > SPEECH_THRESHOLD), None)
        samples.release()
        if onset is None:
            del pending[: max(0, len(pending) - keep - len(pending) % 2)]
            continue
        speaking = True
        yield bytes(pending[max(0, onset * 2 - keep) :])
    if not speaking and pending:
        yield bytes(pending)
