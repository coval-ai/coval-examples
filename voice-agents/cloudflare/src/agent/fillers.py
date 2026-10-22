"""Brief spoken fillers while tools run or a reply is slow. They are audio only, never history."""

import random

from pipecat.frames.frames import TTSSpeakFrame

# A tool result that arrives during a filler waits for it to finish before the
# next model request, so keep each phrase short.
PHRASES = {
    "get_account": (
        "Let me pull up your account.",
        "One sec, finding your orders.",
        "Let me take a look.",
    ),
    "search_help_center": (
        "Let me check our help center.",
        "One sec, looking that up.",
        "Let me find our policy on that.",
    ),
    # Neutral, because the first cancel_order call only reads the order back.
    "cancel_order": (
        "One sec, pulling that order up.",
        "Okay, one moment.",
    ),
    "open_ticket": (
        "I'll open a ticket for that.",
        "Let me write that up for the team.",
    ),
    "request_store_credit": (
        "Let me send that to the team.",
        "I'll ask a teammate to approve that.",
    ),
}
GENERIC = (
    "One sec, let me check.",
    "Let me look into that.",
)
# Said when the language model is slow to reply, instead of silence.
HOLD_ON = (
    "Sorry, give me a second.",
    "Bear with me one moment.",
)
# Tools that answer before a filler could finish; one would only delay the reply.
SILENT = frozenset()


class Fillers:
    """At most one filler per user turn, without repeating the previous one."""

    def __init__(self):
        self._random = random.Random()
        self._generation = None
        self._last = None

    def pick(self, generation: int, tools: list[str]) -> str | None:
        names = {name for name in tools if name not in SILENT}
        if not names or generation == self._generation:
            return None
        self._generation = generation
        phrases = PHRASES.get(names.pop(), GENERIC) if len(names) == 1 else GENERIC
        self._last = self._random.choice([phrase for phrase in phrases if phrase != self._last])
        return self._last

    def hold_on(self) -> str:
        self._last = self._random.choice([phrase for phrase in HOLD_ON if phrase != self._last])
        return self._last


def speak_fillers(llm, tts, control):
    """Queue a filler when Pipecat starts a tool batch or a reply is slow."""
    fillers = Fillers()

    async def say(text, generation):
        # Fillers stay out of the model context and the visible transcript.
        speech = TTSSpeakFrame(text, append_to_context=False)
        speech.workers_generation = generation
        await tts.queue_frame(speech)

    @llm.event_handler("on_response_slow")
    async def on_response_slow(_service, generation):
        if generation == control.generation:
            await say(fillers.hold_on(), generation)

    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(_service, function_calls):
        # Pipecat runs this handler as a task, so the user may have started
        # another turn. The calls carry the generation that requested them.
        generation = function_calls[0].workers_generation
        text = fillers.pick(generation, [call.function_name for call in function_calls])
        if text and generation == control.generation:
            await say(text, generation)
