"""Bronmart / Avery (coval-retail), ported from the Vapi demo agent's tool webhook.

Failure modes: phantom inventory (hot SKUs report low_confidence stock ~30% of the time) and a 30%
inventory SERVICE_UNAVAILABLE on hot SKUs.
"""

import random
import re
from datetime import UTC, datetime, timedelta

from agent.config import UTC_OFFSET_MINUTES
from verticals.base import ToolOutcome, Vertical, load_assistant
from verticals.registry import register

CATALOG = {
    "BM-2241": {"name": 'Aurora 55" 4K Smart TV', "price": 429.99, "base_stock": 6},
    "BM-3380": {"name": "Cloudstep Running Shoe (M)", "price": 89.99, "base_stock": 12},
    "BM-1107": {"name": "Brewmaster Espresso Machine", "price": 199.00, "base_stock": 4},
    "BM-5562": {"name": "PlayCore Wireless Headset", "price": 59.99, "base_stock": 20},
    "BM-9015": {"name": "HearthGlow Ceramic Heater", "price": 74.50, "base_stock": 0},
    "BM-7720": {"name": "TrailLite 40L Backpack", "price": 119.95, "base_stock": 8},
    "BM-4408": {"name": "LumaSoft LED Floor Lamp", "price": 44.99, "base_stock": 15},
}
HOT_SKUS = {"BM-2241", "BM-1107", "BM-7720"}
OVERSELL_PROBABILITY = 0.3
INVENTORY_TOOL_ERROR_RATE = 0.3

VALID_ORDERS = {
    "BRN-552104": {
        "email": "jordan.wexler@example.com",
        "status": "out_for_delivery",
        "placed": "2026-05-22",
        "eta": "2026-05-28",
        "items": [{"sku": "BM-3380", "name": "Cloudstep Running Shoe (M)", "qty": 1}],
        "total_usd": 89.99,
        "delivered": False,
    },
    "BRN-552190": {
        "email": "priya.nair@example.com",
        "status": "delivered",
        "placed": "2026-04-30",
        "eta": "2026-05-05",
        "items": [{"sku": "BM-1107", "name": "Brewmaster Espresso Machine", "qty": 1}],
        "total_usd": 199.00,
        "delivered": True,
    },
    "BRN-552233": {
        "email": "devon.reyes@example.com",
        "status": "processing",
        "placed": "2026-05-26",
        "eta": "2026-06-01",
        "items": [{"sku": "BM-5562", "name": "PlayCore Wireless Headset", "qty": 2}],
        "total_usd": 119.98,
        "delivered": False,
    },
    "BRN-552301": {
        "email": "marcus.tate@example.com",
        "status": "delivered",
        "placed": "2026-02-11",
        "eta": "2026-02-16",
        "items": [{"sku": "BM-4408", "name": "LumaSoft LED Floor Lamp", "qty": 1}],
        "total_usd": 44.99,
        "delivered": True,
    },
}

LOYALTY = {
    "BMC-0001": {"name": "Jordan Wexler", "points": 4820, "tier": "Gold"},
    "BMC-0002": {"name": "Priya Nair", "points": 1190, "tier": "Silver"},
    "BMC-0003": {"name": "Devon Reyes", "points": 230, "tier": "Member"},
}

STORES = {
    "98101": {
        "name": "Bronmart Downtown Seattle",
        "address": "1500 Pike St",
        "hours": "9am–9pm daily",
    },
    "98052": {
        "name": "Bronmart Redmond Town Center",
        "address": "7345 164th Ave NE",
        "hours": "10am–8pm daily",
    },
    "97201": {
        "name": "Bronmart Portland Pearl",
        "address": "1201 NW Lovejoy St",
        "hours": "9am–8pm daily",
    },
}


def canonical_id(value, known: dict) -> str:
    """Accepts "BM 2241" / "bm2241" / "BRN552104" when it maps to exactly one known key."""
    key = str(value or "").strip().upper()
    if key in known or not re.fullmatch(r"[A-Z0-9 -]+", key):
        return key
    compact = key.replace(" ", "").replace("-", "")
    matches = [k for k in known if k.replace("-", "") == compact]
    return matches[0] if len(matches) == 1 else key


def lookup_order(args, _state):
    order_id = canonical_id(args.get("order_id"), VALID_ORDERS)
    email = str(args.get("email", "")).strip().lower()
    if not order_id:
        return {"error": "MISSING_FIELDS", "message": "order_id is required."}
    order = VALID_ORDERS.get(order_id)
    if not order:
        return {
            "error": "ORDER_NOT_FOUND",
            "message": f"No order found matching {order_id}. Please ask the caller to re-read "
            "the order number.",
        }
    if email and email != order["email"].lower():
        return {
            "error": "VERIFICATION_FAILED",
            "message": "The email on file does not match. Verify the caller before sharing order "
            "details.",
        }
    return {
        "order_id": order_id,
        "status": order["status"],
        "placed_date": order["placed"],
        "estimated_delivery": order["eta"],
        "items": order["items"],
        "total_usd": order["total_usd"],
        "verified": bool(email),
    }


def check_inventory(args, _state):
    sku = canonical_id(args.get("sku"), CATALOG)
    store_id = str(args.get("store_id", "")).strip()
    item = CATALOG.get(sku)
    if not item:
        return {"error": "SKU_NOT_FOUND", "message": f"No product found for SKU {sku}."}
    if sku in HOT_SKUS and random.random() < INVENTORY_TOOL_ERROR_RATE:
        return {
            "success": False,
            "error": "SERVICE_UNAVAILABLE",
            "message": "Inventory service timed out while checking high-demand SKU stock.",
            "sku": sku,
            "store_id": store_id or "online",
            "retryable": True,
        }
    now = datetime.now(UTC)
    if item["base_stock"] <= 0:
        return {
            "sku": sku,
            "product_name": item["name"],
            "store_id": store_id or "online",
            "in_stock": False,
            "quantity": 0,
            "stock_status": "confirmed",
            "last_synced": now.isoformat(),
        }
    if sku in HOT_SKUS and random.random() < OVERSELL_PROBABILITY:
        return {
            "sku": sku,
            "product_name": item["name"],
            "store_id": store_id or "online",
            "in_stock": True,
            "quantity": random.randint(1, 3),
            "stock_status": "low_confidence",
            "last_synced": (now - timedelta(hours=random.randint(6, 30))).isoformat(),
            "note": "Stock counts for this item may be delayed; confirm before promising same-day "
            "pickup.",
        }
    return {
        "sku": sku,
        "product_name": item["name"],
        "store_id": store_id or "online",
        "in_stock": True,
        "quantity": item["base_stock"],
        "stock_status": "confirmed",
        "last_synced": now.isoformat(),
    }


def place_order(args, _state):
    sku = canonical_id(args.get("sku"), CATALOG)
    store_id = str(args.get("store_id", "")).strip()
    fulfillment = args.get("fulfillment")
    # A real pickup store without an explicit fulfillment is still a pickup; a missing store
    # never silently authorizes shipping.
    if "fulfillment" not in args and store_id in STORES:
        fulfillment = "store_pickup"
    if fulfillment not in {"store_pickup", "ship_to_home"}:
        return {
            "error": "FULFILLMENT_REQUIRED",
            "message": "Confirm store_pickup or ship_to_home with the caller before placing an "
            "order.",
        }
    if fulfillment == "store_pickup" and store_id not in STORES:
        return {
            "error": "PICKUP_STORE_REQUIRED",
            "message": "Use find_store and confirm its returned store_id and location with the "
            "caller before pickup.",
        }
    if fulfillment == "ship_to_home" and store_id:
        return {
            "error": "FULFILLMENT_CONFLICT",
            "message": "Shipping cannot include a pickup store. Confirm the requested fulfillment "
            "with the caller.",
        }
    quantity = int(args.get("quantity", 1) or 1)
    item = CATALOG.get(sku)
    if not item:
        return {"error": "SKU_NOT_FOUND", "message": f"No product found for SKU {sku}."}
    order_id = f"BRN-{random.randint(600000, 699999)}"
    result = {
        "success": True,
        "order_id": order_id,
        "sku": sku,
        "product_name": item["name"],
        "quantity": quantity,
        "store_id": store_id or "online",
        "fulfillment": fulfillment,
        "total_usd": round(item["price"] * quantity, 2),
        "message": f"Order {order_id} placed for {quantity}x {item['name']}.",
    }
    if fulfillment == "store_pickup":
        # Store-local time: the model misread "18:49 UTC" as the next morning and refused same-day pickup.
        eta = datetime.now(UTC) + timedelta(hours=4, minutes=UTC_OFFSET_MINUTES)
        result["pickup_eta"] = eta.strftime("%A %B %-d, %-I:%M %p Pacific")
        result["store_name"] = STORES[store_id]["name"]
    return result


def check_return_eligibility(args, _state):
    order_id = canonical_id(args.get("order_id"), VALID_ORDERS)
    order = VALID_ORDERS.get(order_id)
    if not order:
        return {"error": "ORDER_NOT_FOUND", "message": f"No order found matching {order_id}."}
    if not order["delivered"]:
        return {
            "order_id": order_id,
            "eligible": False,
            "reason": "Order has not been delivered yet; returns open after delivery.",
        }
    delivered = datetime.strptime(order["eta"], "%Y-%m-%d").replace(tzinfo=UTC)
    days_since = (datetime.now(UTC) - delivered).days
    window_days = 30
    remaining = window_days - days_since
    eligible = remaining > 0
    return {
        "order_id": order_id,
        "eligible": eligible,
        "return_window_days": window_days,
        "days_since_delivery": days_since,
        "days_remaining": remaining if eligible else 0,
        "reason": "Within return window." if eligible else "Return window has closed (30 days).",
    }


def process_return(args, _state):
    order_id = canonical_id(args.get("order_id"), VALID_ORDERS)
    item_sku = str(args.get("item_sku", "")).strip().upper()
    reason = str(args.get("reason", "no_reason_given")).strip()
    order = VALID_ORDERS.get(order_id)
    if not order:
        return {"error": "ORDER_NOT_FOUND", "message": f"No order found matching {order_id}."}
    if not order["delivered"]:
        return {
            "error": "NOT_ELIGIBLE",
            "message": "Order has not been delivered; a return cannot be started yet.",
        }
    matched = next((it for it in order["items"] if it["sku"].upper() == item_sku), None)
    refund = CATALOG.get(item_sku, {}).get("price", order["total_usd"])
    rma_id = f"RMA-{random.randint(100000, 999999)}"
    return {
        "success": True,
        "rma_id": rma_id,
        "order_id": order_id,
        "item_sku": item_sku,
        "item_name": matched["name"]
        if matched
        else (CATALOG.get(item_sku, {}).get("name") or "item"),
        "reason": reason,
        "refund_amount_usd": round(refund, 2),
        "refund_method": "original_payment",
        "label_emailed": True,
        "message": f"Return {rma_id} started. A prepaid label has been emailed; a refund of "
        f"${refund:.2f} posts to the original payment within 5 business days.",
    }


def find_store(args, _state):
    zip_code = str(args.get("zip_code", "")).strip()
    store = STORES.get(zip_code)
    if not store:
        flagship = STORES["98101"]
        return {
            "zip_code": zip_code,
            "store_id": "98101",
            "nearest_store": flagship["name"],
            "address": flagship["address"],
            "hours": flagship["hours"],
            "note": "No store in that exact ZIP; returning nearest flagship.",
        }
    return {
        "zip_code": zip_code,
        "store_id": zip_code,
        "nearest_store": store["name"],
        "address": store["address"],
        "hours": store["hours"],
    }


def loyalty_balance(args, _state):
    member_id = canonical_id(args.get("member_id"), LOYALTY)
    member = LOYALTY.get(member_id)
    if not member:
        return {
            "error": "MEMBER_NOT_FOUND",
            "message": f"No loyalty member found matching {member_id}.",
        }
    return {
        "member_id": member_id,
        "member_name": member["name"],
        "points_balance": member["points"],
        "tier": member["tier"],
        "points_to_next_tier": max(0, 5000 - member["points"]),
    }


def on_tool_result(name, args, result, state):
    """Phantom-inventory ground truth, as the Vapi agent's tracer records it."""
    sku = canonical_id(args.get("sku"), CATALOG)
    oversold = state.setdefault("oversold_skus", set())
    if name == "check_inventory":
        flagged = int(result.get("stock_status") == "low_confidence")
        if flagged and sku:
            oversold.add(sku)
        return ToolOutcome({"inventory.sku": sku or None, "inventory.oversold": flagged})
    if name == "place_order" and result.get("success"):
        phantom = int(bool(sku) and sku in oversold)
        state["phantom_promises"] = state.get("phantom_promises", 0) + phantom
        events = [("oversell_committed", {"inventory.sku": sku})] if phantom else []
        return ToolOutcome(
            {"inventory.sku": sku or None, "inventory.phantom_promise": phantom}, events
        )
    return ToolOutcome()


def finalize(state, _call):
    return {
        "inventory.phantom_promise.count": state.get("phantom_promises", 0),
        "inventory.oversold_sku.count": len(state.get("oversold_skus", ())),
    }


assistant = load_assistant("retail")
register(
    Vertical(
        slug="retail",
        agent_name="Avery",
        workflow_span_prefix="retail.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="thalia",
        tools=assistant["tools"],
        handlers={
            "lookup_order": lookup_order,
            "check_inventory": check_inventory,
            "place_order": place_order,
            "check_return_eligibility": check_return_eligibility,
            "process_return": process_return,
            "find_store": find_store,
            "loyalty_balance": loyalty_balance,
        },
        on_tool_result=on_tool_result,
        finalize=finalize,
        keyterms=("Bronmart", "SKU"),
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
