"""The system prompt: Remy's instructions, voice-channel rules and today's date."""

from datetime import UTC, datetime, timedelta

from remy.hospitality import PROMPT, UTC_OFFSET_MINUTES

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
    "details. Use the dates listed below for weekdays rather than working them out.\n"
    "Never say you verified, looked up, found, booked, sent or completed anything unless a tool "
    "result in this conversation shows it succeeded; if a tool failed or was never called, say so. "
    "Never promise to hold, call someone, transfer the caller, or follow up later unless a tool does "
    "it; you cannot transfer calls or place anyone on hold, so never say you will. Never state "
    "specials, prices, availability, balances, results or policies that are not in these "
    "instructions or a tool result; say you don't have that information.\n"
    'A number the caller says in pairs means those digits: "twenty-one twenty" is 2120, '
    '"fifty-five oh two" is 5502.\n'
    "Only call a tool once the caller has actually given you the details it needs. Never guess, "
    'invent, or fill in placeholder values (empty strings, "Guest", made-up numbers, dates, or '
    "locations) for tool arguments; ask the caller for anything missing first. When the "
    "conversation is finished and you have said goodbye, call the end_call tool."
)


def date_context(today) -> str:
    """Today's date and the next two weeks, so the model never does weekday arithmetic."""
    days = ", ".join((today + timedelta(days=offset)).strftime("%a %b %-d") for offset in range(1, 15))
    return (
        f"Today is {today.strftime('%A, %B %-d, %Y')} ({today.isoformat()}). "
        f"The next 14 days: {days}. A date the caller gives without a year is its next "
        "occurrence on or after today; never use a date before today."
    )


def system_prompt() -> str:
    today = (datetime.now(UTC) + timedelta(minutes=UTC_OFFSET_MINUTES)).date()
    return f"{PROMPT.strip()}\n\n{VOICE_RULES}\n\n{date_context(today)}"
