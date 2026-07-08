"""Prescription transfer request builder — phase 1 (guardrailed phone call).

How transfers actually work in the US: the patient asks the RECEIVING
pharmacy, gives it the medication list and the current pharmacy's details,
and that pharmacy's staff executes the transfer (electronically via NCPDP
RxTransfer where supported, otherwise pharmacist-to-pharmacist). A consumer
app cannot touch the pharmacy-to-pharmacy rail — but it CAN place the
request call. This module builds that call for the existing action layer
(propose → human-confirmed commit → Bland.ai executor; no retries).

Guardrails:
- Schedule II is NEVER transferable under federal rule — matching
  medications are refused with an explanation. The deny-list below is a
  conservative keyword match, NOT an authoritative DEA schedule lookup;
  false positives are acceptable (the patient can still call themselves),
  false negatives are caught by the receiving pharmacist.
- Only active medication orders are included.
- The phone number rides in payload.phone (never in audit output);
  payload.to carries the recipient label only. summary() redaction rules
  in r6/actions/models.py apply unchanged.
"""

from __future__ import annotations

# Conservative Schedule II keyword deny-list (lowercase). Not authoritative.
SCHEDULE_II_TERMS = (
    "oxycodone", "oxycontin", "percocet", "hydrocodone", "vicodin", "norco",
    "fentanyl", "morphine", "hydromorphone", "dilaudid", "oxymorphone",
    "meperidine", "demerol", "methadone", "codeine sulfate",
    "amphetamine", "dextroamphetamine", "adderall", "vyvanse",
    "lisdexamfetamine", "methylphenidate", "ritalin", "concerta", "focalin",
    "dexmethylphenidate", "cocaine", "pentobarbital", "secobarbital",
    "tapentadol", "nucynta",
)


def _med_name(med: dict) -> str:
    concept = med.get("medicationCodeableConcept") or {}
    if concept.get("text"):
        return concept["text"]
    for coding in concept.get("coding", []):
        if coding.get("display"):
            return coding["display"]
    return "unnamed medication"


def _is_schedule_ii(name: str) -> bool:
    lowered = name.lower()
    return any(term in lowered for term in SCHEDULE_II_TERMS)


def _call_script(allowed, to_pharmacy, from_pharmacy) -> str:
    med_lines = "; ".join(m["name"] for m in allowed)
    frm = ""
    if from_pharmacy and from_pharmacy.get("name"):
        frm = f" currently on file at {from_pharmacy['name']}"
        if from_pharmacy.get("phone"):
            frm += f" (phone {from_pharmacy['phone']})"
    return (
        f"Hello, I'm calling on behalf of a patient to request a "
        f"prescription transfer to {to_pharmacy['name']}. The patient would "
        f"like to transfer the following medication(s){frm}: {med_lines}. "
        f"The patient will confirm their identity details (name and date of "
        f"birth) with your pharmacist directly. Could you please initiate "
        f"the transfer and let us know if anything else is needed?"
    )


def build_transfer_request(medication_requests, to_pharmacy,
                           from_pharmacy=None):
    """Pure: MedicationRequests + pharmacy details -> transfer package.

    Returns {allowed, refused, action_payload} where action_payload is ready
    for ProposedAction(kind='phone-call') or None when nothing transferable.
    """
    allowed, refused = [], []
    for med in medication_requests or []:
        if (med.get("status") or "active") != "active":
            continue
        name = _med_name(med)
        if _is_schedule_ii(name):
            refused.append({
                "name": name,
                "reason": ("Schedule II medications cannot be transferred "
                           "between pharmacies under federal rules — a new "
                           "prescription from the prescriber is required."),
            })
            continue
        allowed.append({"name": name})

    if not allowed:
        return {"allowed": [], "refused": refused, "action_payload": None}

    payload = {
        "to": to_pharmacy["name"],
        "phone": to_pharmacy["phone"],
        "body": _call_script(allowed, to_pharmacy, from_pharmacy),
        "rx_transfer": {
            "to_pharmacy": to_pharmacy,
            "from_pharmacy": from_pharmacy,
            "medications": [m["name"] for m in allowed],
        },
    }
    return {"allowed": allowed, "refused": refused, "action_payload": payload}
