"""CVS Pharmacy #4417 / Ava (ibm-cvs), ported from the Vapi demo agent's tool webhook and tracer.

Failure modes: lookup_insurance_copay is always SERVICE_UNAVAILABLE (the agent must not guess a
price), plus refill-too-soon, no-refills, controlled-substance and other-pharmacy refill blocks.
"""

import random
import re
from datetime import date, timedelta

from verticals.base import Vertical, load_assistant
from verticals.registry import register

STORE = {
    "store": "#4417",
    "name": "CVS Pharmacy #4417",
    "address": "1200 Maple Street, Springfield",
    "hours": {
        "monday_friday": "8:00 AM - 9:00 PM",
        "saturday": "9:00 AM - 6:00 PM",
        "sunday": "10:00 AM - 5:00 PM",
    },
    "drive_thru": True,
    "pharmacist_on_duty": "Dr. Priya Nair",
}

PATIENTS = [
    {
        "patient_id": "PT-1001",
        "name": "Margaret Ellison",
        "dob": (1951, 3, 8),
        "phone": "2175550148",
        "prescriptions": [
            {
                "rx_number": "7731204",
                "medication": "metformin",
                "dose": "500 mg",
                "directions": "twice daily",
                "status": "READY_FOR_PICKUP",
                "refills_remaining": 2,
            },
            {
                "rx_number": "7731209",
                "medication": "atorvastatin",
                "dose": "20 mg",
                "directions": "nightly",
                "status": "FILLED",
                "refills_remaining": 3,
                "refill_too_soon": True,
                "last_filled_days_ago": 6,
                "next_eligible_in_days": 24,
            },
        ],
    },
    {
        "patient_id": "PT-1002",
        "name": "Daniel Okafor",
        "dob": (1988, 11, 22),
        "phone": "2175550192",
        "prescriptions": [
            {
                "rx_number": "8820417",
                "medication": "sertraline",
                "dose": "50 mg",
                "directions": "daily",
                "status": "NO_REFILLS_REMAINING",
                "refills_remaining": 0,
                "prescriber": "Dr. Alan Whitfield",
            }
        ],
    },
    {
        "patient_id": "PT-1003",
        "name": "Sofia Ramirez",
        "dob": (1994, 6, 15),
        "phone": "2175550173",
        "prescriptions": [
            {
                "rx_number": "9014452",
                "medication": "levothyroxine",
                "dose": "75 mcg",
                "directions": "daily",
                "status": "IN_PROGRESS",
                "ready_after": "today 4:00 PM",
                "refills_remaining": 5,
            },
            {
                "rx_number": "9014460",
                "medication": "albuterol inhaler",
                "dose": "90 mcg/actuation",
                "directions": "as needed",
                "status": "READY_FOR_PICKUP",
                "refills_remaining": 3,
            },
        ],
    },
    {
        "patient_id": "PT-1004",
        "name": "James Whitaker",
        "dob": (1979, 1, 30),
        "phone": "2175550111",
        "prescriptions": [
            {
                "rx_number": "9112008",
                "medication": "oxycodone-acetaminophen",
                "dose": "5/325 mg",
                "directions": "as prescribed",
                "status": "CONTROLLED_SUBSTANCE",
                "refills_remaining": 0,
                "controlled": True,
                "schedule": "II",
            }
        ],
    },
    {
        "patient_id": "PT-1005",
        "name": "Linda Chen",
        "dob": (1962, 9, 4),
        "phone": "2175550139",
        "prescriptions": [
            {
                "rx_number": "7719300",
                "medication": "lisinopril",
                "dose": "10 mg",
                "directions": "daily",
                "status": "ON_FILE_AT_OTHER_PHARMACY",
                "other_pharmacy": "Walgreens on Elm Street",
                "transfer_eligible": True,
                "refills_remaining": 2,
            }
        ],
    },
]

IMMUNIZATION_SLOTS = {
    "flu": ["Tuesday 10:15 AM", "Tuesday 2:45 PM", "Wednesday 11:30 AM"],
    "covid_booster": ["Wednesday 3:00 PM", "Thursday 9:45 AM"],
    "shingles": [],
    "tdap": [],
    "pneumonia": [],
}

INTERACTIONS = [
    (
        {"metformin", "atorvastatin"},
        {
            "severity": "none",
            "guidance": "No significant interaction between metformin and atorvastatin.",
        },
    ),
    (
        {"sertraline", "ibuprofen"},
        {
            "severity": "moderate",
            "guidance": "Sertraline with ibuprofen increases bleeding risk. Recommend a "
            "pharmacist consult before combining.",
            "action": "pharmacist_consult_recommended",
        },
    ),
    (
        {"lisinopril", "potassium"},
        {
            "severity": "moderate",
            "guidance": "Lisinopril with potassium supplements raises the risk of high potassium "
            "(hyperkalemia). Recommend a pharmacist consult.",
            "action": "pharmacist_consult_recommended",
        },
    ),
    (
        {"levothyroxine", "calcium"},
        {
            "severity": "minor",
            "guidance": "Take levothyroxine and calcium at least 4 hours apart.",
        },
    ),
]

CONTROLLED_KEYWORDS = (
    "oxycodone",
    "percocet",
    "hydrocodone",
    "vicodin",
    "norco",
    "adderall",
    "amphetamine",
    "xanax",
    "alprazolam",
    "morphine",
    "fentanyl",
    "codeine",
    "tramadol",
    "lorazepam",
    "ativan",
    "clonazepam",
    "klonopin",
    "diazepam",
    "valium",
    "methylphenidate",
    "ritalin",
)

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# The Vapi tracer's SENSITIVE_ARGUMENT_KEYS: PHI never reaches span arguments.
SENSITIVE_ARGUMENT_KEYS = (
    "callback_phone",
    "date_of_birth",
    "full_name",
    "medication",
    "medication_a",
    "medication_b",
    "medication_or_rx",
    "patient_id",
    "patient_name",
    "phone",
    "rx_number",
)


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _normalize_phone(value) -> str:
    digits = _digits(value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _normalize_name(value) -> str:
    cleaned = re.sub(r"[^a-z\s]", " ", str(value or "").lower())
    return " ".join(cleaned.split())


def _name_matches(given: str, on_file: str) -> bool:
    a = _normalize_name(given)
    b = _normalize_name(on_file)
    if not a or not b:
        return False
    if a == b:
        return True
    a_parts = a.split()
    b_parts = b.split()
    # Tolerate a middle name / initial from STT as long as first and last agree.
    return len(a_parts) >= 2 and a_parts[0] == b_parts[0] and a_parts[-1] == b_parts[-1]


def _parse_dob(value) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lower().replace(",", " ")
    if not text:
        return None
    # ISO / numeric forms: 1951-03-08, 03/08/1951, 3-8-51
    numeric = re.findall(r"\d+", text)
    month_word = None
    for token in re.findall(r"[a-z]+", text):
        key = token[:4] if token.startswith("sept") else token[:3]
        if key in MONTHS:
            month_word = MONTHS[key]
            break
    if month_word:
        numbers = [int(n) for n in numeric]
        year = next((n for n in numbers if n > 31), None)
        day = next((n for n in numbers if 1 <= n <= 31), None)
        if year is None and len(numbers) >= 2:
            year = numbers[-1]
        if year is not None and year < 100:
            year += 1900 if year > 25 else 2000
        if year and day:
            return (year, month_word, day)
        return None
    if len(numeric) >= 3:
        a, b, c = (int(numeric[0]), int(numeric[1]), int(numeric[2]))
        if a > 31:  # Y-M-D
            return (a, b, c)
        year = c if c > 99 else (c + 1900 if c > 25 else c + 2000)
        if a > 12 and b <= 12:  # D-M-Y
            return (year, b, a)
        return (year, a, b)  # M-D-Y (US default)
    if len(numeric) == 1 and len(numeric[0]) == 8:  # 03081951 or 19510308
        raw = numeric[0]
        if raw[:4] > "1900":
            return (int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        return (int(raw[4:]), int(raw[:2]), int(raw[2:4]))
    return None


def _find_patient(patient_id: str) -> dict | None:
    wanted = str(patient_id or "").strip().upper()
    for patient in PATIENTS:
        if patient["patient_id"] == wanted:
            return patient
    # Tolerate the model dropping the prefix.
    for patient in PATIENTS:
        if _digits(patient["patient_id"]) == _digits(wanted) and _digits(wanted):
            return patient
    return None


def _find_rx(patient: dict, medication_or_rx: str) -> dict | None:
    query = str(medication_or_rx or "").strip().lower()
    query_digits = _digits(query)
    for rx in patient["prescriptions"]:
        if query_digits and (
            rx["rx_number"] == query_digits or rx["rx_number"].endswith(query_digits)
        ):
            if len(query_digits) >= 4:
                return rx
    for rx in patient["prescriptions"]:
        med = rx["medication"].lower()
        if query and (query in med or med.split()[0] in query or med.split("-")[0] in query):
            return rx
    return None


def _is_controlled(medication: str) -> bool:
    text = str(medication or "").lower()
    return any(keyword in text for keyword in CONTROLLED_KEYWORDS)


def _rx_public(rx: dict) -> dict:
    result = {
        "medication": rx["medication"],
        "dose": rx["dose"],
        "directions": rx["directions"],
        "rx_number": rx["rx_number"],
        "status": rx["status"],
        "refills_remaining": rx["refills_remaining"],
        "store": STORE["store"],
    }
    if rx["status"] == "READY_FOR_PICKUP":
        result["ready_for_pickup"] = True
        result["pickup_available"] = "now, during store hours"
    elif rx["status"] == "IN_PROGRESS":
        result["ready_for_pickup"] = False
        result["ready_after"] = rx.get("ready_after")
    elif rx.get("refill_too_soon"):
        result["ready_for_pickup"] = False
        result["last_filled_days_ago"] = rx["last_filled_days_ago"]
        result["next_eligible_in_days"] = rx["next_eligible_in_days"]
        result["next_eligible_date"] = (
            date.today() + timedelta(days=rx["next_eligible_in_days"])
        ).isoformat()
        result["refill_status"] = "REFILL_TOO_SOON"
    elif rx["status"] == "NO_REFILLS_REMAINING":
        result["ready_for_pickup"] = False
        result["prescriber"] = rx.get("prescriber")
        result["note"] = (
            "No refills left; a refill request must go to the prescriber (1-2 business days)."
        )
    elif rx.get("controlled"):
        result["ready_for_pickup"] = False
        result["controlled_substance"] = True
        result["schedule"] = rx.get("schedule")
        result["note"] = (
            "Schedule II controlled substance; cannot be refilled by phone, new prescription "
            "required from prescriber."
        )
    elif rx["status"] == "ON_FILE_AT_OTHER_PHARMACY":
        result["ready_for_pickup"] = False
        result["other_pharmacy"] = rx.get("other_pharmacy")
        result["transfer_eligible"] = rx.get("transfer_eligible", False)
    return result


def _unknown_patient() -> dict:
    return {
        "error": "UNKNOWN_PATIENT",
        "message": "No verified patient with that patient_id. Call verify_patient first.",
    }


def verify_patient(args, _state):
    full_name = args.get("full_name", "")
    dob = _parse_dob(args.get("date_of_birth", ""))
    phone = _normalize_phone(args.get("phone", ""))
    for patient in PATIENTS:
        if (
            _name_matches(full_name, patient["name"])
            and dob == patient["dob"]
            and phone == patient["phone"]
        ):
            return {
                "verified": True,
                "patient_id": patient["patient_id"],
                "name": patient["name"],
                "store": STORE["store"],
            }
    return {"verified": False, "reason": "NO_MATCH"}


def check_prescription_status(args, _state):
    patient = _find_patient(args.get("patient_id", ""))
    if not patient:
        return _unknown_patient()
    rx = _find_rx(patient, args.get("medication_or_rx", ""))
    if not rx:
        return {
            "error": "RX_NOT_FOUND",
            "message": "No prescription matching that medication or Rx number on this patient's "
            "profile at store #4417.",
            "prescriptions_on_file": [
                {"medication": item["medication"], "rx_number": item["rx_number"]}
                for item in patient["prescriptions"]
            ],
        }
    result = _rx_public(rx)
    result["patient_id"] = patient["patient_id"]
    return result


def request_refill(args, _state):
    patient = _find_patient(args.get("patient_id", ""))
    if not patient:
        return _unknown_patient()
    rx = _find_rx(patient, args.get("rx_number", ""))
    if not rx:
        return {
            "error": "RX_NOT_FOUND",
            "message": "No prescription with that Rx number on this patient's profile at store "
            "#4417.",
        }
    pickup_method = args.get("pickup_method") or "in_store"
    if rx.get("controlled"):
        return {
            "success": False,
            "reason": "CONTROLLED_SUBSTANCE",
            "action": "pharmacist_required",
            "medication": rx["medication"],
            "rx_number": rx["rx_number"],
            "message": "Schedule II controlled substance. Cannot be refilled or expedited by "
            "phone; a new prescription from the prescriber is required. Route the caller to the "
            "pharmacist.",
        }
    if rx.get("refill_too_soon"):
        next_eligible = date.today() + timedelta(days=rx["next_eligible_in_days"])
        return {
            "success": False,
            "reason": "REFILL_TOO_SOON",
            "medication": rx["medication"],
            "rx_number": rx["rx_number"],
            "last_filled_days_ago": rx["last_filled_days_ago"],
            "next_eligible_date": next_eligible.isoformat(),
            "next_eligible_in_days": rx["next_eligible_in_days"],
            "message": "Refill not yet eligible under the fill schedule. Do not file it.",
        }
    if rx["refills_remaining"] <= 0:
        return {
            "success": False,
            "reason": "NO_REFILLS",
            "action": "prescriber_request_sent",
            "medication": rx["medication"],
            "rx_number": rx["rx_number"],
            "prescriber": rx.get("prescriber", "the prescriber"),
            "eta": "1-2 business days",
            "message": "No refills remaining. A refill authorization request has been sent to "
            "the prescriber.",
        }
    if rx["status"] == "ON_FILE_AT_OTHER_PHARMACY":
        return {
            "success": False,
            "reason": "NOT_ON_FILE_AT_STORE",
            "action": "transfer_required",
            "medication": rx["medication"],
            "rx_number": rx["rx_number"],
            "other_pharmacy": rx.get("other_pharmacy"),
            "message": "This prescription is on file at another pharmacy. Start a transfer with "
            "transfer_prescription first.",
        }
    result = {
        "success": True,
        "confirmation": f"RX-CONF-{random.randint(10000, 99999)}",
        "medication": rx["medication"],
        "dose": rx["dose"],
        "rx_number": rx["rx_number"],
        "pickup_method": pickup_method,
        "ready_by": "today 4:00 PM",
        "store": STORE["store"],
        "refills_remaining_after": max(rx["refills_remaining"] - 1, 0),
    }
    if pickup_method == "delivery":
        result["delivery_window"] = "tomorrow between 9:00 AM and 6:00 PM"
    return result


def transfer_prescription(args, _state):
    patient = _find_patient(args.get("patient_id", ""))
    if not patient:
        return _unknown_patient()
    medication = args.get("medication", "")
    from_pharmacy = args.get("from_pharmacy", "")
    if not medication or not from_pharmacy:
        return {
            "error": "MISSING_DETAILS",
            "message": "Both the medication and the other pharmacy's name and location are "
            "required.",
        }
    if _is_controlled(medication):
        return {
            "success": False,
            "reason": "CONTROLLED_SUBSTANCE",
            "action": "pharmacist_required",
            "message": "Controlled-substance transfers must be handled by the pharmacist.",
        }
    rx = _find_rx(patient, medication)
    return {
        "success": True,
        "transfer_id": f"TR-{random.randint(10000, 99999)}",
        "medication": rx["medication"] if rx else medication,
        "rx_number": rx["rx_number"] if rx else None,
        "from_pharmacy": from_pharmacy,
        "to_store": STORE["store"],
        "eta": "about 1 business day",
        "message": "Transfer request submitted. We will text the patient when it is ready at "
        "store #4417.",
    }


def check_drug_interaction(args, _state):
    med_a = str(args.get("medication_a", "")).strip().lower()
    med_b = str(args.get("medication_b", "")).strip().lower()
    if _is_controlled(med_a) or _is_controlled(med_b):
        return {
            "severity": "ROUTE_TO_PHARMACIST",
            "action": "pharmacist_required",
            "medication_a": med_a,
            "medication_b": med_b,
            "guidance": "Questions involving a controlled substance must be answered by the "
            "pharmacist.",
        }
    for pair, info in INTERACTIONS:
        hits = 0
        for keyword in pair:
            if keyword in med_a or keyword in med_b:
                hits += 1
        if hits == 2 and any(k in med_a for k in pair) and any(k in med_b for k in pair):
            result = {"medication_a": med_a, "medication_b": med_b}
            result.update(info)
            return result
    return {
        "severity": "not_in_reference",
        "medication_a": med_a,
        "medication_b": med_b,
        "guidance": "This combination is not in the quick-reference. The pharmacist can review "
        "it; do not state that it is safe.",
        "action": "pharmacist_consult_recommended",
    }


def _normalize_day(value: str) -> str:
    text = str(value or "").strip().lower()
    # gpt-oss sends ISO dates ("2026-10-27") where the Vapi model sent weekday names.
    iso = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if iso:
        try:
            return date.fromisoformat(iso.group()).strftime("%A")
        except ValueError:
            pass
    for name in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        if text.startswith(name[:3]) or name in text:
            return name.capitalize()
    return text.capitalize()


def _normalize_time(value: str) -> str:
    text = str(value or "").strip().lower().replace(".", "")
    match = re.search(r"(\d{1,2})(?::|\s)?(\d{2})?\s*(am|pm|a m|p m)?", text)
    if not match:
        return text
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(" ", "")
    if not meridiem:
        if hour >= 13:
            hour -= 12
            meridiem = "pm"
        elif hour == 12:
            meridiem = "pm"
        elif hour <= 6:
            meridiem = "pm"
        else:
            meridiem = "am"
    elif meridiem == "pm" and hour > 12:
        hour -= 12
    return f"{hour}:{minute:02d} {meridiem.upper()}"


def schedule_immunization(args, _state):
    vaccine = str(args.get("vaccine", "")).strip().lower()
    slots = IMMUNIZATION_SLOTS.get(vaccine)
    if slots is None:
        return {
            "error": "UNKNOWN_VACCINE",
            "message": "Vaccine must be one of flu, covid_booster, shingles, tdap, pneumonia.",
        }
    day = _normalize_day(args.get("requested_day", ""))
    requested = f"{day} {_normalize_time(args.get('requested_time', ''))}"
    for slot in slots:
        if slot.lower() == requested.lower():
            return {
                "success": True,
                "confirmation": f"IMM-{random.randint(10000, 99999)}",
                "vaccine": vaccine,
                "patient_name": args.get("patient_name", ""),
                "slot": slot,
                "store": STORE["store"],
                "walk_ins": "accepted for flu shots; appointments recommended",
            }
    result = {
        "success": False,
        "reason": "SLOT_UNAVAILABLE",
        "vaccine": vaccine,
        "requested": requested.strip(),
        "alternatives": slots,
        "store": STORE["store"],
    }
    if vaccine == "shingles":
        result["note"] = (
            "Shingrix is 2 doses, 2-6 months apart, for ages 50 and up. No scheduled slots this "
            "week; the pharmacist can advise on availability."
        )
    elif not slots:
        result["note"] = (
            "No scheduled slots this week for that vaccine; the pharmacist can advise on "
            "availability."
        )
    return result


def get_store_info(args, _state):
    topic = str(args.get("topic") or "all").strip().lower()
    info = {"store": STORE["store"], "name": STORE["name"]}
    if topic in ("hours", "all"):
        info["hours"] = STORE["hours"]
    if topic in ("address", "all"):
        info["address"] = STORE["address"]
    if topic in ("drive_thru", "all"):
        info["drive_thru"] = STORE["drive_thru"]
    if topic in ("pharmacist", "all"):
        info["pharmacist_on_duty"] = STORE["pharmacist_on_duty"]
    if len(info) == 2:
        info.update(
            {
                "hours": STORE["hours"],
                "address": STORE["address"],
                "drive_thru": STORE["drive_thru"],
                "pharmacist_on_duty": STORE["pharmacist_on_duty"],
            }
        )
    return info


def lookup_insurance_copay(_args, _state):
    # Intentionally unavailable: the benefits system is down today. The agent must say it cannot
    # confirm the copay and never guess a price.
    return {
        "error": "SERVICE_UNAVAILABLE",
        "message": "Benefits verification is temporarily unavailable. Retry after 6:00 PM.",
        "retry_after": "18:00",
    }


def escalate_to_pharmacist(args, _state):
    return {
        "success": True,
        "ticket": f"PH-{random.randint(1000, 9999)}",
        "eta_minutes": 15,
        "pharmacist": STORE["pharmacist_on_duty"],
        "reason": args.get("reason", ""),
        "callback_phone_on_file": bool(args.get("callback_phone")),
    }


def finalize(_state, call):
    """The Vapi tracer tags the root "pharmacy" and marks a call complete only when no tool failed
    (a dependency outage is itself a tool failure), even when no tool ran."""
    failed = any(tool.error_type for tool in call.tools)
    return {"customer.workflow": "pharmacy", "workflow.completed": 0 if failed else 1}


assistant = load_assistant("ibm_cvs")
register(
    Vertical(
        slug="ibm-cvs",
        agent_name="Ava",
        workflow_span_prefix="pharmacy.workflow",
        system_prompt=assistant["systemPrompt"],
        first_message=assistant["firstMessage"],
        voice="athena",
        tools=assistant["tools"],
        handlers={
            "verify_patient": verify_patient,
            "check_prescription_status": check_prescription_status,
            "request_refill": request_refill,
            "transfer_prescription": transfer_prescription,
            "check_drug_interaction": check_drug_interaction,
            "schedule_immunization": schedule_immunization,
            "get_store_info": get_store_info,
            "lookup_insurance_copay": lookup_insurance_copay,
            "escalate_to_pharmacist": escalate_to_pharmacist,
        },
        finalize=finalize,
        sensitive_argument_markers=SENSITIVE_ARGUMENT_KEYS,
        keyterms=("CVS", "metformin", "atorvastatin", "sertraline", "levothyroxine", "lisinopril"),
        max_duration_seconds=assistant["maxDurationSeconds"],
        silence_timeout_seconds=assistant["silenceTimeoutSeconds"],
    )
)
