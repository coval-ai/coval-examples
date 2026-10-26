"""One conversation: a Pipecat pipeline for Remy on a Coval Agent SDK AudioConnection.

    caller audio ─► Flux (speech-to-text + turns) ─► gpt-oss (+ tools) ─► Aura ─► agent audio
          ▲                                                                           │
          └────────────────────── AudioConnection (coval-agent-sdk) ◄─────────────────┘

Pipecat's built-in OpenTelemetry tracing writes the conversation, turn, stt, llm and tts spans.
The root span is named "conversation" and carries conversation.id = the simulation ID, which is
how CovalTraceExporter knows which Coval simulation each trace belongs to. Tool calls add
llm_tool_call spans with each failure mode's ground truth.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import aiohttp
from coval_agent_sdk import AudioConnection
from loguru import logger
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.llm_service import FunctionCallParams, FunctionCallResultProperties

from remy import hospitality
from remy.prompt import system_prompt
from remy.speech import speakable_transform
from remy.transport import SAMPLE_RATE, CovalAudioTransport
from remy.workers_ai import (
    WorkersAIAuraTTSService,
    WorkersAIFluxSTTService,
    WorkersAILLMService,
)

TOOL_TIMEOUT_SECONDS = 10
# How long the pipeline may take to finish after Coval ends the conversation.
SHUTDOWN_GRACE_SECONDS = 10
# Argument keys whose values are not exported in traces.
SENSITIVE_ARGUMENTS = ("member_id", "loyalty_id")

END_CALL = FunctionSchema(
    name="end_call",
    description="Hang up the phone call. Use only after the caller's needs are handled and "
    "you have said goodbye, or if the caller asks to end the call.",
    properties={},
    required=[],
)

tracer = trace.get_tracer("remy")


@dataclass
class Cloudflare:
    account_id: str
    api_token: str

    @classmethod
    def from_env(cls) -> "Cloudflare":
        return cls(os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"])


@dataclass
class CallState:
    """What the silence check-in needs to know about the call."""

    last_activity: float = field(default_factory=time.monotonic)
    someone_speaking: bool = False
    tools_running: int = 0
    ended: bool = False


class ActivityWatch(FrameProcessor):
    """Records when the caller or Remy last spoke."""

    def __init__(self, state: CallState):
        super().__init__(name="ActivityWatch")
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (UserStartedSpeakingFrame, BotStartedSpeakingFrame)):
            self._state.someone_speaking = True
        elif isinstance(frame, (UserStoppedSpeakingFrame, BotStoppedSpeakingFrame)):
            self._state.someone_speaking = False
        if isinstance(
            frame,
            (
                UserStartedSpeakingFrame,
                UserStoppedSpeakingFrame,
                BotStartedSpeakingFrame,
                BotStoppedSpeakingFrame,
            ),
        ):
            self._state.last_activity = time.monotonic()
        await self.push_frame(frame, direction)


def _trace_arguments(arguments: dict) -> str:
    redacted = {
        key: "<redacted>" if any(marker in key for marker in SENSITIVE_ARGUMENTS) else value
        for key, value in arguments.items()
    }
    return json.dumps(redacted)[:2000]


def _conversation_context(task: PipelineTask):
    """The conversation span's context, so tool spans join the conversation's trace."""
    tracing = getattr(task, "_tracing_context", None)
    return tracing.get_conversation_context() if tracing else None


def _tool_handler(name: str, handler, task_ref: list, state: CallState):
    async def execute(params: FunctionCallParams):
        # Models sometimes send the string "null" for an omitted optional field.
        arguments = {k: v for k, v in (params.arguments or {}).items() if v not in (None, "null")}
        started = time.monotonic()
        state.tools_running += 1
        with tracer.start_as_current_span(
            "llm_tool_call", context=_conversation_context(task_ref[0])
        ) as span:
            try:
                with tracer.start_as_current_span(f"hospitality.workflow.{name}") as workflow:
                    result = handler(arguments)
            except Exception:
                logger.exception(f"Tool {name} failed")
                result = {
                    "error": "TOOL_EXCEPTION",
                    "message": "The action failed and did not happen.",
                }
            finally:
                state.tools_running -= 1
            error = result.get("error") or ""
            attributes = {
                "tool_call_id": params.tool_call_id,
                "tool.error": int(bool(error)),
                "tool.result.count": 1,
                "tool.dependency_unavailable": int(error in hospitality.DEPENDENCY_ERRORS),
                **hospitality.outcome_attributes(name, arguments, result),
            }
            span.set_attributes(
                {
                    **attributes,
                    "function.name": name,
                    "function.arguments": _trace_arguments(arguments),
                    "tool.latency_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
            workflow.set_attributes({**attributes, "workflow.step": name})
            if error:
                for failed in (span, workflow):
                    failed.set_status(Status(StatusCode.ERROR, error))
        await params.result_callback(result)

    return execute


def _tool_schemas() -> list[FunctionSchema]:
    schemas = []
    for spec in hospitality.TOOLS:
        parameters = spec.get("parameters") or {}
        schemas.append(
            FunctionSchema(
                name=spec["name"],
                description=spec.get("description", ""),
                properties=parameters.get("properties", {}),
                required=parameters.get("required", []),
            )
        )
    return schemas + [END_CALL]


async def _check_in_on_silence(task: PipelineTask, state: CallState, connection: AudioConnection):
    """Asks once whether the caller is still there after half the silence timeout.

    The Agent SDK has no way for the agent to hang up, so a silent call runs until Coval ends it.
    """
    quiet_limit = hospitality.SILENCE_TIMEOUT_SECONDS / 2
    asked = False
    while not state.ended and not connection.closed:
        await asyncio.sleep(0.5)
        if state.someone_speaking or state.tools_running:
            state.last_activity = time.monotonic()
            asked = False
            continue
        if not asked and time.monotonic() - state.last_activity > quiet_limit:
            asked = True
            await task.queue_frame(TTSSpeakFrame("Are you still there?", append_to_context=True))


async def _stop_after_hang_up(task: PipelineTask, connection: AudioConnection):
    """Backstop: cancel the pipeline if it is still running shortly after Coval ends the call."""
    try:
        await connection.wait_closed()
    except Exception:
        pass  # A failed connection ends the conversation too.
    await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
    await task.cancel()


async def run_conversation(connection: AudioConnection, cloudflare: Cloudflare) -> None:
    """The on_connection handler: runs Remy until Coval ends the conversation."""
    logger.info(f"Conversation {connection.simulation_id} connected")
    state = CallState()
    task_ref: list[PipelineTask] = []

    async with aiohttp.ClientSession() as session:
        transport = CovalAudioTransport(connection)
        stt = WorkersAIFluxSTTService(
            account_id=cloudflare.account_id,
            api_token=cloudflare.api_token,
            settings=DeepgramFluxSTTService.Settings(keyterm=list(hospitality.KEYTERMS), eot_timeout_ms=1500),
        )
        llm = WorkersAILLMService(
            account_id=cloudflare.account_id,
            api_token=cloudflare.api_token,
            system_instruction=system_prompt(),
        )
        tts = WorkersAIAuraTTSService(
            account_id=cloudflare.account_id,
            api_token=cloudflare.api_token,
            voice=hospitality.VOICE,
            aiohttp_session=session,
            text_transforms=[("*", speakable_transform)],
        )

        for name, handler in hospitality.HANDLERS.items():
            llm.register_function(
                name,
                _tool_handler(name, handler, task_ref, state),
                timeout_secs=TOOL_TIMEOUT_SECONDS,
            )

        async def end_call(params: FunctionCallParams):
            # A tool reply has no spoken text, so the goodbye is spoken here. Coval ends the
            # conversation when the caller hangs up.
            await params.result_callback(
                {"success": True}, properties=FunctionCallResultProperties(run_llm=False)
            )
            await task_ref[0].queue_frame(TTSSpeakFrame(hospitality.GOODBYE, append_to_context=True))

        llm.register_function(END_CALL.name, end_call)

        context = LLMContext(tools=ToolsSchema(standard_tools=_tool_schemas()))
        aggregators = LLMContextAggregatorPair(context)

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                aggregators.user(),
                llm,
                tts,
                ActivityWatch(state),
                transport.output(),
                aggregators.assistant(),
            ]
        )
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=SAMPLE_RATE,
                audio_out_sample_rate=SAMPLE_RATE,
                enable_metrics=True,
            ),
            enable_tracing=True,
            # The trace's root span gets conversation.id = this simulation ID.
            conversation_id=connection.simulation_id,
            idle_timeout_secs=None,
        )
        task_ref.append(task)

        @task.event_handler("on_pipeline_started")
        async def greet(_task, _frame):
            await task.queue_frame(TTSSpeakFrame(hospitality.GREETING, append_to_context=True))

        watchers = [
            asyncio.create_task(_check_in_on_silence(task, state, connection)),
            asyncio.create_task(_stop_after_hang_up(task, connection)),
        ]
        try:
            runner = PipelineRunner(handle_sigint=False)
            await runner.add_workers(task)
            await runner.run()
        finally:
            state.ended = True
            for watcher in watchers:
                watcher.cancel()
    # The SDK fails a handler that returns before Coval ends the conversation. Normally the
    # pipeline only stops once it has, but if it stopped early, stay connected silently.
    if not connection.closed:
        logger.warning(f"Pipeline for {connection.simulation_id} stopped before the call ended")
        await connection.wait_closed()
    logger.info(f"Conversation {connection.simulation_id} finished")
