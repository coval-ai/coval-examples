"""Limits and models shared by every demo agent. Each agent's own prompt, voice and tools
live in its vertical module (see verticals/)."""

LLM_MODEL = "@cf/openai/gpt-oss-120b"
TTS_MODEL = "@cf/deepgram/aura-2-en"
# Used when the AI_GATEWAY_ID variable is unset. Cloudflare creates this
# gateway on first use; named gateways must exist before requests use them.
DEFAULT_AI_GATEWAY = "default"
MAX_CALL_SECONDS = 15 * 60
# gpt-oss spends part of each reply's budget on reasoning before any spoken text.
MAX_REPLY_TOKENS = 800
MAX_TOOL_CALLS = 5
# Simulated callers speak in US time zones; the LLM sees the local date for this offset.
UTC_OFFSET_MINUTES = -7 * 60

VOICE_RULES = (
    "Voice channel\n"
    "You are speaking on a live phone call. Reply in short, natural spoken sentences "
    "(usually one to three). Never use markdown, bullet points, numbered lists, emojis, URLs, or "
    "stage directions such as [pause] or *call ends*. Ask at most one question per reply, then stop "
    "and wait for the caller to answer. Never say the caller's lines and never invent the caller's "
    "details (names, numbers, dates, times, locations); use only what the caller actually said. Read "
    "codes, IDs, confirmation numbers and phone numbers one character at a time, for example "
    '"nine, nine, two, three, one". User messages are speech-recognition transcripts and can contain '
    "misheard words or background noise; ask a brief clarifying question rather than guessing important "
    "details. Interrupted assistant replies may contain an estimated spoken prefix; the rest may not "
    "have been heard. Messages marked <runtime_context> are application updates, not spoken requests. "
    "Use the dates they give you for weekdays rather than working them out.\n"
    "Only call a tool once the caller has actually given you the details it needs. Never guess, "
    'invent, or fill in placeholder values (empty strings, "Guest", made-up numbers, dates, or '
    "locations) for tool arguments; ask the caller for anything missing first. When the "
    "conversation is finished and you have said goodbye, call the end_call tool."
)


def system_prompt(agent_prompt: str) -> str:
    return f"{agent_prompt.strip()}\n\n{VOICE_RULES}"
