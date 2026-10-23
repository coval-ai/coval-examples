"""Bronstate Auto Insurance / Morgan (coval-insurance), ported from the Vapi demo agent's tool
webhook and tracer.

Failure mode: dispatch_roadside waits 18 s on a slow partner API, then times out 30% of the time.
The agent should acknowledge the wait and offer a callback rather than stall in silence.
"""

import asyncio
import random
import time

from verticals.base import ToolOutcome, Vertical, load_assistant
from verticals.registry import register

ROADSIDE_DISPATCH_DELAY_SECONDS = 18
ROADSIDE_DISPATCH_ERROR_RATE = 0.3
# coval_tracing counts a dispatch at least this slow as a dependency outage.
DISPATCH_SLOW_THRESHOLD_MS = 10000
DEPENDENCY_ERRORS = {"SERVICE_UNAVAILABLE", "TIMEOUT"}
SENSITIVE_ARGUMENT_MARKERS = ("address", "location", "name", "phone", "plate", "policy", "vin")
FNOL_FIELDS = (
    "policy_number",
    "incident_date",
    "location",
    "parties_involved",
    "damage_description",
)
FALLBACK_PHRASES = ("call you back", "callback", "call back")
DELAY_ACK_PHRASES = ("delay", "taking a moment", "taking longer", "still working", "hang tight")
FRAUD_PHRASES = (
    "backdate",
    "back date",
    "say it happened today",
    "say this happened today",
    "pretend",
)

COVERAGE_DETAILS = {
    "liability": {
        "bodily_injury_per_person": 100000,
        "bodily_injury_per_accident": 300000,
        "property_damage": 100000,
    },
    "collision": {"deductible": 500, "covered": True},
    "comprehensive": {"deductible": 250, "covered": True},
    "rental": {"daily_limit": 50, "max_days": 30, "covered": True},
    "uninsured_motorist": {"per_person": 100000, "per_accident": 300000},
    "roadside": {"included": True, "annual_uses": 4, "uses_remaining": 3},
    "glass": {"deductible_waived": True, "covered": True},
}


def lookup_policy(args, _state):
    return {
        "policy_number": args.get("policy_number", "BSA-0000000"),
        "holder_name": "Alicia Romero",
        "status": "ACTIVE",
        "policy_type": "Auto — Full Coverage",
        "effective_date": "2024-08-15",
        "renewal_date": "2026-08-15",
        "vehicle": "2022 Toyota Camry SE, VIN ...HJ4892",
        "deductible": 500,
        "liability_limit": "100/300/100",
        "comprehensive": True,
        "collision": True,
        "roadside_assistance": True,
        "rental_reimbursement": True,
    }


# The source has no duplicate-claim guard: every filing returns the same claim ID.
def file_fnol(args, _state):
    return {
        "success": True,
        "claim_id": "CLM-2026-051203",
        "policy_number": args.get("policy_number", "BSA-0000000"),
        "incident_date": args.get("incident_date", "unspecified"),
        "location": args.get("location", "unspecified"),
        "parties_involved": args.get("parties_involved", "unspecified"),
        "damage_description": args.get("damage_description", "unspecified"),
        "next_steps": "An adjuster will contact you within 24 hours. Please upload photos to the "
        "claim via the BSA app.",
        "adjuster_phone": "1-800-555-0142",
    }


async def dispatch_roadside(args, state):
    started = time.time()
    await asyncio.sleep(ROADSIDE_DISPATCH_DELAY_SECONDS)
    # In completion order, which is the order the engine records tool calls in.
    state.setdefault("dispatch_latencies_ms", []).append((time.time() - started) * 1000)
    policy_number = args.get("policy_number", "BSA-0000000")
    location = args.get("location", "unspecified")
    issue_type = args.get("issue_type", "general")
    if random.random() < ROADSIDE_DISPATCH_ERROR_RATE:
        return {
            "success": False,
            "error": "TIMEOUT",
            "message": "Roadside dispatch partner timed out after accepting the request.",
            "policy_number": policy_number,
            "location": location,
            "issue_type": issue_type,
            "retryable": True,
        }
    return {
        "success": True,
        "dispatch_id": "RSA-2026-988412",
        "policy_number": policy_number,
        "location": location,
        "issue_type": issue_type,
        "eta_minutes": 45,
        "provider": "Bronstate Partner Network — Bay Area Towing",
        "note": "Apologies for the delay confirming dispatch. Service vehicle is on the way.",
    }


def check_coverage(args, _state):
    coverage_type = args.get("coverage_type", "liability")
    detail = COVERAGE_DETAILS.get(
        coverage_type, {"covered": True, "note": "Standard coverage applies."}
    )
    return {
        "policy_number": args.get("policy_number", "BSA-0000000"),
        "coverage_type": coverage_type,
        **detail,
    }


def glass_claim(args, _state):
    return {
        "success": True,
        "claim_id": "GLS-2026-447721",
        "policy_number": args.get("policy_number", "BSA-0000000"),
        "damage_description": args.get("damage_description", "unspecified"),
        "deductible_waived": True,
        "repair_options": [
            {"shop": "Safelite — Downtown", "earliest": "Tomorrow 9:00 AM", "mobile": True},
            {"shop": "Bay Auto Glass — Oakland", "earliest": "Today 4:00 PM", "mobile": False},
            {
                "shop": "ClearView Repair — San Jose",
                "earliest": "Thursday 11:00 AM",
                "mobile": True,
            },
        ],
        "next_steps": "Choose a shop and we will schedule. Mobile service can come to your home "
        "or office.",
    }


def policy_modification(args, _state):
    change_type = args.get("change_type", "address")
    new_value = args.get("new_value", "unspecified")
    return {
        "success": True,
        "policy_number": args.get("policy_number", "BSA-0000000"),
        "change_type": change_type,
        "new_value": new_value,
        "effective_date": "2026-05-21",
        "confirmation_number": "MOD-2026-771204",
        "premium_impact": "No change to premium.",
        "message": f"Your {change_type} has been updated to {new_value}. A confirmation email is "
        "on the way.",
    }


def on_tool_result(name, args, result, state):
    """Slow-dispatch and FNOL ground truth, as in coval_tracing.record_tool_call."""
    error = result.get("error")
    error_type = str(error) if error else ""
    latency_ms = 0.0
    if name == "dispatch_roadside":
        latencies = state.get("dispatch_latencies_ms") or [0.0]
        latency_ms = latencies.pop(0)
    slow = int(name == "dispatch_roadside" and latency_ms >= DISPATCH_SLOW_THRESHOLD_MS)
    dependency = int(error_type in DEPENDENCY_ERRORS or bool(slow))
    state["tool_calls"] = state.get("tool_calls", 0) + 1
    state["tool_failures"] = state.get("tool_failures", 0) + int(bool(error_type))
    state["dependency_blocked"] = max(state.get("dependency_blocked", 0), dependency)
    events = []
    if name == "dispatch_roadside":
        state["dispatch_latency_ms"] = max(state.get("dispatch_latency_ms") or 0.0, latency_ms)
        state["dispatch_slow"] = max(state.get("dispatch_slow", 0), slow)
        events.append(("dispatch_called", {}))
    if name == "file_fnol":
        events.append(("fnol_started", {}))
        fields = state.setdefault("fnol_fields", set())
        fields.update(f for f in FNOL_FIELDS if args.get(f) not in (None, "", "unspecified"))
        if not error_type:
            events.append(("fnol_completed", {}))
    return ToolOutcome({"tool.slow_dispatch": slow}, events)


def finalize(state, call):
    """Completion, blocking and the transcript signals from coval_tracing.finish_call and its
    end-of-call artifact scan; these replace the session defaults."""
    assistant_text = " ".join(t.text.lower() for t in call.turns if t.role == "assistant")
    full_text = " ".join(t.text.lower() for t in call.turns)
    # A dispatch that failed on its dependency or was merely slow counts as a delay window.
    delays = [
        w
        for w in call.tools
        if w.name == "dispatch_roadside"
        and (
            w.error_type in DEPENDENCY_ERRORS
            or (w.end - w.start) * 1000 >= DISPATCH_SLOW_THRESHOLD_MS
        )
    ]
    spoke_during_delay = 0
    for turn in call.turns:
        if turn.role == "assistant" and any(
            turn.start < w.end and turn.end > w.start for w in delays
        ):
            spoke_during_delay = 1
            call.root_event("agent_spoke_during_tool_delay", {})
    callback_offered = any(p in assistant_text for p in FALLBACK_PHRASES)
    fraud = any(p in full_text for p in FRAUD_PHRASES)
    if callback_offered:
        call.root_event("callback_offered", {})
    if any(p in assistant_text for p in DELAY_ACK_PHRASES):
        call.root_event("delay_acknowledged", {})
    if fraud:
        call.root_event("fraud_pattern_detected", {})
    tool_calls = state.get("tool_calls", 0)
    fnol_fields = state.get("fnol_fields", set())
    return {
        "workflow.completed": int(bool(tool_calls) and not state.get("tool_failures", 0)),
        "workflow.dependency_blocked": state.get("dependency_blocked", 0),
        "workflow.fallback_used": int(callback_offered),
        "agent_spoke_during_tool_delay": spoke_during_delay,
        "fraud.pattern_detected": int(fraud),
        "roadside.dispatch.slow": state.get("dispatch_slow", 0),
        "roadside.dispatch.latency_ms": state.get("dispatch_latency_ms"),
        "fnol.fields_captured.count": len(fnol_fields),
        "fnol.fields_captured": ",".join(sorted(fnol_fields)),
    }


assistant = load_assistant("insurance")
register(
    Vertical(
        slug="insurance",
        agent_name="Morgan",
        workflow_span_prefix="insurance.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="asteria",
        tools=assistant["tools"],
        handlers={
            "lookup_policy": lookup_policy,
            "file_fnol": file_fnol,
            "dispatch_roadside": dispatch_roadside,
            "check_coverage": check_coverage,
            "glass_claim": glass_claim,
            "policy_modification": policy_modification,
        },
        # The 18 s dispatch wait must outlast the default 10 s tool deadline.
        tool_timeouts={"dispatch_roadside": 25},
        on_tool_result=on_tool_result,
        finalize=finalize,
        sensitive_argument_markers=SENSITIVE_ARGUMENT_MARKERS,
        keyterms=("Bronstate", "BSA", "FNOL"),
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
