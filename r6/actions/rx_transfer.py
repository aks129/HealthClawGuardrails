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
- The match is not keyed on the feed's word for the drug. It used to read
  `medicationCodeableConcept.text` first and stop there, so RxNorm 7804
  (oxycodone) with `text: "pain reliever"` was proposed for transfer, and
  so was a code-only order, which is exactly what a redacted read yields for
  a code the label table does not know. Every name-bearing signal is now
  checked (our own label for the code, the text, every coding display), the
  RxNorm ingredient codes below are checked directly, and an order that
  carries no name at all is refused as unverifiable rather than transferred
  as "unnamed medication".
- Only active medication orders are included.
- The phone number rides in payload.phone (never in audit output);
  payload.to carries the recipient label only. summary() redaction rules
  in r6/actions/models.py apply unchanged.
"""

from __future__ import annotations

from r6.terminology import RXNORM, canonical_system, lookup

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


# RxNorm ingredient-level RxCUIs for the same substances, so a coded order
# is caught even when nothing names it. Ingredient level, because that is
# what a static list can hold; product-level codes (a specific tablet) are
# caught by name wherever the feed or our label table carries one. Not
# authoritative, same caveat as the terms above.
SCHEDULE_II_RXCUI = frozenset({
    "7804",    # oxycodone
    "5489",    # hydrocodone
    "4337",    # fentanyl
    "7052",    # morphine
    "3423",    # hydromorphone
    "7814",    # oxymorphone
    "6813",    # methadone
    "6754",    # meperidine
    "2670",    # codeine
    "725",     # amphetamine
    "3288",    # dextroamphetamine
    "6901",    # methylphenidate
    "700449",  # lisdexamfetamine
    "787390",  # tapentadol
})

UNVERIFIABLE_REASON = (
    "This order carries no medication name and no code the guardrail "
    "recognises, so it cannot be checked against the Schedule II rule and "
    "is not transferred. Ask the prescriber's office or the pharmacy directly."
)


def _codings(med: dict) -> list[dict]:
    concept = med.get("medicationCodeableConcept") or {}
    return [c for c in (concept.get("coding") or []) if isinstance(c, dict)]


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    return [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]


def _label_names(med: dict) -> list[str]:
    """Our own labels for the order's codes (r6/terminology.py)."""
    names = []
    for coding in _codings(med):
        label = lookup(coding.get("system"), coding.get("code"))
        if label:
            names.append(label)
    return _dedupe(names)


def _feed_names(med: dict) -> list[str]:
    """What the feed itself calls the order: `text`, then every display."""
    concept = med.get("medicationCodeableConcept") or {}
    names = []
    if isinstance(concept.get("text"), str) and concept["text"].strip():
        names.append(concept["text"].strip())
    for coding in _codings(med):
        display = coding.get("display")
        if isinstance(display, str) and display.strip():
            names.append(display.strip())
    return _dedupe(names)


def medication_names(med: dict) -> list[str]:
    """Every name the order carries, our own label for the code first.

    The Schedule II check reads all of them; `display_name` decides what a
    person sees.
    """
    return _dedupe(_label_names(med) + _feed_names(med))


def display_name(med: dict) -> str | None:
    """What the review page shows and the call script says.

    Our label identifies the drug, junk-proof and keyed by code. The feed's
    own text rides along as "recorded as" when it says more, because it is
    the order as written (dose, form, frequency) and that is what the
    pharmacist needs and what the person confirms. When we have no label,
    the feed's text is the name. None when nothing names the order.
    """
    labels, feed = _label_names(med), _feed_names(med)
    if not labels:
        return feed[0] if feed else None
    if feed and feed[0].lower() != labels[0].lower():
        return "%s (recorded as: %s)" % (labels[0], feed[0])
    return labels[0]


def _is_schedule_ii(med: dict, names: list[str]) -> bool:
    for coding in _codings(med):
        if (canonical_system(coding.get("system")) == RXNORM
                and str(coding.get("code")) in SCHEDULE_II_RXCUI):
            return True
    return any(term in name.lower()
               for name in names for term in SCHEDULE_II_TERMS)


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
        names = medication_names(med)
        shown = display_name(med) or "unnamed medication"
        if _is_schedule_ii(med, names):
            refused.append({
                "name": shown,
                "reason": ("Schedule II medications cannot be transferred "
                           "between pharmacies under federal rules — a new "
                           "prescription from the prescriber is required."),
            })
            continue
        if not names:
            refused.append({"name": "unnamed medication",
                            "reason": UNVERIFIABLE_REASON})
            continue
        allowed.append({"name": shown})

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
