"""Bronchase Bank / Cassidy (coval-banking), ported from the Vapi demo agent's tool webhook.

Failure modes: a 25% CORE_BANKING_UNAVAILABLE outage on lookup_account, and account numbers one
digit away from a real one rejected as likely STT mistranscriptions (hint stt_mistranscription_likely).
"""

import random
import re

from verticals.base import ToolOutcome, Vertical, load_assistant
from verticals.registry import register

LOOKUP_OUTAGE_RATE = 0.25
# Outages that block the workflow. The shared session only recognises the last two.
DEPENDENCY_ERRORS = {"CORE_BANKING_UNAVAILABLE", "SERVICE_UNAVAILABLE", "TIMEOUT"}

PROFILES = {
    "1001234": {"name": "Jordan Wexler", "last_four_ssn": "7193", "balance": 4218.42},
    "1002847": {"name": "Priya Nair", "last_four_ssn": "4829", "balance": 9810.05},
    "1005520": {"name": "Devon Reyes", "last_four_ssn": "5520", "balance": 1380.27},
    # Business account: any SSN digits verify.
    "1008847": {"name": "Alessandra Choi", "last_four_ssn": "0000", "balance": 184320.00},
    "1007711": {"name": "Marcus Tate", "last_four_ssn": "7711", "balance": 2701.88},
}

TRANSACTIONS = [
    {"id": "TX-44821", "date": "2026-05-18", "amount": -89.99, "merchant": "Trident Athletics"},
    {
        "id": "TX-44820",
        "date": "2026-05-17",
        "amount": -42.10,
        "merchant": "Bay Area Coffee Roasters",
    },
    {"id": "TX-44819", "date": "2026-05-16", "amount": 2400.00, "merchant": "Payroll Deposit"},
    {"id": "TX-44818", "date": "2026-05-15", "amount": -119.50, "merchant": "PG&E Utility"},
    {"id": "TX-44817", "date": "2026-05-14", "amount": -32.00, "merchant": "Whole Foods Market"},
    {
        "id": "TX-44816",
        "date": "2026-05-13",
        "amount": -14.99,
        "merchant": "Streaming Subscription",
    },
]

REVERIFY = "Account number does not match our records — please re-verify."


def account_number(value) -> str:
    """Accepts "100 1234" / "100-1234" as 1001234; anything else is kept for the off-by-one check."""
    key = str(value or "").strip()
    return re.sub(r"[ -]", "", key) if re.fullmatch(r"[0-9 -]+", key) else key


def mistranscribed(account: str) -> bool:
    """True when the account is one digit away from a real one: the simulated STT mistranscription."""
    if account in PROFILES:
        return False
    return any(
        len(valid) == len(account) and sum(a != b for a, b in zip(account, valid)) == 1
        for valid in PROFILES
    )


def lookup_account(args, _state):
    account = account_number(args.get("account_number"))
    last_four_ssn = str(args.get("last_four_ssn", "")).strip()
    if not account or not last_four_ssn:
        return {
            "error": "MISSING_FIELDS",
            "message": "Both account_number and last_four_ssn are required for verification.",
        }
    if random.random() < LOOKUP_OUTAGE_RATE:
        return {
            "error": "CORE_BANKING_UNAVAILABLE",
            "message": "The core banking lookup service is temporarily unavailable. Do not "
            "disclose balances, transactions, transfers, disputes, or card status until the "
            "lookup succeeds.",
            "retry_after": "2026-06-09T18:00:00-07:00",
        }
    if mistranscribed(account):
        return {
            "error": "ACCOUNT_NOT_FOUND",
            "message": f"No account found matching number {account}. Please ask the caller to "
            "read back their account number digit by digit and try again.",
            "hint": "stt_mistranscription_likely",
        }
    profile = PROFILES.get(account)
    if not profile:
        return {
            "error": "ACCOUNT_NOT_FOUND",
            "message": f"No account found matching number {account}.",
        }
    if last_four_ssn != profile["last_four_ssn"] and profile["last_four_ssn"] != "0000":
        return {
            "error": "VERIFICATION_FAILED",
            "message": "The last 4 digits of SSN provided do not match our records.",
        }
    return {
        "account_number": account,
        "account_holder": profile["name"],
        "account_type": "checking" if account != "1008847" else "business_checking",
        "balance_usd": profile["balance"],
        "verified": True,
    }


def recent_transactions(args, _state):
    account = account_number(args.get("account_number"))
    count = int(args.get("count", 5))
    if mistranscribed(account):
        return {"error": "ACCOUNT_NOT_FOUND", "message": REVERIFY}
    return {"account_number": account, "transactions": TRANSACTIONS[:count]}


def dispute_charge(args, _state):
    account = account_number(args.get("account_number"))
    transaction_id = str(args.get("transaction_id", "")).strip()
    reason = str(args.get("reason", "unauthorized_charge")).strip()
    if mistranscribed(account):
        return {"error": "ACCOUNT_NOT_FOUND", "message": REVERIFY}
    case_id = f"DSP-{random.randint(100000, 999999)}"
    return {
        "success": True,
        "case_id": case_id,
        "transaction_id": transaction_id,
        "reason": reason,
        "provisional_credit_business_days": 10,
        "message": f"Dispute {case_id} opened for transaction {transaction_id}. A provisional "
        "credit will post within 10 business days while we investigate.",
    }


def freeze_card(args, _state):
    account = account_number(args.get("account_number"))
    card_last_four = str(args.get("card_last_four", "")).strip()
    if mistranscribed(account):
        return {"error": "ACCOUNT_NOT_FOUND", "message": REVERIFY}
    return {
        "success": True,
        "card_last_four": card_last_four,
        "status": "FROZEN",
        "replacement_eta_days": 3,
        "message": f"Card ending in {card_last_four} is now frozen. A replacement will arrive in "
        "3 business days at the address on file.",
    }


def transfer_funds(args, _state):
    from_account = account_number(args.get("from_account"))
    to_account = account_number(args.get("to_account"))
    amount = float(args.get("amount", 0))
    if mistranscribed(from_account) or mistranscribed(to_account):
        return {
            "error": "ACCOUNT_NOT_FOUND",
            "message": "One of the account numbers does not match our records — please re-verify.",
        }
    confirmation = f"TR-{random.randint(100000, 999999)}"
    return {
        "success": True,
        "from_account": from_account,
        "to_account": to_account,
        "amount_usd": amount,
        "confirmation_number": confirmation,
        "message": f"Transferred ${amount:.2f} successfully. Confirmation {confirmation}.",
    }


def wire_transfer(args, _state):
    from_account = account_number(args.get("from_account"))
    recipient_name = str(args.get("recipient_name", "")).strip()
    recipient_account = str(args.get("recipient_account", "")).strip()
    amount = float(args.get("amount", 0))
    if mistranscribed(from_account):
        return {
            "error": "ACCOUNT_NOT_FOUND",
            "message": "Source account number does not match our records — please re-verify.",
        }
    confirmation = f"WR-{random.randint(100000, 999999)}"
    return {
        "success": True,
        "from_account": from_account,
        "recipient_name": recipient_name,
        "recipient_account": recipient_account,
        "amount_usd": amount,
        "enhanced_auth": "passed",
        "confirmation_number": confirmation,
        "settlement_eta_hours": 24,
        "message": f"Wire of ${amount:.2f} to {recipient_name} initiated after enhanced "
        f"authentication. Confirmation {confirmation}. Settlement within 24 hours.",
    }


def report_fraud(args, _state):
    account = account_number(args.get("account_number"))
    fraud_description = str(args.get("fraud_description", "")).strip()
    if mistranscribed(account):
        return {"error": "ACCOUNT_NOT_FOUND", "message": REVERIFY}
    case_id = f"FRD-{random.randint(100000, 999999)}"
    return {
        "success": True,
        "case_id": case_id,
        "account_number": account,
        "description": fraud_description,
        "next_steps": [
            "Card has been frozen automatically.",
            "Fraud investigator will call within 1 business day.",
            "Provisional credit for affected charges within 10 business days.",
        ],
        "message": f"Fraud case {case_id} opened. Your card is frozen and an investigator will "
        "call within one business day.",
    }


def on_tool_result(_name, _args, result, _state):
    """Outage and result-count ground truth, as the Vapi agent's _parse_tool_result records it."""
    attributes = {"tool.dependency_unavailable": int(result.get("error") in DEPENDENCY_ERRORS)}
    if isinstance(result.get("transactions"), list):
        attributes["tool.result.count"] = len(result["transactions"])
    return ToolOutcome(attributes)


def finalize(_state, call):
    """Blocked only on an outage the call never recovered from; handled tool errors still complete."""
    dependency = any(tool.error_type in DEPENDENCY_ERRORS for tool in call.tools)
    fallback = any(tool.result.get("hint") == "stt_mistranscription_likely" for tool in call.tools)
    blocked = int(dependency and not fallback)
    return {
        "workflow.completed": int(bool(call.tools) and not blocked),
        "workflow.dependency_blocked": blocked,
    }


assistant = load_assistant("banking")
register(
    Vertical(
        slug="banking",
        agent_name="Cassidy",
        workflow_span_prefix="banking.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="asteria",
        tools=assistant["tools"],
        dependency_errors=frozenset({"CORE_BANKING_UNAVAILABLE", "SERVICE_UNAVAILABLE", "TIMEOUT"}),
        handlers={
            "lookup_account": lookup_account,
            "recent_transactions": recent_transactions,
            "dispute_charge": dispute_charge,
            "freeze_card": freeze_card,
            "transfer_funds": transfer_funds,
            "wire_transfer": wire_transfer,
            "report_fraud": report_fraud,
        },
        on_tool_result=on_tool_result,
        finalize=finalize,
        sensitive_argument_markers=(
            "account_number",
            "card_last_four",
            "from_account",
            "last_four_ssn",
            "recipient_account",
            "to_account",
        ),
        keyterms=("Bronchase", "Cassidy"),
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
