"""Bron and Sons Italian Eats / Tony (coval-restaurant), ported from the Vapi demo agent's tool
webhook.

Failure modes: book_reservation fails its database write 25% of the time and otherwise silently
shifts the booked date by a day 40% of the time. Only a read-back of the returned date catches it.
"""

import json
import random
import uuid
from datetime import UTC, datetime, timedelta

from verticals.base import ToolOutcome, Vertical, load_assistant
from verticals.registry import register

RESERVATION_DATE_CORRUPTION_PROBABILITY = 0.4
RESERVATION_WRITE_ERROR_RATE = 0.25
# Weekday names always move forward a day, whichever way the shift was drawn (as in the source).
NEXT_DAY = {
    "monday": "tuesday",
    "tuesday": "wednesday",
    "wednesday": "thursday",
    "thursday": "friday",
    "friday": "saturday",
    "saturday": "sunday",
    "sunday": "monday",
}
RESULT_LIST_KEYS = ("items", "results", "availability", "orders", "transactions")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d')}-{random.randint(1000, 9999)}"


def check_availability(args, _state):
    date = args.get("date", "Saturday")
    time = args.get("time", "7:00 PM")
    party_size = args.get("party_size", 2)
    location = args.get("location", "main dining room")
    return {
        "date": date,
        "time": time,
        "party_size": party_size,
        "location": location,
        "available_slots": [
            {"time": time, "table": "Window 4-top", "notes": "quietest spot"},
            {"time": "6:30 PM", "table": "Booth 7", "notes": "kid-friendly"},
            {"time": "7:30 PM", "table": "Private nook", "notes": "best for conversation"},
        ],
        "private_room_available": party_size >= 8,
        "specials_tonight": [
            "Osso buco with saffron risotto",
            "Branzino al cartoccio",
            "House-made pappardelle bolognese",
        ],
    }


def book_reservation(args, _state):
    """Broken by design: the read-back step is the agent's only defence against the date shift."""
    date_requested = args.get("date", "Saturday")
    time = args.get("time", "7:00 PM")
    party_size = args.get("party_size", 2)
    location = args.get("location", "main dining room")
    name = args.get("name", "Guest")
    phone = args.get("phone", "")
    if random.random() < RESERVATION_WRITE_ERROR_RATE:
        return {
            "success": False,
            "error": "RESERVATION_WRITE_FAILED",
            "message": "Reservation database write failed after availability was confirmed.",
            "retryable": True,
            "date": date_requested,
            "time": time,
            "party_size": party_size,
            "location": location,
            "name": name,
            "phone": phone,
        }
    date_returned = date_requested
    if random.random() < RESERVATION_DATE_CORRUPTION_PROBABILITY:
        shift_days = random.choice([-1, 1])
        try:
            parsed = datetime.strptime(date_requested, "%Y-%m-%d")
            date_returned = (parsed + timedelta(days=shift_days)).strftime("%Y-%m-%d")
        except ValueError, TypeError:
            lowered = (date_requested or "").lower()
            for key, shifted in NEXT_DAY.items():
                if key in lowered:
                    date_returned = lowered.replace(key, shifted)
                    break
    return {
        "success": True,
        "reservation_id": _new_id("RES"),
        "date": date_returned,
        "time": time,
        "party_size": party_size,
        "location": location,
        "name": name,
        "phone": phone,
        "confirmation_message": f"Your table for {party_size} is booked under {name} for "
        f"{date_returned} at {time}. We will text a confirmation shortly.",
    }


def modify_reservation(args, _state):
    # Fields the caller didn't change stay as booked; the webhook's defaults (Friday, 7 PM, two
    # guests) silently rewrote them when the model left them out.
    return {
        "success": True,
        "reservation_id": args.get("reservation_id", "RES-UNKNOWN"),
        "new_date": args.get("new_date") or "unchanged",
        "new_time": args.get("new_time") or "unchanged",
        "new_party_size": args.get("new_party_size") or "unchanged",
        "message": "Your reservation has been updated. You'll receive a fresh confirmation by "
        "text.",
    }


def cancel_reservation(args, _state):
    return {
        "success": True,
        "reservation_id": args.get("reservation_id", "RES-UNKNOWN"),
        "message": "Your reservation has been cancelled. We hope to see you another time.",
    }


def place_takeout_order(args, _state):
    pickup_time = args.get("pickup_time", "30 minutes")
    return {
        "success": True,
        "order_id": _new_id("ORD"),
        "items": args.get("items", []),
        "pickup_time": pickup_time,
        "ready_at": pickup_time,
        "name": args.get("name", "Guest"),
        "phone": args.get("phone", ""),
        "estimated_total": "$0.00 — paid at pickup",
        "message": "Your takeout order is in. Please come to the host stand at pickup time.",
    }


def note_dietary_restriction(args, _state):
    return {
        "success": True,
        "reservation_id": args.get("reservation_id", "RES-UNKNOWN"),
        "restriction_recorded": args.get("restriction_text", ""),
        "message": "I've flagged this on your reservation and notified the kitchen.",
    }


def escalate_to_manager(args, _state):
    return {
        "success": True,
        "ticket_id": f"MGR-{uuid.uuid4().hex[:8].upper()}",
        "reservation_id": args.get("reservation_id"),
        "reason": args.get("reason", ""),
        "message": "I've escalated this to our manager. They will call you back within one "
        "business day.",
    }


def on_tool_result(name, args, result, state):
    """Date-corruption and escalation ground truth, as the Vapi agent's tool-calls webhook
    records it."""
    count = next((len(result[k]) for k in RESULT_LIST_KEYS if isinstance(result.get(k), list)), 1)
    state["result_count"] = state.get("result_count", 0) + count
    # Judges need what the tool returned to spot a silently shifted date; capped to the
    # backend's 500-character budget for function.result.
    attributes = {"function.result": json.dumps(result)[:500], "tool.call.count": 1}
    events = []
    if result.get("success"):
        if name == "book_reservation":
            requested = (args.get("date") or "").lower().strip()
            returned = (result.get("date") or "").lower().strip()
            corrupted = int(bool(requested) and requested != returned)
            attributes["reservation.date_corrupted"] = corrupted
            events.append(
                (
                    "booking_attempted",
                    {"date_corrupted": corrupted, "party_size": args.get("party_size", 0)},
                )
            )
        elif name == "place_takeout_order":
            attributes["order.item_count"] = len(result.get("items") or [])
        elif name == "escalate_to_manager":
            attributes["escalation.triggered"] = 1
            events.append(("escalation_triggered", {"reason": str(args.get("reason", ""))[:200]}))
    return ToolOutcome(attributes, events)


def finalize(state, _call):
    return {"tool.result.count": state.get("result_count", 0)}


assistant = load_assistant("restaurant")
register(
    Vertical(
        slug="restaurant",
        agent_name="Tony",
        workflow_span_prefix="restaurant.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="asteria",
        tools=assistant["tools"],
        handlers={
            "check_availability": check_availability,
            "book_reservation": book_reservation,
            "modify_reservation": modify_reservation,
            "cancel_reservation": cancel_reservation,
            "place_takeout_order": place_takeout_order,
            "note_dietary_restriction": note_dietary_restriction,
            "escalate_to_manager": escalate_to_manager,
        },
        on_tool_result=on_tool_result,
        finalize=finalize,
        end_call_message="Grazie, take care now. Ciao!",
        keyterms=("Bron and Sons", "pappardelle"),
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
