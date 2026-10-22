"""Pipecat LLMService backed by the native Workers AI binding."""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from pipecat.adapters.base_llm_adapter import BaseLLMAdapter
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    FunctionCallFromLLM,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings
from pipecat.utils.errors import ErrorCategory
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from agent.speech_history import SpeechHistory
from server.observability import exception_fields, log
from services.inference import InferenceError, run_inference
from services.models import TextReply, ToolReply, WorkersAIModel, reply_from

# After this long without a reply, the agent says a short holding line.
HOLD_ON_SECONDS = 4
# A reply that takes longer gets the fallback line instead of more silence.
LLM_TIMEOUT_SECONDS = 12
# Where a streamed reply is cut into spoken pieces. The Workers build of Pipecat has no
# sentence tokenizer, so the TTS service can't aggregate streamed text itself.
# A sentence ends at punctuation followed by a space, or run straight into a capital letter.
SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?:\s|(?=[A-Z]))")
MIN_SPOKEN_CHARS = 12
# Periods that don't end a sentence ("Dr. Chen", "9 a.m. tomorrow").
ABBREVIATION = re.compile(
    r"(?:\b(?:Dr|Mr|Mrs|Ms|St|Jr|Sr|vs|etc|e\.g|i\.e|a\.m|p\.m)|\b[A-Za-z])\.$"
)
# A spoken reply ends at its first question or after this many sentences; anything the model
# writes past that (often the caller's side of the conversation) is dropped, not voiced.
MAX_SPOKEN_SENTENCES = 6
# Stage directions and control tokens the model sometimes writes: "[pauses]", "*Call ends*",
# "<|end_of_call|>".
STAGE_DIRECTION = re.compile(r"\[[^\]]*\]|\*[^*]+\*|(?:<|&lt;)\|[^|]*\|(?:>|&gt;)")
# Markdown bold ("**Margherita**") keeps its words; only single-starred text is a stage direction.
BOLD = re.compile(r"\*\*([^*]+)\*\*")
# gpt-oss occasionally voices its tool-call channel or a note to itself; drop the whole piece.
INTERNAL = re.compile(
    r"\bcommentary\b|\bto=functions\b|waiting for (?:the )?(?:user|caller)|^\W*end of (?:the )?call\W*$",
    re.IGNORECASE,
)
# Unicode hyphens and dashes ("555‑0142") read as words or quantities; Aura reads "-" digit by digit.
DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-"})
SPEAKABLE = re.compile(r"[A-Za-z0-9]")


class DegenerateReply(ValueError):
    """The model streamed text with no words in it (gpt-oss sometimes emits a run of "!")."""


def date_context(today) -> str:
    """Today's date with its weekday and the next two weeks, so the model never does weekday math."""
    days = ", ".join(
        (today + timedelta(days=offset)).strftime("%a %b %-d") for offset in range(1, 15)
    )
    return (
        f"Today is {today.strftime('%A, %B %-d, %Y')} ({today.isoformat()}). "
        f"The next 14 days: {days}. A date the caller gives without a year is its next "
        "occurrence on or after today; never use a date before today."
    )


class WorkersAIAdapter(BaseLLMAdapter):
    @property
    def id_for_llm_specific_messages(self):
        return "workers-ai"

    def to_provider_tools_format(self, tools_schema):
        return [
            {"type": "function", "function": tool.to_default_dict()}
            for tool in tools_schema.standard_tools
        ]

    def get_messages_for_logging(self, context):
        # Framework debug logging must not retain callers' conversation text.
        return [
            {"role": message.get("role")}
            for message in context.get_messages()
            if isinstance(message, dict)
        ]

    def get_llm_invocation_params(self, context, **kwargs):
        messages = context.get_messages()
        if not messages:
            raise ValueError("A conversation requires at least one message.")
        # Preserve tool_calls and tool_call_id: the assistant aggregator owns
        # these messages and needs them for the next inference after a tool.
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }:
                raise ValueError("This service accepts text and tool messages.")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("Multimodal messages require another LLM service.")
        serialized = json.loads(json.dumps(messages))
        instructions = [
            message.get("content") or "" for message in serialized if message["role"] == "system"
        ]
        # Normalize the request without modifying Pipecat's shared history.
        serialized = [message for message in serialized if message["role"] != "system"]
        if instructions:
            serialized.insert(0, {"role": "system", "content": "\n\n".join(instructions)})
        for message in serialized:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                # Pipecat omits content on tool-only assistant messages. Keep
                # the native binding's text field explicit on the wire.
                message.setdefault("content", "")
        result = {"messages": serialized}
        if isinstance(context.tools, ToolsSchema) and context.tools.standard_tools:
            result["tools"] = self.to_provider_tools_format(context.tools)
        return result


class WorkersAILLM(LLMService):
    """Full replies keep tokenizer dependencies out of the audio pipeline.

    Inference runs in a managed task so incoming speech can interrupt it.
    Function calls use LLMService's normal runner and the shared context pair.
    """

    adapter_class = WorkersAIAdapter

    def __init__(
        self,
        env,
        summary,
        control,
        *,
        model: WorkersAIModel,
        speech_history: SpeechHistory,
        call_id=None,
        gateway=None,
        utc_offset_minutes=0,
    ):
        super().__init__(
            name="WorkersAILLM",
            settings=LLMSettings(model=model.model_id),
            function_call_timeout_secs=10,
            group_parallel_tools=True,
        )
        self.env = env
        self.model = model
        self.summary = summary
        self.control = control
        self.speech_history = speech_history
        self.log = log.bind(call_id=call_id, provider="llm", model=model.model_id)
        self._request_options = {
            "extraHeaders": {"x-session-affinity": call_id or uuid.uuid4().hex}
        }
        if gateway:
            # AI Gateway logs, meters, and can cap spending per customer.
            self._request_options["gateway"] = gateway
        self._request_task = None
        self._closing = False
        self._tool_calls = {}
        self._timezone = timezone(timedelta(minutes=utc_offset_minutes))
        self._last_date = None
        self._text_filter = MarkdownTextFilter(
            MarkdownTextFilter.InputParams(filter_repeated_sequences=False)
        )
        self._register_event_handler("on_response_text", sync=True)
        self._register_event_handler("on_response_slow")

    async def _cancel_request(self):
        task, self._request_task = self._request_task, None
        if task and not task.done() and task is not asyncio.current_task():
            await self.cancel_task(task, timeout=2)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self.speech_history.interrupt(self.control.generation)
            self.control.generation += 1
            self.summary["interruptions"] += 1
            await self._cancel_request()
        elif isinstance(frame, (CancelFrame, EndFrame)):
            self._closing = True
            await self._cancel_request()
        # The base service registers schema handlers and cancels tools on a
        # user interruption. Do this even for upstream tool-result context.
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            if not self._closing:
                await self._cancel_request()
                self.speech_history.start(frame.context, self.control.generation)
                self._request_task = self.create_task(
                    self._complete(frame.context, self.control.generation), "workers-ai-reply"
                )
            return
        await self.push_frame(frame, direction)

    async def _complete(self, context, generation):
        started = time.monotonic()
        # Wall-clock start and token usage feed the call trace (see agent/engine.py).
        record = {"generation": generation, "outcome": "pending", "started_at": time.time()}
        try:
            local_date = datetime.now(self._timezone).date()
            if local_date != self._last_date:
                context.add_message(
                    {
                        "role": "user",
                        "content": f"<runtime_context>\n{date_context(local_date)}\n"
                        "</runtime_context>",
                    }
                )
                self._last_date = local_date
            used = self._tool_calls.get(generation, 0)
            remaining = self.model.max_tool_calls - used
            parameters = self.get_llm_adapter().get_llm_invocation_params(context)
            if remaining <= 0:
                # This instruction belongs to this request, not future user turns.
                parameters["messages"].append(
                    {
                        "role": "user",
                        "content": "<runtime_context>\n"
                        "The tool-call limit for this turn has been reached. "
                        "Answer from the available evidence and explain anything you could not verify.\n"
                        "</runtime_context>",
                    }
                )
            params = self.model.build_request(
                parameters,
                allow_tools=remaining > 0,
            )
            self.summary["requests"].append(record)
            start = LLMFullResponseStartFrame()
            start.workers_generation = generation
            await self.push_frame(start)
            self.control.llm_pending = True
            hold_on = self.create_task(self._hold_on(generation), "workers-ai-hold-on")
            # A streamed text reply has already been sent on to speech as it arrived.
            streamed = getattr(self.model, "streaming", False)
            try:
                if streamed:
                    try:
                        reply, usage = await self._stream(params, generation, record)
                    except DegenerateReply:
                        # One fresh attempt; a second wordless reply falls back below.
                        record["degenerate_retries"] = record.get("degenerate_retries", 0) + 1
                        self.log.warning("llm_degenerate_reply", generation=generation)
                        reply, usage = await self._stream(params, generation, record)
                else:
                    result = await run_inference(
                        self.env.AI,
                        self.model.model_id,
                        params,
                        self._request_options,
                        log=self.log.bind(generation=generation),
                        is_current=lambda: (
                            not self._closing and generation == self.control.generation
                        ),
                        timeout=LLM_TIMEOUT_SECONDS,
                    )
                    usage = self.model.get_usage(result)
                    reply = self.model.parse_response(result)
                record["usage"] = usage
                if usage:
                    self.log.info(
                        "llm_usage",
                        generation=generation,
                        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                        **usage,
                    )
                if isinstance(reply, TextReply):
                    # Preview, speech, and spoken context share the same plain text.
                    text = (await self._text_filter.filter(reply.text)).strip()
                    if not text:
                        raise ValueError("Model returned an empty plain-text reply")
                    reply = TextReply(text)
                if isinstance(reply, ToolReply) and remaining <= 0:
                    raise ValueError("Tool call limit reached")
            except Exception as exc:
                streamed = False
                record["outcome"] = "fallback"
                self.summary["errors"].append({"type": type(exc).__name__})
                fields = exception_fields(exc.__cause__ or exc)
                if isinstance(exc, InferenceError):
                    fields.update(
                        status=exc.status,
                        error_code=exc.category.value,
                        provider_error_code=exc.code,
                    )
                self.log.error("provider_failed", generation=generation, **fields)
                if (
                    isinstance(exc, InferenceError)
                    and (exc.category.is_permanent or exc.category == ErrorCategory.QUOTA)
                    and fields["error_category"] != "context_limit"
                ):
                    record["outcome"] = "error"
                    await self.push_error(
                        "The language model is unavailable.",
                        exception=exc,
                        category=exc.category,
                        force_treat_as_permanent=True,
                    )
                    return
                fallbacks = {
                    "context_limit": "This conversation is too long for the model. Please start a new call.",
                    "reply_limit": "That answer got too long. Could you ask me to focus on one part?",
                }
                reply = TextReply(
                    fallbacks.get(
                        fields["error_category"],
                        "Sorry, I lost my train of thought. Could you say that again?",
                    )
                )
            finally:
                await self.cancel_task(hold_on)
            self.control.llm_pending = False
            if self._closing or generation != self.control.generation:
                self.summary["stale_dropped"] += 1
                return
            if isinstance(reply, ToolReply):
                # Parallel lookups consume the same per-turn budget. Only
                # dispatched calls enter Pipecat's tool-call/result history.
                calls = reply.calls[:remaining]
                self._tool_calls[generation] = used + len(calls)
                functions = [
                    FunctionCallFromLLM(
                        function_name=call.name,
                        tool_call_id=call.id,
                        arguments=call.arguments,
                        context=context,
                    )
                    for call in calls
                ]
                for function in functions:
                    # Pipecat runs on_function_calls_started handlers as tasks,
                    # which can start after the user begins another turn.
                    function.workers_generation = generation
                await self._end_response(generation)
                await self.run_function_calls(functions)
                record["outcome"] = "tools"
                record["tool_count"] = len(functions)
                return
            text = reply.text
            self.speech_history.set_text(generation, text)
            await self._call_event_handler("on_response_text", text, generation)
            if not streamed:
                speech = TTSSpeakFrame(text=text, append_to_context=True)
                speech.workers_generation = generation
                await self.push_frame(speech)
            await self._end_response(generation)
            if record["outcome"] == "pending":
                record["outcome"] = "emitted"
            self.summary["responses"].append({"generation": generation, "characters": len(text)})
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            self.summary["cancelled"] += 1
            raise
        except Exception as exc:
            record["outcome"] = "error"
            self.summary["errors"].append({"type": type(exc).__name__})
            self.log.error("provider_failed", generation=generation, **exception_fields(exc))
            await self.push_error(
                "The language model could not finish this response.",
                exception=exc,
                force_treat_as_permanent=True,
            )
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            self.control.llm_pending = False

    async def _stream(self, params, generation, record):
        """Read a streamed completion, sending text on to speech as it arrives.

        Returns the complete reply (text or tool calls) and its token usage.
        """
        response = await run_inference(
            self.env.AI,
            self.model.model_id,
            params,
            self._request_options,
            log=self.log.bind(generation=generation),
            is_current=lambda: not self._closing and generation == self.control.generation,
            timeout=LLM_TIMEOUT_SECONDS,
            raw=True,
        )
        native = getattr(response, "js_response", response)
        if native.body is None:
            raise InferenceError(ErrorCategory.SERVER)
        reader = native.body.getReader()
        started = time.monotonic()
        buffer = b""
        pending = ""
        finished = False
        text, spoken, calls, usage = [], [], {}, {}
        while not (finished and not calls):
            async with asyncio.timeout(LLM_TIMEOUT_SECONDS):
                part = await reader.read()
            if part.done:
                break
            buffer += part.value.to_bytes()
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                    continue
                try:
                    chunk = json.loads(line[5:])
                except ValueError:
                    continue
                if isinstance(chunk.get("usage"), dict) and chunk["usage"].get("prompt_tokens"):
                    usage = self.model.get_usage(chunk)
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                content = delta.get("content")
                if isinstance(content, str) and content and not finished:
                    if not text:
                        record["first_text_ms"] = round((time.monotonic() - started) * 1000, 1)
                    text.append(content)
                    pending += content
                    if len(pending) >= 24 and not SPEAKABLE.search(pending[-24:]):
                        if not spoken:
                            raise DegenerateReply("Model streamed a reply with no words")
                        # A wordless tail after real sentences ends the reply.
                        pending, finished = "", True
                    while not finished and (
                        match := next(
                            (
                                m
                                for m in SENTENCE_END.finditer(pending)
                                if m.start() >= MIN_SPOKEN_CHARS
                                and not ABBREVIATION.search(pending[: m.start() + 1])
                            ),
                            None,
                        )
                    ):
                        sentence = pending[: match.end()]
                        pending = pending[match.end() :]
                        spoken.append(sentence)
                        await self._speak(sentence, generation)
                        finished = sentence.rstrip().endswith("?") or (
                            len(spoken) >= MAX_SPOKEN_SENTENCES
                        )
                    self.speech_history.set_text(generation, "".join(spoken) + pending)
                for call in delta.get("tool_calls") or []:
                    entry = calls.setdefault(
                        call.get("index", len(calls)),
                        {"id": None, "function": {"name": "", "arguments": ""}},
                    )
                    entry["id"] = call.get("id") or entry["id"]
                    function = call.get("function") or {}
                    entry["function"]["name"] += function.get("name") or ""
                    entry["function"]["arguments"] += function.get("arguments") or ""
                if finished and not calls:
                    break
        if finished:
            # Stop the model; nothing it writes past this point will be spoken.
            try:
                await reader.cancel()
            except Exception:
                pass
        else:
            await self._speak(pending, generation)
            spoken.append(pending)
        tool_calls = [calls[index] for index in sorted(calls)]
        if not tool_calls and not SPEAKABLE.search("".join(spoken)):
            # gpt-oss sometimes ends a turn after tool results with no text at all; retry it.
            raise DegenerateReply("Model streamed an empty reply")
        # The conversation keeps only what was voiced, so a cut-off reply stays cut off.
        return reply_from("".join(spoken), tool_calls or None), usage

    async def _speak(self, piece, generation):
        if INTERNAL.search(piece):
            return
        piece = BOLD.sub(r"\1", piece.translate(DASHES))
        piece = STAGE_DIRECTION.sub("", piece)
        piece = piece.replace("*", "").replace("#", "").replace("`", "")
        if not SPEAKABLE.search(piece) or generation != self.control.generation:
            return
        speech = TTSSpeakFrame(text=piece.strip(), append_to_context=True)
        speech.workers_generation = generation
        await self.push_frame(speech)

    async def _hold_on(self, generation):
        await asyncio.sleep(HOLD_ON_SECONDS)
        await self._call_event_handler("on_response_slow", generation)

    async def _end_response(self, generation):
        frame = LLMFullResponseEndFrame()
        frame.workers_generation = generation
        await self.push_frame(frame)

    async def cleanup(self):
        self._closing = True
        await self._cancel_request()
        await super().cleanup()
