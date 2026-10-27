"""Remy, the Bronwood Hotels & Resorts reservations agent from Coval's coval-hospitality demo org.

The prompt, greeting and tool schemas come from data/hospitality.json. The tools are mock backends
with deliberate failure modes, so Coval's metrics have something to catch:

- book_room's property-management write fails with ROOM_INVENTORY_SYNC_FAILED 25% of the time.
- Otherwise book_room, and a modify_reservation room change, silently book one tier down 40% of
  the time. Remy should catch the downgrade when reading the booking back.
- A requested premium room type sells out 45% of the time.

This is the same agent, with the same odds, as the Cloudflare Workers version in
../cloudflare/src/verticals/hospitality.py.
"""

import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Simulated callers speak in US time zones; dates are checked against this local offset.
UTC_OFFSET_MINUTES = -7 * 60

_ASSISTANT = json.loads((Path(__file__).parent / "data" / "hospitality.json").read_text())
PROMPT = _ASSISTANT["systemPrompt"]
GREETING = _ASSISTANT["firstMessage"]
# OpenAI-style function schemas.
TOOLS = [tool.get("function", tool) for tool in _ASSISTANT["tools"]]
VOICE = "asteria"
GOODBYE = "Thanks for calling Bronwood Hotels and Resorts. Have a great day!"
# Words the speech recognizer should expect.
KEYTERMS = ("Bronwood", "Bronwood Rewards", "Remy")
SILENCE_TIMEOUT_SECONDS = _ASSISTANT["silenceTimeoutSeconds"]

ROOM_DOWNGRADE_PROBABILITY = 0.4
PREMIUM_SOLD_OUT_PROBABILITY = 0.45
ROOM_BOOKING_ERROR_RATE = 0.25
# Tool errors that mean a backend was down, rather than a bad request.
DEPENDENCY_ERRORS = {"ROOM_INVENTORY_SYNC_FAILED", "SERVICE_UNAVAILABLE", "TIMEOUT"}

# One-tier-down room downgrade map for the signature failure mode. Only room types in this map
# are ever at risk; premium and suite bookings are the exposure.
DOWNGRADE_MAP = {
    "presidential suite": "suite",
    "suite": "deluxe king",
    "deluxe king": "king",
    "king": "queen",
    "queen": "double",
}

# Per-room-type nightly rate card (fictional). Used by quote, book and cancel.
RATE_CARD = {
    "double": 189.0,
    "queen": 219.0,
    "king": 249.0,
    "deluxe king": 309.0,
    "suite": 459.0,
    "presidential suite": 899.0,
}


def new_reservation_id() -> str:
    return f"BWD-{datetime.now(UTC).strftime('%Y%m%d')}-{random.randint(1000, 9999)}"


def new_ticket_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def nights_between(check_in: str, check_out: str) -> int:
    """Best-effort night count; 1 when the dates can't be parsed or are out of order."""
    try:
        ci = datetime.strptime(check_in, "%Y-%m-%d")
        co = datetime.strptime(check_out, "%Y-%m-%d")
        nights = (co - ci).days
        return nights if nights > 0 else 1
    except (ValueError, TypeError):
        return 1


def past_date_error(check_in):
    """gpt-oss books guests into last year when they give a date without one; refuse it."""
    try:
        requested = datetime.strptime(str(check_in), "%Y-%m-%d").date()
    except ValueError:
        return None
    today = (datetime.now(UTC) + timedelta(minutes=UTC_OFFSET_MINUTES)).date()
    if requested >= today:
        return None
    return {
        "error": "DATE_IN_PAST",
        "message": f"{check_in} is before today ({today.isoformat()}). Use the next "
        f"{requested:%B} {requested.day} on or after today and confirm the year with the guest.",
    }


def nightly_rate(room_type: str, rate_plan: str) -> float:
    base = RATE_CARD.get((room_type or "").lower().strip(), 229.0)
    # Advance-purchase (non-refundable) is ~12% off; flexible is full price.
    if (rate_plan or "").lower().strip() == "advance_purchase":
        return round(base * 0.88, 2)
    return base


def check_availability(args):
    property_id = args.get("property_id", "bwd-sf-downtown")
    check_in = args.get("check_in", "2026-07-10")
    if error := past_date_error(check_in):
        return error
    check_out = args.get("check_out", "2026-07-12")
    room_type = args.get("room_type", "king")
    party_size = args.get("party_size", 2)
    # Premium types sometimes sell out so the sold-out scenarios can land.
    premium = (room_type or "").lower().strip() in ("suite", "presidential suite")
    premium_sold_out = premium and random.random() < PREMIUM_SOLD_OUT_PROBABILITY
    available_room_types = [
        {"room_type": "queen", "rate": RATE_CARD["queen"], "remaining": 6},
        {"room_type": "king", "rate": RATE_CARD["king"], "remaining": 4},
        {"room_type": "deluxe king", "rate": RATE_CARD["deluxe king"], "remaining": 2},
    ]
    if not premium_sold_out:
        available_room_types.append({"room_type": "suite", "rate": RATE_CARD["suite"], "remaining": 1})
    return {
        "success": True,
        "property_id": property_id,
        "property_name": "Bronwood San Francisco Downtown",
        "check_in": check_in,
        "check_out": check_out,
        "party_size": party_size,
        "requested_room_type": room_type,
        "requested_type_available": (not premium_sold_out) if premium else True,
        "sold_out_types": ["presidential suite"] + (["suite"] if premium_sold_out else []),
        "available_room_types": available_room_types,
        "amenities": [
            "complimentary wifi",
            "rooftop pool",
            "fitness center",
            "valet parking ($55/night)",
        ],
    }


def quote_rate(args):
    property_id = args.get("property_id", "bwd-sf-downtown")
    room_type = args.get("room_type", "king")
    check_in = args.get("check_in", "2026-07-10")
    if error := past_date_error(check_in):
        return error
    check_out = args.get("check_out", "2026-07-12")
    rate_plan = args.get("rate_plan", "flexible")
    if (rate_plan or "").lower().strip() not in ("flexible", "advance_purchase"):
        rate_plan = "flexible"
    nights = nights_between(check_in, check_out)
    nightly = nightly_rate(room_type, rate_plan)
    subtotal = round(nightly * nights, 2)
    taxes_fees = round(subtotal * 0.155, 2)
    return {
        "success": True,
        "property_id": property_id,
        "room_type": room_type,
        "check_in": check_in,
        "check_out": check_out,
        "rate_plan": rate_plan,
        "refundable": rate_plan == "flexible",
        "nights": nights,
        "nightly_rate": nightly,
        "subtotal": subtotal,
        "taxes_and_fees": taxes_fees,
        "total": round(subtotal + taxes_fees, 2),
        "currency": "USD",
    }


def book_room(args):
    """Broken by design: returns success but silently books one tier down 40% of the time.
    Remy should catch it on the read-back; if not, the Room Booking Accuracy judge fires NO."""
    property_id = args.get("property_id", "bwd-sf-downtown")
    room_type_requested = args.get("room_type", "king")
    check_in = args.get("check_in", "2026-07-10")
    if error := past_date_error(check_in):
        return error
    check_out = args.get("check_out", "2026-07-12")
    guest_name = args.get("guest_name", "Guest")
    rate_plan = args.get("rate_plan", "flexible")
    loyalty_id = args.get("loyalty_id")
    if (rate_plan or "").lower().strip() not in ("flexible", "advance_purchase"):
        rate_plan = "flexible"
    # The property-management write fails after the quote is confirmed.
    if random.random() < ROOM_BOOKING_ERROR_RATE:
        return {
            "success": False,
            "error": "ROOM_INVENTORY_SYNC_FAILED",
            "message": "Property-management system rejected the room booking after quote confirmation.",
            "retryable": True,
            "property_id": property_id,
            "room_type": room_type_requested,
            "check_in": check_in,
            "check_out": check_out,
            "guest_name": guest_name,
            "rate_plan": rate_plan,
            "loyalty_id": loyalty_id,
        }
    room_type_returned = room_type_requested
    requested_key = (room_type_requested or "").lower().strip()
    if requested_key in DOWNGRADE_MAP and random.random() < ROOM_DOWNGRADE_PROBABILITY:
        room_type_returned = DOWNGRADE_MAP[requested_key]
    nightly = nightly_rate(room_type_returned, rate_plan)
    total = round(nightly * nights_between(check_in, check_out) * 1.155, 2)
    return {
        "success": True,
        "reservation_id": new_reservation_id(),
        "property_id": property_id,
        "room_type": room_type_returned,
        "check_in": check_in,
        "check_out": check_out,
        "rate": nightly,
        "total": total,
        "guest_name": guest_name,
        "rate_plan": rate_plan,
        "loyalty_id": loyalty_id,
        "confirmation_message": (
            f"Booked for {guest_name}: {room_type_returned} at {property_id}, "
            f"{check_in} to {check_out}, ${nightly}/night ({rate_plan}). "
            "A confirmation email is on its way."
        ),
    }


def modify_reservation(args):
    """A room-type change here can also trigger the silent downgrade."""
    if error := past_date_error(args.get("new_check_in")):
        return error
    new_room_type_requested = args.get("new_room_type")
    new_room_type_returned = new_room_type_requested
    if new_room_type_requested:
        requested_key = (new_room_type_requested or "").lower().strip()
        if requested_key in DOWNGRADE_MAP and random.random() < ROOM_DOWNGRADE_PROBABILITY:
            new_room_type_returned = DOWNGRADE_MAP[requested_key]
    return {
        "success": True,
        "reservation_id": args.get("reservation_id", "BWD-UNKNOWN"),
        "new_check_in": args.get("new_check_in"),
        "new_check_out": args.get("new_check_out"),
        "new_room_type": new_room_type_returned,
        "new_party_size": args.get("new_party_size"),
        "message": "Your reservation has been updated. A fresh confirmation email is on its way.",
    }


def cancel_reservation(args):
    """flexible: free outside 48h of the 4pm check-in, one-night penalty inside.
    advance_purchase: non-refundable."""
    reservation_id = args.get("reservation_id", "BWD-UNKNOWN")
    rate_plan = (args.get("rate_plan") or "flexible").lower().strip()
    nightly = float(args.get("rate") or RATE_CARD["king"])
    hours_to_checkin = args.get("hours_to_checkin")
    if rate_plan == "advance_purchase":
        refund_amount = 0.0
        penalty = round(nightly, 2)
        policy = "advance_purchase_non_refundable"
        message = "This is an advance-purchase (non-refundable) booking, so no refund applies."
    elif isinstance(hours_to_checkin, (int, float)) and hours_to_checkin <= 48:
        refund_amount = 0.0
        penalty = round(nightly, 2)
        policy = "flexible_inside_48h_one_night_penalty"
        message = "We're inside the 48-hour window, so a one-night penalty applies on this flexible rate."
    else:
        refund_amount = round(nightly, 2)
        penalty = 0.0
        policy = "flexible_free_cancellation"
        message = "You're outside the 48-hour window, so this flexible booking cancels free of charge."
    return {
        "success": True,
        "reservation_id": reservation_id,
        "rate_plan": rate_plan,
        "refund_amount": refund_amount,
        "penalty": penalty,
        "policy": policy,
        "currency": "USD",
        "message": message,
    }


def lookup_reservation(args):
    reservation_id = args.get("reservation_id", "")
    last_name = (args.get("last_name") or "").strip()
    if not reservation_id or not last_name:
        return {
            "success": False,
            "error": "VERIFICATION_FAILED",
            "message": "I need both the reservation ID and the last name on the booking to look that up.",
        }
    # Mock verification: a last name of letters (hyphens and apostrophes allowed) verifies.
    if not (last_name.replace("-", "").replace("'", "").isalpha() and len(last_name) >= 2):
        return {
            "success": False,
            "error": "NOT_FOUND",
            "reservation_id": reservation_id,
            "message": "I couldn't find a reservation matching that ID and last name.",
        }
    return {
        "success": True,
        "reservation_id": reservation_id,
        "guest_last_name": last_name,
        "property_id": "bwd-sf-downtown",
        "room_type": "king",
        "check_in": "2026-07-10",
        "check_out": "2026-07-12",
        "rate_plan": "flexible",
        "status": "confirmed",
    }


def add_special_request(args):
    request_text = args.get("request_text", "")
    lowered = request_text.lower()
    # Accessible and connecting rooms depend on inventory, so they are requests; pets and late
    # checkout are guaranteed.
    request_only = any(
        k in lowered for k in ("ada", "accessib", "wheelchair", "connecting", "adjoining", "roll-in")
    )
    return {
        "success": True,
        "reservation_id": args.get("reservation_id", "BWD-UNKNOWN"),
        "request_text": request_text,
        "request_type": "accessibility_or_connecting" if request_only else "standard",
        "guaranteed": not request_only,
        "status": "request_only_pending_inventory" if request_only else "guaranteed",
        "message": (
            "I've added this to your reservation as a request — it's subject to availability "
            "and not guaranteed until the property confirms."
            if request_only
            else "I've added this to your reservation and it's confirmed."
        ),
    }


def apply_loyalty(args):
    member_id = (args.get("member_id") or "").strip()
    # Short or empty member IDs are rejected so the fake-loyalty persona gets caught.
    if not member_id or len(member_id) < 8:
        return {
            "success": False,
            "error": "MEMBER_NOT_FOUND",
            "member_id": member_id,
            "message": "I couldn't find a Bronwood Rewards account for that member number.",
        }
    tier = random.choice(["Silver", "Gold", "Platinum"])
    return {
        "success": True,
        "member_id": member_id,
        "reservation_id": args.get("reservation_id"),
        "program": "Bronwood Rewards",
        "tier": tier,
        "points_balance": random.randint(2_500, 85_000),
        "benefits": ["late checkout (subject to availability)", "bonus points on stay"],
        "message": f"Thanks — your {tier} Bronwood Rewards benefits are applied to this stay.",
    }


def request_upgrade(args):
    """Submits an upgrade request; never auto-confirms or comps one."""
    return {
        "success": True,
        "request_id": new_ticket_id("UPG"),
        "reservation_id": args.get("reservation_id", "BWD-UNKNOWN"),
        "requested_type": args.get("requested_type", "suite"),
        "status": "pending",
        "guaranteed": False,
        "message": "I've submitted an upgrade request to the property. It's not guaranteed — "
        "the front desk will confirm at check-in based on availability.",
    }


HANDLERS = {
    "check_availability": check_availability,
    "quote_rate": quote_rate,
    "book_room": book_room,
    "modify_reservation": modify_reservation,
    "cancel_reservation": cancel_reservation,
    "lookup_reservation": lookup_reservation,
    "add_special_request": add_special_request,
    "apply_loyalty": apply_loyalty,
    "request_upgrade": request_upgrade,
}


def _downgrade(requested, returned) -> dict:
    requested = (requested or "").lower().strip()
    returned = (returned or "").lower().strip()
    return {
        "reservation.room_downgraded": int(requested != returned),
        "reservation.requested_room_type": requested,
        "reservation.returned_room_type": returned,
    }


def outcome_attributes(name: str, args: dict, result: dict) -> dict:
    """Failure-mode ground truth for one tool call, recorded on its trace span."""
    if not result.get("success"):
        if name == "apply_loyalty":
            return {"loyalty.lookup_failed": 1}
        if name == "lookup_reservation":
            return {"lookup.verification_failed": 1}
        return {}
    if name == "book_room":
        return _downgrade(args.get("room_type"), result.get("room_type"))
    if name == "modify_reservation" and (args.get("new_room_type") or "").strip():
        return _downgrade(args.get("new_room_type"), result.get("new_room_type"))
    if name == "cancel_reservation":
        return {
            "cancellation.refund_amount": float(result.get("refund_amount") or 0.0),
            "cancellation.penalty": float(result.get("penalty") or 0.0),
            "cancellation.policy": str(result.get("policy", "")),
        }
    if name == "request_upgrade":
        return {"upgrade.requested": 1}
    if name == "apply_loyalty":
        return {"loyalty.applied": 1}
    return {}
