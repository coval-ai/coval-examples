"""Rewrites model text into something Aura reads aloud correctly.

gpt-oss sometimes writes stage directions or tool-call JSON as text, and Aura reads a digit run
as a quantity ("ninety-nine thousand..."), so codes are spelled a character at a time.
"""

import re

# "[pauses]", "*Call ends*", "<|end_of_call|>", "(waiting for your answer)", "--- note ---",
# "end of conversation", and tool-call JSON such as {"tool":"end_call","arguments":{}}.
STAGE_DIRECTION = re.compile(
    r"\[[^\]]*\]|\*[^*]+\*|(?:<|&lt;)\|[^|]*\|(?:>|&gt;)|\((?:waiting|awaiting|pause|note)\b[^)]*\)"
    r"|-{2,}[^-]*-{2,}|\bend of (?:the )?(?:call|conversation)\b\W*"
    r"|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
    re.IGNORECASE,
)
# Markdown bold keeps its words; only single-starred text is a stage direction.
BOLD = re.compile(r"\*\*([^*]+)\*\*")
# Codes and IDs with at least four digits ("BWD-20261026-4821", "555-339-2121").
CODE = re.compile(r"\b(?:[A-Za-z]+-)*[A-Za-z]*\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*\b")
LAST_FOUR = re.compile(
    r"\b((?:ending|ends)(?: in| with)?|last four(?: digits)?(?: are| of)?)\s+(\d{4})\b", re.I
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTHS = "January February March April May June July August September October November December".split()
# Unicode hyphens read as words or quantities; Aura reads "-" between digits as a pause. gpt-oss
# also writes no-break spaces inside dates ("Oct\u202f31").
DASHES = str.maketrans(
    {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u00a0": " ", "\u202f": " "}
)


def spell_codes(text: str) -> str:
    def spell(match):
        token = match.group()
        if sum(ch.isdigit() for ch in token) < 4 or (token.isdigit() and len(token) == 4):
            return token  # short numbers and four-digit years read naturally
        parts = token.split("-")
        joiner = ", " if all(part.isdigit() for part in parts) else " dash "
        return joiner.join(" ".join(part) if part.isdigit() or part.isupper() else part for part in parts)

    def iso_date(match):
        year, month, day = (int(group) for group in match.groups())
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return match.group()
        return f"{MONTHS[month - 1]} {day}, {year}"

    text = ISO_DATE.sub(iso_date, text)
    text = LAST_FOUR.sub(lambda m: f"{m.group(1)} {' '.join(m.group(2))}", text)
    return CODE.sub(spell, text)


def speakable(text: str) -> str:
    text = BOLD.sub(r"\1", text.translate(DASHES))
    text = STAGE_DIRECTION.sub(" ", text)
    return re.sub(r"\s{2,}", " ", spell_codes(text)).strip()


async def speakable_transform(text: str, _text_type) -> str:
    """A Pipecat TTS text transform: the voice hears speakable(text), the context keeps the original."""
    return speakable(text)
