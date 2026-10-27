"""Synthetic fixtures, deliberately independent of any real customer data or policies."""

INSTRUCTIONS = """You are Riley, the voice assistant for Cedar Demo Lending, a fictional testing service.
Speak naturally in English with one or two short sentences per turn. Ask one question at a time.
Say this is a demo in your greeting. Never request a real SSN, bank account, password or card number.
All details and actions are synthetic. You cannot approve a loan, promise eligibility, forgive debt,
change a payment or provide personalized financial advice. You can explain the demo support process.
For account questions, first ask for the demo loan code, then call lookup_demo_account.
Repeat the code back if unclear or corrected. The valid code is DEMO-1042; never invent account data.
When the caller says the code is DEMO-500, use it as given; that fixture deliberately fails.
If lookup fails, acknowledge the problem; do not invent an account or claim a successful lookup.
For difficulty making a payment, say a specialist can discuss options; do not promise an outcome.
If the caller requests a specialist, ask their consent to create a demo handoff. Only after they
explicitly agree, call create_demo_handoff with consent set to the exact string "confirmed".
State the returned ticket number and say no live transfer or account change occurred.
If the caller corrects a detail, use their latest correction. If interrupted, listen to their new request.
After the caller says they are done, say a short goodbye. Do not keep asking more questions."""
TOOLS = [
    {
        "type": "function",
        "name": "lookup_demo_account",
        "description": "Read a synthetic demo loan record; use the code the caller provided.",
        "parameters": {
            "type": "object",
            "properties": {"loan_code": {"type": "string"}},
            "required": ["loan_code"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "create_demo_handoff",
        "description": "Create a synthetic support ticket, only after the caller agrees. Does not transfer calls or modify accounts.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "consent": {
                    "type": "string",
                    "enum": ["confirmed", "not_confirmed"],
                    "description": "Use confirmed only after the caller explicitly agrees to the demo handoff.",
                },
            },
            "required": ["reason", "consent"],
            "additionalProperties": False,
        },
    },
]


def execute_tool(name, args):
    if not isinstance(args, dict):
        return {"ok": False, "error": "invalid_arguments"}
    if name == "lookup_demo_account":
        code = str(args.get("loan_code", "")).upper().replace(" ", "")
        if code == "DEMO-500":
            return {
                "ok": False,
                "error": "demo_service_unavailable",
                "retryable": False,
            }
        if code not in ("DEMO-1042", "DEMO1042"):
            return {"ok": False, "error": "demo_account_not_found"}
        return {
            "ok": True,
            "synthetic": True,
            "loan_code": "DEMO-1042",
            "customer_name": "Alex Sample",
            "payment_due": "$125",
            "due_date": "November 1, 2026",
            "support": "A specialist can discuss options; no approval is implied.",
        }
    if name == "create_demo_handoff":
        if args.get("consent") != "confirmed":
            return {"ok": False, "error": "consent_required"}
        return {
            "ok": True,
            "synthetic": True,
            "ticket": "DEMO-H1042",
            "live_transfer": False,
            "account_changed": False,
        }
    return {"ok": False, "error": "unknown_tool"}
