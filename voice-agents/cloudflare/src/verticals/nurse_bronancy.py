"""Nurse Bronancy (Wellness Alliance Medical Group): the tool-less agent behind the cal-demo and
double-dog-bronson-deluxe sandbox keepers (Vapi assistant dd29e891)."""

from verticals.base import Vertical, load_assistant
from verticals.registry import register

assistant = load_assistant("nurse_bronancy")
register(
    Vertical(
        slug="nurse-bronancy",
        agent_name="Nurse Bronancy",
        workflow_span_prefix="healthcare.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="harmonia",
        tools=[],
        handlers={},
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
