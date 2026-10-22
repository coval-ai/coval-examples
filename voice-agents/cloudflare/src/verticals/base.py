"""What a demo agent is: its prompt, voice, tools, and failure-mode trace enrichment."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"


@dataclass
class ToolOutcome:
    # Extra attributes for this tool's llm_tool_call and workflow spans.
    span_attributes: dict[str, Any] = field(default_factory=dict)
    # (name, attributes) events for the conversation root span.
    root_events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class CallTurn:
    """What was said and when, in epoch seconds on the caller's timeline."""

    role: str
    text: str
    start: float
    end: float


@dataclass
class ToolWindow:
    name: str
    start: float
    end: float
    error_type: str
    arguments: dict
    result: dict


@dataclass
class CallRecord:
    turns: list[CallTurn]
    tools: list[ToolWindow]
    root_event: Callable[[str, dict], None]


@dataclass
class Vertical:
    slug: str
    agent_name: str
    # Prefix for per-tool workflow spans, e.g. "retail.workflow" -> retail.workflow.check_inventory.
    workflow_span_prefix: str
    system_prompt: str
    first_message: str
    voice: str
    # OpenAI-style function schemas, copied verbatim from the live Vapi assistant.
    tools: list[dict]
    # Tool name -> handler(arguments, state) returning a dict (or a JSON string).
    handlers: dict[str, Callable[[dict, dict], Any]]
    tool_timeouts: dict[str, float] = field(default_factory=dict)
    # Failure-mode ground truth for one finished tool call.
    on_tool_result: Callable[[str, dict, dict, dict], ToolOutcome] | None = None
    # Extra conversation-root attributes when the call ends. They override the
    # session defaults (such as workflow.completed) where a source tracer differed.
    finalize: Callable[[dict, CallRecord], dict] | None = None
    # Argument keys containing any of these substrings export as "<redacted>".
    sensitive_argument_markers: tuple[str, ...] = ()
    # Tool error codes this agent's backends use for a dependency outage.
    dependency_errors: frozenset[str] = frozenset({"SERVICE_UNAVAILABLE", "TIMEOUT"})
    # Spoken when the model hangs up without saying goodbye itself.
    end_call_message: str = "Thanks for calling. Goodbye!"
    keyterms: tuple[str, ...] = ()
    max_duration_seconds: int = 600
    silence_timeout_seconds: int = 30


def load_assistant(slug: str) -> dict:
    """Prompt, greeting and tool schemas imported from the live Vapi assistant."""
    return json.loads((DATA / f"{slug}.json").read_text())
