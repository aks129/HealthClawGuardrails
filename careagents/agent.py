"""The CareAgents agent loop.

One chat turn = run_turn(): a bounded tool loop over the HealthClaw client,
yielding UI events the route streams to the browser as SSE:

    {"type": "tool",   "name": ..., "label": ...}   # a chip appears
    {"type": "card",   "kind": "review", ...}        # review & approve card
    {"type": "card",   "kind": "pdf", ...}           # signed PDF ready
    {"type": "text",   "text": ...}                  # the agent's reply
    {"type": "error",  "text": ...}

The model never sees unredacted data — every tool result comes through the
guardrail layer. Tool results handed to the model are consumer summaries, not
raw bundles, to keep turns small and grounded.
"""

from __future__ import annotations

import json
import datetime
import logging
from typing import Callable
from urllib.parse import quote

from careagents import brief as brief_mod
from careagents import causes, labs_timeline, llm, text_self
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.personas import NO_TREND_VERDICT

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

#: Surfaces that are a phone's text thread: no cards, no charts, no
#: markdown. A card there becomes a link back to the web app.
TEXT_SURFACES = frozenset({"imessage", "sms"})

# Keep a conversation from growing without limit. Nothing trimmed these before,
# so a heavy user's cost per turn climbed forever and eventually the request
# exceeded the model's context window — at which point every further turn for
# that person failed until the process restarted.
MAX_HISTORY_MESSAGES = 40

# The one place patient-facing failure text is decided. Every surface —
# durable worker, streamed browser chat, SMS/iMessage collapse, the app's
# empty-event fallbacks — imports these rather than carrying its own copy;
# four sites had diverging literals, and one of them was yielding str(exc),
# which showed a patient "model call failed (HTTP 429)" verbatim.
# The same sentence as r6/agent_runs/service.py RUN_FAILED_TEXT: either
# side may be the one that fails a run, and both write this answer.
GENERIC_FAILURE_TEXT = "Something went wrong on our side. Try asking again."
RATE_LIMITED_TEXT = (
    "I'm getting more requests than I can answer right now. Nothing is wrong "
    "with your records — try asking again in a moment.")
# The model key is out of credit or refused (#926). Says nothing about
# providers or billing: that is the operator's problem, and the worker
# emails the owner (careagents/worker.py). "We've been told" is that email,
# so it is said only where an owner address is configured to receive it.
UNAVAILABLE_TEXT = "The assistant is unavailable right now; we've been told."
UNAVAILABLE_UNTOLD_TEXT = (
    "The assistant is unavailable right now. Please try again later.")


def failure_text(exc: Exception, *, owner_told: bool = False) -> str:
    """What the person reads when a turn fails.

    A rate limit is not a defect, and saying "something went wrong on our
    side" for one is two failures at once: it invites a bug report for a bug
    that does not exist, and it leaves a patient wondering whether their
    health records are broken. Everything else stays generic on purpose —
    exception internals are never patient-facing text.
    """
    if causes.needs_owner(exc):
        # `owner_told`: the caller emails the owner (CARE_OWNER_EMAILS set).
        return UNAVAILABLE_TEXT if owner_told else UNAVAILABLE_UNTOLD_TEXT
    if isinstance(exc, llm.LLMRateLimited):
        return RATE_LIMITED_TEXT
    return GENERIC_FAILURE_TEXT


#: For the model, when a read failed: what it means and what to say. The
#: exception's own text never goes with it; "search Condition failed (503)"
#: told the model nothing it could say, and a model with nothing else to go
#: on told the person no records were connected (2026-10-12).
READ_FAILED_NOTE = (
    "These records could not be read just now, on our side. They are not "
    "absent: never say the person has no records, that nothing is "
    "connected, or that they have none of this kind. Say plainly that you "
    "could not read their records this time and offer to try again.")


def tool_error_result(exc: Exception) -> str:
    """The tool result for a tool whose HealthClaw read failed."""
    return json.dumps({"error": "records_unavailable",
                       "note": READ_FAILED_NOTE})


def _trim_history(history: list) -> None:
    """Drop the oldest turns in place, cutting only at a safe boundary.

    A tool call and its result must stay together: an assistant message with
    tool_calls whose matching tool results were trimmed away is rejected
    outright by the provider APIs. So rather than slicing at an arbitrary
    index, cut forward to the next plain user message.
    """
    if len(history) <= MAX_HISTORY_MESSAGES:
        return
    start = len(history) - MAX_HISTORY_MESSAGES
    while start < len(history):
        msg = history[start]
        if msg.get("role") == "user" and not msg.get("tool_call_id"):
            break
        start += 1
    if start < len(history):
        del history[:start]

TOOLS = [
    {"name": "get_health_summary",
     "description": ("The person's current conditions, medications, and "
                     "allergies from their records. Use before answering "
                     "anything about their health."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_labs",
     "description": ("Recent lab results with plain-language reference-range "
                     "interpretation (what's normal, what's flagged)."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "show_lab_timeline",
     "description": ("Show a CHART of how a lab value has changed over time. "
                     "Use for any 'timeline', 'trend', 'over time', 'history' "
                     "or 'has it improved' question about a lab (cholesterol, "
                     "LDL, HDL, triglycerides, A1c, glucose) or blood "
                     "pressure. The chart "
                     "appears in the conversation, so describe what it shows "
                     "rather than listing every number."),
     "parameters": {"type": "object", "properties": {
         "topic": {"type": "string",
                   "description": ("What the person asked about, e.g. "
                                   "'cholesterol' or 'a1c'. Omit to show "
                                   "every lab series on file.")},
     }, "required": []}},
    {"name": "get_care_gaps",
     "description": ("Preventive screenings and immunizations that are due "
                     "or coming due (USPSTF/ACIP/ADA guidance)."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "search_records",
     "description": ("Search the person's FHIR records by type. "
                     "MedicationRequest is what was prescribed; "
                     "MedicationStatement is what the record says they "
                     "take. Keep the two apart."),
     "parameters": {"type": "object", "properties": {
         "resource_type": {"type": "string", "enum": [
             "Condition", "Observation", "MedicationRequest",
             "MedicationStatement",
             "AllergyIntolerance", "Immunization", "Procedure"]},
     }, "required": ["resource_type"]}},
    {"name": "appointment_brief",
     "description": ("A short pre-visit brief from the person's records: "
                     "problems, medications, recent labs, screenings due and "
                     "recent visits. Use for 'get me ready for my visit' or "
                     "'what should I bring up with my doctor'."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "start_intake_form",
     "description": ("Start filling the new-patient intake form from the "
                     "person's records. This only PROPOSES the form — a "
                     "review card appears and the person approves every "
                     "medication and allergy themselves before anything is "
                     "generated."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_form_status",
     "description": ("Check an intake form the person already started. Once "
                     "they've reviewed and approved, this returns the signed "
                     "PDF link."),
     "parameters": {"type": "object", "properties": {
         "action_id": {"type": "string"}}, "required": ["action_id"]}},
]

#: Offered only in a turn whose account may be texted (text_self.py): the
#: CARE_ACTIONS_SMS flag admits it, it is not paused, and it has one verified
#: phone. The model picks a purpose and nothing else; CareAgents writes the
#: number, the recipient and the words.
PROPOSE_ACTION_TOOL = {
    "name": "propose_action",
    "description": ("Offer to text the person's own phone a short nudge to "
                    "sign in, for example that their visit prep is ready. "
                    "This only PROPOSES it: a review card appears and "
                    "nothing is sent until the person approves it on the "
                    "review page. A texted yes does not approve. The text "
                    "is fixed and carries nothing from their records; you "
                    "choose only its purpose."),
    "parameters": {"type": "object", "properties": {
        "purpose": {"type": "string", "enum": list(text_self.PURPOSES)},
    }, "required": ["purpose"], "additionalProperties": False}}


def tools_for_turn(sms_phone: str | None) -> list[dict]:
    """The tools one turn offers. propose_action only with a number to text;
    the dispatcher asks again, because a model can call a tool it was not
    offered."""
    return [*TOOLS, PROPOSE_ACTION_TOOL] if sms_phone else TOOLS


TOOL_LABELS = {
    "get_health_summary": "Reading your records",
    "get_labs": "Interpreting your labs",
    "show_lab_timeline": "Charting your results over time",
    "get_care_gaps": "Checking preventive care gaps",
    "search_records": "Searching your records",
    "appointment_brief": "Preparing your visit brief",
    "start_intake_form": "Preparing your intake form",
    "check_form_status": "Checking your form",
    "propose_action": "Preparing a text to your phone",
}

#: TOOL_LABELS on the sample connection, whose records are made up: a status
#: line that said "your labs" read as the tester's own (2026-10-08 walk).
SAMPLE_TOOL_LABELS = {
    "get_health_summary": "Reading the made-up records",
    "get_labs": "Interpreting the made-up labs",
    "show_lab_timeline": "Charting the made-up results over time",
    "get_care_gaps": "Checking screenings and vaccines",
    "search_records": "Searching the made-up records",
    "appointment_brief": "Preparing the sample visit brief",
    "start_intake_form": "Preparing the sample intake form",
    "check_form_status": "Checking the sample form",
    "propose_action": "Preparing a text to the linked phone",
}


# How many DISTINCT Medication references one tool call may chase. Each is a
# separately audited read; a pathological bundle must not turn one chat
# message into an unbounded fan-out.
MAX_MEDICATION_DEREFS = 10

# Types whose name may live behind a medicationReference. A statement carries
# one exactly as a request does (#377).
_MEDICATION_TYPES = ("MedicationRequest", "MedicationStatement")


def _medication_resolver(hc: HealthClawClient, tenant: str):
    """A ref -> label function for medicationReference chasing.

    Real feeds (MEDENT, live 2026-08-04) send MedicationRequest with no
    inline code at all — the name lives on a referenced Medication resource,
    which DOES carry a proper coding. Without following that reference the
    agent told a patient "I can't read the names of these medications" while
    four correctly-coded Medication rows sat in their record.

    Every read goes through hc.read — the same redact-then-relabel gate as
    every other access, each with its own AuditEvent. The label that comes
    back is therefore server-derived, never upstream text. Failures return
    None and the item stays unreadable-not-absent; per-call memo so one ref
    costs one read; capped so a junk bundle cannot fan out.
    """
    memo: dict[str, tuple[str | None, str]] = {}

    def resolve(ref) -> tuple[str | None, str]:
        """Return (label, reason).

        The reason is the point. This used to return a bare label, so four
        different outcomes arrived at the caller as one `None` and the caller
        explained all of them to the patient as "the source sent free text"
        — a claim about the upstream feed, made by a branch that had learned
        nothing about the upstream feed. PR #376 fixed one cause of that
        sentence; this stops the remaining three from producing it.
        """
        if not isinstance(ref, str) or not ref.startswith("Medication/"):
            return None, "not-a-ref"
        if ref in memo:
            return memo[ref]
        if len(memo) >= MAX_MEDICATION_DEREFS:
            # Never looked at. Anything said about how the source coded it
            # would be invented.
            return None, "not-attempted"
        label = None
        reason = "no-label"
        try:
            med = hc.read(tenant, "Medication", ref.split("/", 1)[1])
            code = med.get("code") or {}
            label = code.get("text") or next(
                (c.get("display") for c in (code.get("coding") or [])
                 if isinstance(c, dict) and c.get("display")), None)
            if isinstance(label, str):
                label = label.strip() or None
            else:
                label = None
            reason = "resolved" if label else "no-label"
        except HealthClawError:
            reason = "unavailable"
        memo[ref] = (label, reason)
        return label, reason

    return resolve


ALLERGY_STATUS_STATEMENT = (
    "Your records don't say whether you have allergies.")

#: Explicit SNOMED allergy-status concepts: the record says allergy
#: information was not collected, not which substance. Keyed by system and
#: code, never by label, and kept tiny on purpose. Coding-level null flavors
#: and data-absent-reason codes are NOT here: C-CDA writes an uncoded
#: allergen as nullFlavor UNK beside free text that redaction strips, so
#: those are real allergy records (#945 security F3). "No known allergy"
#: codes are not status either: they are _NKA_CODES below.
_ALLERGY_STATUS_CODES = frozenset({
    ("http://snomed.info/sct", "1631000175102"),  # Patient not asked
})

#: "No known allergy" concepts: SNOMED 716186003 and every descendant
#: (tx.fhir.org, isa/716186003, 2026-10-13), each with our own words for
#: it. The words are keyed by code, never taken from the record: the
#: terminology label ("No known allergy") reached the model as an allergy's
#: name, a bare fact about the person (#946).
_NKA_CODES = {
    ("http://snomed.info/sct", "716186003"): "no known allergies",
    ("http://snomed.info/sct", "409137002"): "no known drug allergies",
    ("http://snomed.info/sct", "428197003"): "no known insect allergies",
    ("http://snomed.info/sct", "428607008"):
        "no known environmental allergies",
    ("http://snomed.info/sct", "429625007"): "no known food allergies",
    ("http://snomed.info/sct", "716220001"): "no known animal allergies",
    ("http://snomed.info/sct", "1003774007"): "no known latex allergies",
    # Inactive predecessor of 716186003, still in legacy feeds (#957).
    ("http://snomed.info/sct", "160244002"): "no known allergies",
    # "None", which named an allergy "None" (#957 delta 4).
    ("http://snomed.info/sct", "260413007"): "allergies as none",
    # LOINC answers stating an absence (r6.terminology.LOINC_ABSENCE_ANSWERS).
    # Missing here, LA10030-7 read as an ordinary allergy named by the
    # engine's qualified text, and undated (#957 re-QA).
    ("http://loinc.org", "LA10030-7"): "allergies as none known",
    ("http://loinc.org", "LA9134-3"): "allergies as none known",
    ("http://loinc.org", "LA4715-4"): "allergies as none or not known",
}


#: The spellings of SNOMED's and LOINC's systems the engine reads as each,
#: mirrored from r6.terminology._SYSTEM_ALIASES (CareAgents imports nothing
#: from the engine; tests hold the two equal).
_SNOMED_SYSTEMS = frozenset({"http://snomed.info/sct",
                             "urn:oid:2.16.840.1.113883.6.96"})
_LOINC_SYSTEMS = frozenset({"http://loinc.org", "https://loinc.org",
                            "urn:oid:2.16.840.1.113883.6.1"})


def _coding_key(c: object) -> tuple[str, str] | None:
    """(system, code) as the engine keys a coding before it labels it
    (r6.terminology.canonical_system and lookup: stripped, the SNOMED OID
    read as SNOMED, an int code read as its digits), or None when it is not
    one we can key on. Keyed any other way, a coding the engine labels "No
    known allergy" missed our sets and its label became a name (#949 F1).
    Strings only: a list in `code` is unhashable, and ingest stores
    whatever arrived (#885)."""
    if not isinstance(c, dict):
        return None
    system, code = c.get("system"), c.get("code")
    if (not isinstance(system, str) or isinstance(code, bool)
            or not isinstance(code, (str, int))):
        return None
    system, code = system.strip(), str(code).strip()
    if not system or not code:
        return None
    if system in _SNOMED_SYSTEMS:
        system = "http://snomed.info/sct"
    elif system in _LOINC_SYSTEMS:
        system = "http://loinc.org"
    return (system, code)


def _coding_keys(res: dict) -> list | None:
    """The key of every coding, or None when there is none or any one is
    not a coding we can key on. A coding we cannot key on is still a
    coding, never a reason to read the record as anything but an allergy
    (F4)."""
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    if not isinstance(codings, list) or not codings:
        return None
    keys = [_coding_key(c) for c in codings]
    return None if None in keys else keys


def _carries_absence_code(res: dict) -> bool:
    """Any coding at all that keys to a status or "no known allergy"
    concept, whatever else the record carries. Such a record's text and
    displays are never its name: the engine writes that concept's label
    there ("No known allergy", "Patient not asked"), and a record that
    failed the checks above would hand it to the model as an allergy
    (#949 F1)."""
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    return isinstance(codings, list) and any(
        k in _ALLERGY_STATUS_CODES or k in _NKA_CODES
        for k in map(_coding_key, codings) if k)


def _only_absence_codes(res: dict) -> bool:
    """Every coding keys to a status or "no known allergy" concept, and
    there is at least one. Any other coding, odd or not, makes it false:
    such a record stays, unlabeled (the backstop above)."""
    keys = _coding_keys(res)
    return bool(keys) and all(
        k in _ALLERGY_STATUS_CODES or k in _NKA_CODES for k in keys)


#: What a real allergy coded beside an absence concept is called when its
#: substance has no terminology label (FHI-48). Never an absence, never
#: dropped, never the record's own words.
ALLERGY_UNNAMED = "an allergy, name not recognised"


def _substance_label(res: dict) -> str:
    """The label on the first coding that keys to neither a status nor a
    "no known allergy" concept, or "". `code.text` and an absence coding's
    display are never read, and a coding we cannot key on is no name
    either (FHI-48).

    INVARIANT this relies on: the engine strips every upstream `display`
    and `text` (r6.redaction.apply_redaction) and only then writes back a
    label from r6/terminology.py, keyed by code. So the only `display` that
    reaches CareAgents is the terminology label, as for every other record
    named in _summarize_bundle. Pinned through the real engine, with a name
    planted in the upstream display, by
    tests/test_careagents_mixed_absence_allergy.py."""
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    for c in codings if isinstance(codings, list) else []:
        k = _coding_key(c)
        if k is None or k in _ALLERGY_STATUS_CODES or k in _NKA_CODES:
            continue
        display = c.get("display")
        if isinstance(display, str) and display.strip():
            return display
    return ""


#: verificationStatus codes that withdraw an entry (FHIR
#: allergyintolerance-verification). Read from any coding, system or not:
#: a wrong match here only stops a "no known allergy" entry being quoted.
_WITHDRAWN = frozenset({"entered-in-error", "refuted"})


def _withdrawn(res: dict) -> bool:
    vs = res.get("verificationStatus")
    if isinstance(vs, str):  # STU3 wrote a bare code
        return vs.strip() in _WITHDRAWN
    codings = vs.get("coding") if isinstance(vs, dict) else None
    return isinstance(codings, list) and any(
        isinstance(c, dict) and isinstance(c.get("code"), str)
        and c["code"].strip() in _WITHDRAWN for c in codings)


def _is_allergy_status(res: dict) -> bool:
    """True only when the record carries at least one explicit status
    concept and NO other coding of any kind, well formed or not. Penicillin
    beside a status code is a penicillin allergy (F1). A "no known allergy"
    coding beside a status code reads as status: a record that also says
    "not asked" does not say there are none (#946)."""
    keys = _coding_keys(res)
    return (bool(keys) and any(k in _ALLERGY_STATUS_CODES for k in keys)
            and all(k in _ALLERGY_STATUS_CODES or k in _NKA_CODES
                    for k in keys))


def _nka_kinds(res: dict) -> list[str]:
    """Our words for a "no known allergy" record, or [] when it is not one.
    Every coding must be in _NKA_CODES, as for status: any other coding
    makes it an allergy record."""
    keys = _coding_keys(res)
    if not keys or not all(k in _NKA_CODES for k in keys):
        return []
    return list(dict.fromkeys(_NKA_CODES[k] for k in keys))


def _nka_when(item: dict) -> str:
    """The date clause for an entry: what kind of date it is, said as that."""
    return {"recorded": f", recorded on {item.get('date')},",
            "onset": f", dated {item.get('date')},",
            "last_updated": (", last updated in your records on "
                             f"{item.get('date')},"),
            }.get(item.get("date_kind"), "")


def _nka_item(res: dict, kinds: list[str]) -> dict:
    """A "no known allergy" record as what it is: an entry in the records,
    dated and quoted, never a fact about the person. Never "no known
    allergies" inferred: this repeats what someone recorded and says when,
    so a stale entry reads as one (#946)."""
    item = {"type": "AllergyIntolerance", "no_known_allergy_record": True}
    # The clinic's dates first. meta.lastUpdated is the engine's own write
    # time (r6/models.py), when the entry arrived here: said as that, never
    # as when it was recorded. onsetDateTime only, never onsetString.
    meta = res.get("meta")
    for kind, raw in (("recorded", res.get("recordedDate")),
                      ("onset", res.get("onsetDateTime")),
                      ("last_updated", meta.get("lastUpdated")
                       if isinstance(meta, dict) else None)):
        date = labs_timeline.parse_date(raw)
        if date:
            item.update({"date": date, "date_kind": kind})
            break
    lists = " and ".join(kinds)
    if item.get("date_kind") == "last_updated":
        tail = "; the records don't show when it was first recorded"
    elif "date" in item:
        tail = ""
    else:
        tail = "; the records don't show when it was recorded"
    item["statement"] = (f"Your records include an entry{_nka_when(item)} "
                         f"that lists {lists}{tail}. Please confirm with your "
                         "doctor or pharmacist.")
    item["note"] = (
        "This is an entry in the person's records, not a fact about them: "
        "someone recorded it, and it may be out of date. Give the statement "
        "in these plain words, as a dated record. Never say they have no "
        "allergies, and always tell them to confirm with their doctor or "
        "pharmacist.")
    return item


#: Records per list the tools hand the model: the engine's search page (50,
#: r6/routes.py), so a list one search can return is never cut here. At 12,
#: a 19-condition record lost diabetes, hypertension and atrial
#: fibrillation, and no follow-up could reach them (#945 patient tester).
SUMMARY_LIMIT = 50


def _summarize_bundle(bundle: dict, limit: int = 12,
                      resolve_ref=None) -> list[dict]:
    """Compact, model-friendly view of a searchset bundle (already redacted).

    Truncates to `limit` and SAYS SO. The list this returns is the model's
    entire view of the person's records for that resource type, so a silent
    cut is indistinguishable from "that is all there is" — a person on 30
    medications would be told about 12, in a confident complete-sounding
    sentence.

    That is the same mistake as #207, four lines below: dropping an unreadable
    record's name made the model report the condition as ABSENT. We fixed
    unreadable-is-not-absent and left truncated-is-not-absent in the same
    function. It cannot fire on the synthetic demo tenant, which is why
    nothing caught it; it fires on the first real record with a long list.
    """
    entries = bundle.get("entry") or []
    total = sum(1 for e in entries
                if (e.get("resource") or {}).get("resourceType")
                != "OperationOutcome")
    out = []
    dropped = 0
    withdrawn_nka = False
    for entry in entries[:limit]:
        res = entry.get("resource") or {}
        rt = res.get("resourceType")
        if rt == "OperationOutcome":
            continue
        item = {"type": rt}
        if rt != "AllergyIntolerance" and _only_absence_codes(res):
            # Coded only as "no known allergy" or "not asked", on a list
            # that is not allergies: no clinical finding, so not a record of
            # this kind. Left out, and not counted, so the list does not
            # read as longer than shown. Never an allergy statement either:
            # that is AllergyIntolerance's alone (#949 patient tester).
            dropped += 1
            continue
        if rt == "AllergyIntolerance" and _is_allergy_status(res):
            # Not an allergy: a note that none were recorded. Its label
            # ("Patient not asked", the terminology service's for SNOMED
            # 1631000175102) reached a person verbatim as if it were one.
            # Never read as "no known allergies" either: that is only ever
            # the person's own attestation.
            item.update({
                "not_an_allergy": True,
                "statement": ALLERGY_STATUS_STATEMENT,
                "note": ("This record is not an allergy. Tell the person, in "
                         "these plain words, that their records don't say "
                         "whether they have allergies. Do not quote the "
                         "record, and never say they have no allergies.")})
            out.append(item)
            continue
        nka = _nka_kinds(res) if rt == "AllergyIntolerance" else []
        if nka and _withdrawn(res):
            # Entered in error or refuted: the entry says nothing either
            # way (#949 F2), so it is not sent at all. Sent with a "do not
            # mention it" note, the model mentioned it and disclosed the
            # note (#949 real-model QA): what must not reach the person is
            # never handed to the model. Not counted either. A withdrawn
            # REAL allergy is kept: dropping it would let a "no known
            # allergies" entry beside it be quoted.
            dropped += 1
            withdrawn_nka = True
            continue
        if nka:
            # Before the name below: that is where the resolver's label
            # "No known allergy" became an allergy's name (#946).
            out.append(_nka_item(res, nka))
            continue
        code = res.get("code") or res.get("medicationCodeableConcept") or {}
        # Ingest stores whatever arrived: a string `code` or `coding`
        # raised here and failed the whole turn (#949 security round 4).
        # Malformed reads as an unreadable record, never a name or absence.
        if not isinstance(code, dict):
            code = {}
        if not isinstance(code.get("coding"), list):
            code = {**code, "coding": []}
        first = (code["coding"] or [None])[0]
        text = code.get("text") or (
            first.get("display") if isinstance(first, dict) else "")
        text = text if isinstance(text, str) else ""
        if _carries_absence_code(res):
            # Backstop: whatever the checks above made of it, a record
            # with a status or "no known allergy" coding is never named by
            # its text, which may hold that concept's label (#949 F1). Every
            # type, not allergies alone: feeds put "No known allergy" on
            # the problem list too, and it read as a fact beside an empty
            # allergy list (#949 F3). A substance coded beside the absence
            # concept is a real allergy, named by that coding's label
            # alone (FHI-48); an allergy with no label says so plainly.
            text = _substance_label(res)
            if not text and rt == "AllergyIntolerance":
                # Still a real record the model must not treat as absent:
                # the unreadable note below says how to talk about it.
                item["unreadable"] = True
                text = ALLERGY_UNNAMED
        lookup_reason = None
        if not text and resolve_ref is not None:
            # No inline code — the name may live behind medicationReference.
            ref = (res.get("medicationReference") or {}).get("reference")
            if ref:
                text, lookup_reason = resolve_ref(ref)
                text = text or ""
        if text:
            item["name"] = text.strip()
        else:
            # A record exists but carries no readable label. Say so explicitly
            # and pass the raw code through: dropping the name key entirely
            # made the model report the condition as ABSENT (#207).
            # Unreadable is not absent.
            #
            # Two distinct situations hide here, and they deserve different
            # sentences because they have different remedies:
            #
            # - A code is present but our label table doesn't know it. That is
            #   OUR gap; "unlabeled record, code X" is accurate and the code
            #   gives the person something to look up or ask about.
            # - Nothing codeable ever existed. Live example (MEDENT,
            #   2026-08-04): an AllergyIntolerance whose only coding carried
            #   free text — which redaction rightly strips, because real
            #   feeds put patient names there. No table can ever fix that
            #   row. Calling it "unreadable to me" sounds like our failure
            #   and tells the person nothing; "recorded but not coded at the
            #   source" is what actually happened and points at the fix
            #   (ask the clinic to code it / confirm details at the visit).
            #
            # The record is named with the system its coding comes from,
            # never the code itself (`_CODE_SYSTEM_NAMES`).
            codings = [c for c in (code.get("coding") or [])
                       if isinstance(c, dict) and c.get("code")]
            system = next((_CODE_SYSTEM_NAMES[c.get("system")]
                           for c in codings
                           if c.get("system") in _CODE_SYSTEM_NAMES), None)
            if system:
                item["name"] = f"unlabeled record ({system} code)"
            elif codings:
                item["name"] = "unlabeled record"
            elif lookup_reason in ("unavailable", "not-attempted",
                                  "not-a-ref"):
                # We did not learn anything about this record's coding, so we
                # say nothing about it. "The source sent free text" below is a
                # finding; asserting it here would be inventing one — the
                # defect PR #376 fixed, reached by a different route.
                #
                # "not-a-ref" belongs here too: a #contained or urn:uuid:
                # target is ordinary FHIR that we decline to chase, so we
                # learn nothing about its coding either. Routing it to the
                # sentence below was the fourth way into the same falsehood.
                item["name"] = "a medication I could not look up just now"
                item["note"] = (
                    "The name is stored behind a reference this turn could "
                    "not read, so it is missing for a reason on OUR side, not "
                    "the clinic's. Do not describe how the source recorded "
                    "it. It is still a real record — never treat it as "
                    "absent; offer to try again.")
            else:
                item["name"] = "recorded but not coded at the source"
                item["uncoded"] = True
                item["note"] = (
                    "The source system sent this record as free text with no "
                    "standard code, and free text is removed for privacy. It "
                    "is still a real record — never treat it as absent; "
                    "suggest confirming the details with the clinician.")
            item["unreadable"] = True
        # Every field below is checked against a closed set or a parse before
        # it is copied: ingest accepts any string in `status` or a date, and
        # whatever is copied verbatim is a channel to the model (#922).
        if res.get("status") in _STATUS.get(rt, ()):
            item["status"] = res["status"]
        vq = res.get("valueQuantity")
        if isinstance(vq, dict) and vq.get("value") is not None:
            # The coded, allow-listed unit, never the free-text `unit`:
            # upstream can write a name or an instruction there. A unit we
            # do not recognise means no number, as on get_labs (#884 QA F1).
            unit = labs_timeline.coded_unit(
                vq, _ANALYTE_KEY.get(_obs_loinc(res) or ""), _RECORD_UNITS)
            if unit is not None:
                item["value"] = f"{vq.get('value')} {unit}".strip()
        date = labs_timeline.parse_date(res.get("effectiveDateTime"))
        if date:
            item["date"] = date
        if item.get("unreadable") and "note" not in item:
            # A coded record we cannot name. "I couldn't read it... I can't
            # rule anything out" read to people as a mystery illness (#949
            # patient tester): it is one entry to ask about, no more.
            entry = f"one entry from {date}" if date else "one entry"
            item["note"] = (
                f"Say plainly that {entry} has no readable name here, and "
                "that their doctor can say what it is. It is still a real "
                "record, never absent. Do not guess what it is or suggest it "
                "could be serious, and never say you can't rule anything "
                "out.")
        out.append(item)

    # The bundle's total, when the engine gives one: the entries are one page
    # (50 by default), so counting them calls a longer list complete.
    stated = bundle.get("total")
    if isinstance(stated, int) and not isinstance(stated, bool):
        total = max(total, stated)
    total -= dropped
    if total > len(out) or any(i.get("type") == "AllergyIntolerance"
                               and not i.get("not_an_allergy") for i in out):
        # "Your records don't say whether you have allergies" holds only for
        # a list known in full to be status records alone. A recorded
        # allergy in this list (#945 security F2), or a cut list whose rest
        # we have not seen (F5), makes it false. Then the status records
        # are not sent at all: sent with a "do not mention" note, such an
        # entry was mentioned and the note disclosed (#949 real-model QA).
        # Taken off the total too, so the list does not read as cut for
        # them, and a list cut before stays cut.
        status = [i for i in out if i.get("not_an_allergy")]
        out = [i for i in out if not i.get("not_an_allergy")]
        total -= len(status)
    # A "no known allergy" entry is quoted only from a list known in full
    # with no allergy in it. Beside a recorded allergy, the allergy wins and
    # the entry is a disagreement to confirm; in a cut list, the rest may
    # hold an allergy. Either way it never says "no known" (#946).
    conflict = any(i.get("type") == "AllergyIntolerance"
                   and not i.get("not_an_allergy")
                   and not i.get("no_known_allergy_record") for i in out)
    for i in out:
        if not i.get("no_known_allergy_record"):
            continue
        if conflict:
            i["statement"] = (
                f"Your records also include an entry{_nka_when(i)} that "
                "disagrees with the allergies listed. Please confirm your "
                "allergies with your doctor or pharmacist.")
            i["note"] = (
                "The records disagree: they list an allergy, and this entry "
                "says otherwise. The allergy listed wins: never say the "
                "person has no known allergies. Say plainly that their "
                "records disagree, name the allergies listed, and tell them "
                "to confirm with their doctor or pharmacist.")
        elif total > len(out):
            i["statement"] = (
                "Your allergy list is longer than can be shown here, so it "
                "can't be read as complete. Please confirm your allergies "
                "with your doctor or pharmacist.")
            i["note"] = (
                "More allergy records are on file than are listed, and any "
                "of them may be an allergy. Never say the person has no "
                "known allergies and do not quote this entry. Say their "
                "allergy list is longer than you can show in this chat, that "
                "the full list is in their patient portal, and tell them to "
                "confirm with their doctor or pharmacist.")
    if total > len(out):
        # Deliberately shaped so it cannot be mistaken for a record: no
        # "type", no "name". The instruction is carried in the payload rather
        # than left to the system prompt alone, because this is the one place
        # the model can tell complete from partial.
        #
        # No counts in it: handed "12 of 19", the model told a patient "I
        # could only see part of the records (12 of 19)", which reads as
        # being blocked from their own records (#939).
        out.append({
            "truncated": True,
            #
            # And no offer to narrow it or show the rest: no tool can, and
            # the engine has no offset to page with. "Show me the rest"
            # returned the same list (#945 patient tester).
            "note": ("This list is longer than you can show here: more "
                     "records of this kind are on file than are listed. Do "
                     "not describe it as complete, do not count it, and do "
                     "not say how many you were shown. Answer from what is "
                     "here, then say plainly that their list is longer than "
                     "you can show in this chat and that the full list is in "
                     "their patient portal. Do not offer to show the rest "
                     "or to look again: you cannot."),
        })
    elif withdrawn_nka and not any(i.get("type") == "AllergyIntolerance"
                                   for i in out):
        # Only withdrawn entries were here, and none is sent. An empty
        # allergy list must not read as no allergies; every word below is
        # one the person may hear, and none says why the list is empty.
        out.append({"note": (
            "No allergy entries can be shown here. That does not mean they "
            "have no allergies: say their records here show no allergy "
            "entries, and ask them to confirm their allergies with their "
            "doctor or pharmacist.")})
    return out


#: Fields per brief section handed to the model; the page has the rest.
MAX_BRIEF_FIELDS = 6


#: Units a lab reading may carry to the model: the closed list in
#: labs_timeline, which builds the chart's series from it too.
_UCUM_ALLOWED = labs_timeline.UCUM_ALLOWED

#: Units an Observation in a record list may carry: the lab list plus the
#: vital-sign units a search_records answer shows. Still closed, for the
#: same reason (R875-3).
_RECORD_UNITS = _UCUM_ALLOWED | frozenset({
    "kg", "g", "[lb_av]", "lb", "cm", "m", "[in_i]", "in", "kg/m2",
    "/min", "{beats}/min", "bpm", "{breaths}/min", "Cel", "[degF]",
    "h", "min",
    # Units our own wearable mapper writes (r6/wearables/mapper.py), and
    # real lab and vital codes #922 QA found dropped (F3).
    "{count}", "steps", "mL/min/kg", "breaths/min", "°C", "°F",
    "ng/L", "mL/min", "{cells}/uL", "{SG}", "{#}/[HPF]", "mL", "L/min",
    "U/mL", "kU/L", "ug/mL", "mg/24.h", "/uL", "[iU]/mL", "mg/kg", "1",
    "a", "d", "wk", "mo", "lbs", "beats/min", "mg/dl",
})

#: The code systems an unlabeled record can be said to come from. The code
#: itself never reaches the model: redaction keeps codes, and upstream can
#: put a name, a phone number or an instruction in `code`. Per-system shape
#: checks kept admitting some (an SSN-shaped SNOMED id with a valid check
#: digit, a 7-digit phone as RxNorm; #922 re-tests), and the number adds
#: nothing the model needs: the record exists, uncoded at the source.
_CODE_SYSTEM_NAMES = {
    "http://loinc.org": "LOINC",
    "http://snomed.info/sct": "SNOMED CT",
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://hl7.org/fhir/sid/icd-10": "ICD-10",
    "http://hl7.org/fhir/sid/icd-9-cm": "ICD-9-CM",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
}


#: The `status` values FHIR defines per resource type (R4 through R6). A
#: status passes to the model only when it is one of its type's; a type not
#: listed here, or one with no `status` element (Condition,
#: AllergyIntolerance), passes none (#922 B1).
_STATUS = {
    "Observation": frozenset({
        "registered", "preliminary", "final", "amended", "corrected",
        "cancelled", "entered-in-error", "unknown", "cannot-be-obtained"}),
    "MedicationRequest": frozenset({
        "active", "on-hold", "ended", "stopped", "completed", "cancelled",
        "entered-in-error", "draft", "unknown"}),
    "MedicationStatement": frozenset({
        "active", "completed", "entered-in-error", "intended", "stopped",
        "on-hold", "unknown", "not-taken", "recorded", "draft"}),
    "Immunization": frozenset({"completed", "entered-in-error", "not-done"}),
    "Procedure": frozenset({
        "preparation", "in-progress", "not-done", "on-hold", "stopped",
        "completed", "entered-in-error", "unknown"}),
    "DiagnosticReport": frozenset({
        "registered", "partial", "preliminary", "modified", "final",
        "amended", "corrected", "appended", "cancelled", "entered-in-error",
        "unknown"}),
    "Encounter": frozenset({
        "planned", "arrived", "triaged", "in-progress", "onleave",
        "finished", "on-hold", "discharged", "completed", "cancelled",
        "discontinued", "entered-in-error", "unknown"}),
    "CarePlan": frozenset({
        "draft", "active", "on-hold", "revoked", "completed",
        "entered-in-error", "unknown"}),
    "ServiceRequest": frozenset({
        "draft", "active", "on-hold", "revoked", "completed",
        "entered-in-error", "unknown"}),
    "DocumentReference": frozenset({
        "current", "superseded", "entered-in-error"}),
}


def _coded_unit(reading: dict, series: dict) -> str | None:
    """`labs_timeline.coded_unit` for a reading in a series: the stated
    allow-listed unit, the analyte's known one when none was stated, or None
    for a unit we do not recognise (#884 QA F1)."""
    return labs_timeline.coded_unit(reading, series.get("key"))


#: The engine's analyte name per LOINC code, mirrored from
#: r6.labs.interpret.LOINC_RANGES (CareAgents imports nothing from the
#: engine; a test holds the two equal). The labs pairing checks each
#: consumer line's analyte against it.
ENGINE_ANALYTE_LABELS = {
    "2951-2": "Sodium", "2823-3": "Potassium", "2075-0": "Chloride",
    "2028-9": "Carbon dioxide", "3094-0": "Urea nitrogen (BUN)",
    "2160-0": "Creatinine", "2345-7": "Glucose", "17861-6": "Calcium",
    "33914-3": "eGFR", "718-7": "Hemoglobin",
    "6690-2": "White blood cell count", "777-3": "Platelets",
    "2093-3": "Total cholesterol", "13457-7": "LDL cholesterol",
    "2085-9": "HDL cholesterol", "2571-8": "Triglycerides",
    "4548-4": "Hemoglobin A1c",
}


def _obs_date(resource: dict) -> str | None:
    """The reading's calendar date, if it starts with a real YYYY-MM-DD;
    else None. Never a cut string: anything can sit in the field (R875-4)."""
    return labs_timeline.parse_date(
        resource.get("effectiveDateTime") or resource.get("issued")) or None


_LOINC_SYSTEM = "http://loinc.org"
_INTERPRETATION = ("http://terminology.hl7.org/CodeSystem/"
                   "v3-ObservationInterpretation")
#: LOINC -> labs_timeline analyte key, for the known-unit fallback.
_ANALYTE_KEY = {code: a["key"] for a in labs_timeline.ANALYTES
                for code in a["codes"]}


def _obs_loinc(resource: dict) -> str | None:
    code = resource.get("code")
    coding = code.get("coding") if isinstance(code, dict) else None
    for c in (coding if isinstance(coding, list) else []):
        if isinstance(c, dict) and c.get("system") == _LOINC_SYSTEM:
            return c.get("code")
    return None


def _obs_flag(resource: dict) -> str | None:
    # An unscored reading's interpretation is the feed's own, any JSON.
    for i in labs_timeline._dicts(resource.get("interpretation")):
        for c in labs_timeline._dicts(i.get("coding")):
            if c.get("system") == _INTERPRETATION:
                return c.get("code")
    return None


def _latest_per_analyte(consumer: dict, bundle: dict) -> list[dict] | None:
    """The latest reading per analyte, with its date and how many earlier
    readings there are, or None when the engine's answer cannot be read
    that way.

    `$interpret` gives one consumer line per scored result, in the order of
    the annotated Observations in its bundle, and only the scored ones carry
    an interpretation. The two are paired in that order and checked flag by
    flag; any disagreement falls back to the lines as they came. Labels and
    messages are the engine's (its code table); the unit is the coded one
    (`_coded_unit`), never the free-text `unit`.
    """
    lines = consumer.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    scored = [(e or {}).get("resource") or {}
              for e in (bundle.get("entry") or [])]
    scored = [r for r in scored if r.get("resourceType") == "Observation"
              and _obs_flag(r)]
    # Flag AND analyte, the analyte against the engine's own name for the
    # Observation's LOINC code: a line paired with the wrong reading would
    # put one test's message beside another's value and date.
    if len(scored) != len(lines) or any(
            not isinstance(line, dict) or line.get("flag") != _obs_flag(r)
            or line.get("analyte") != ENGINE_ANALYTE_LABELS.get(
                _obs_loinc(r) or "")
            for line, r in zip(lines, scored)):
        return None
    groups: dict[str, list[tuple[tuple, dict, dict]]] = {}
    for line, r in zip(lines, scored):
        date = _obs_date(r)
        # A parsed date orders the readings, its full timestamp breaks a
        # same-day tie; a reading with no valid date sorts first and keeps
        # the Observations' order.
        raw = str(r.get("effectiveDateTime") or r.get("issued") or "")
        order = (date is not None, date or "", raw if date else "")
        groups.setdefault(_obs_loinc(r), []).append((order, line, r))
    out = []
    for key, readings in groups.items():
        readings = sorted(readings, key=lambda x: x[0])
        (_, date, _), line, r = readings[-1]
        vq = r.get("valueQuantity") or {}
        value = vq.get("value")
        unit = _coded_unit(vq, {"key": _ANALYTE_KEY.get(key)})
        if unit is None:
            # A unit we do not recognise: no number rather than a number
            # beside the wrong unit (#884 QA F1).
            value, unit = None, ""
        out.append({
            "analyte": line.get("analyte"),
            "date": date or None,
            "value": (value if isinstance(value, (int, float))
                      and not isinstance(value, bool) else None),
            "unit": unit,
            "flag": line.get("flag"),
            "message": line.get("message"),
            "earlier_readings": len(readings) - 1,
        })
    return out


#: The two parts of a blood pressure, by LOINC code. A reading made of
#: parts is given a number only when it is exactly these two.
_SYSTOLIC, _DIASTOLIC = "8480-6", "8462-4"


def _plain_value(resource: dict) -> tuple[object, str]:
    """(value, unit) for a reading the range check did not score, or
    (None, "") when there is no number we can stand behind: no number, or a
    unit we do not recognise (#884 QA F1). A blood pressure is its two parts,
    as "138/88"."""
    key = _ANALYTE_KEY.get(_obs_loinc(resource) or "")
    vq = resource.get("valueQuantity")
    if isinstance(vq, dict):
        value = vq.get("value")
        unit = labs_timeline.coded_unit(vq, key)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and unit):
            # A number with no unit at all, for a test whose usual unit we
            # do not know, is a number the reader would put a unit beside.
            return value, unit
        return None, ""
    parts = {}
    components = resource.get("component")
    if not isinstance(components, list):
        return None, ""
    for c in components:
        if not isinstance(c, dict):
            return None, ""
        code = _obs_loinc(c)
        cvq = c.get("valueQuantity") or {}
        value = cvq.get("value") if isinstance(cvq, dict) else None
        unit = (labs_timeline.coded_unit(cvq, None)
                if isinstance(cvq, dict) else None)
        if (code in parts or not isinstance(value, (int, float))
                or isinstance(value, bool) or not unit):
            return None, ""
        parts[code] = (value, unit)
    if set(parts) != {_SYSTOLIC, _DIASTOLIC} or \
            parts[_SYSTOLIC][1] != parts[_DIASTOLIC][1]:
        return None, ""
    return (f"{parts[_SYSTOLIC][0]:g}/{parts[_DIASTOLIC][0]:g}",
            parts[_SYSTOLIC][1])


def _not_evaluated(consumer: dict, bundle: dict) -> list[dict] | None:
    """Each reading the range check did not score: the engine's name for it,
    its value and date, and whether the record itself carried a reference
    range. None when the engine's answer cannot be read that way.

    "Could not be interpreted" is what reached the person for a blood
    pressure of 138/88 and a glucose of 180 (#937): true of the check, and no
    use to them. The reading itself is. The pairing is the engine's own
    order, checked code by code against `unevaluated_codes`; any mismatch
    gives None and the engine's note stands alone. The label is the
    engine's (r6/terminology.py by code), never the reading's own text.
    """
    names = consumer.get("unevaluated_analytes")
    codes = consumer.get("unevaluated_codes")
    if not isinstance(names, list) or not isinstance(codes, list) \
            or len(names) != len(codes) or not names:
        return None
    unscored = [(e or {}).get("resource") or {}
                for e in (bundle.get("entry") or [])]
    unscored = [r for r in unscored if r.get("resourceType") == "Observation"
                and not _obs_flag(r)]
    if len(unscored) != len(names) or any(
            not isinstance(name, str) or not code or _obs_loinc(r) != code
            for name, code, r in zip(names, codes, unscored)):
        return None
    # One entry per analyte, its latest reading, as _latest_per_analyte does
    # for scored ones: listing a BP history left the model to order raw
    # dates and work out a direction itself (QA #1021, #942).
    groups: dict[str, list[tuple[tuple, dict]]] = {}
    for name, r in zip(names, unscored):
        date = _obs_date(r)
        raw = str(r.get("effectiveDateTime") or r.get("issued") or "")
        groups.setdefault(name, []).append(
            ((date is not None, date or "", raw if date else ""), r))
    out = []
    for name, readings in groups.items():
        r = sorted(readings, key=lambda x: x[0])[-1][1]
        value, unit = _plain_value(r)
        rng = r.get("referenceRange")
        out.append({"analyte": name, "date": _obs_date(r), "value": value,
                    "unit": unit,
                    "reference_range_in_records": bool(
                        isinstance(rng, list) and rng),
                    "earlier_readings": len(readings) - 1})
    return out


#: For a trend sentence from the engine's KDIGO creatinine check (#867),
#: wherever the model meets one: get_labs' `trends` and the brief's
#: lab-trends section. The sentence already says what to do and stops short
#: of a diagnosis; a paraphrase is where a diagnosis would creep in.
_TREND_NOTE = (
    "Each trend line compares the person's own results over time. Report it "
    "as written, in its own sentence: do not paraphrase it into a diagnosis "
    "or name a condition it does not name. When it says to contact the "
    "doctor promptly, tell the person to contact their doctor promptly.")


#: How a lab result, or a change in one, is told. The system prompt's own
#: rule (personas.NO_TREND_VERDICT), repeated in every lab tool's note: the
#: text timeline note was a hand-written copy that never got the sentence,
#: and a follow-up answered from get_labs never saw the timeline's (#942).
_TREND_NO_VERDICT = NO_TREND_VERDICT

#: What the model may offer to look at next. It offered HDL and
#: triglycerides after an LDL answer on records that hold neither (#942).
_ONLY_TESTS_ON_FILE = (
    "If you offer to look at another test, offer only one named in "
    "other_tests_on_file. Never suggest a test that is not listed there: "
    "these records may not have it.")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _date_in_words(iso: str) -> str:
    """"April 21, 2025" for a real YYYY-MM-DD, so the model never reads a
    person "2025-04-21" (#942); anything else unchanged. A fixed month list,
    not strftime, which follows the process locale."""
    try:
        d = datetime.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _timeline_in_words(series: dict) -> dict:
    """What a text needs instead of a chart: the first and latest reading
    and the direction, worked out here so the model never computes one.
    A single reading has no direction, so it gets neither."""
    out = {"name": series["name"], "readings": len(series["readings"]),
           "trend_plottable": series["trend_plottable"]}
    if series.get("withheld"):
        # Readings exist but their unit could not be confirmed, so no
        # numbers: say so rather than imply there are none.
        out["readings_unit_unconfirmed"] = series["withheld"]
    if not series["trend_plottable"]:
        return out
    dated = [r for r in series["readings"] if r["date"]]
    first, latest = dated[0], dated[-1]
    first_unit = _coded_unit(first, series)
    latest_unit = _coded_unit(latest, series)
    # A unit we do not recognise, or two different units: no numbers and
    # no direction. "Lower" from 120 mg/dL to 6.5 mmol/L is not a finding
    # (#884 QA F1).
    if first_unit is None or latest_unit is None or first_unit != latest_unit:
        return out
    out["first"] = {"date": _date_in_words(first["date"]),
                    "value": first["value"], "unit": first_unit}
    out["latest"] = {"date": _date_in_words(latest["date"]),
                     "value": latest["value"], "unit": latest_unit}
    out["direction"] = ("higher" if latest["value"] > first["value"]
                        else "lower" if latest["value"] < first["value"]
                        else "unchanged")
    return out


def records_arriving(hc: HealthClawClient, tenant: str,
                     connection: dict) -> bool:
    """Whether this connection's records may still be arriving.

    The stored status cannot say: the chat page settles a connection to
    `active` as soon as anything lands, so a first answer mid-import was
    never told the rest was on its way (QA945-F1). The engine's Fasten job
    list can (#940). A look that fails answers True: "may still be
    loading" is true either way, and silence read as "that is all".
    Made-up records never arrive, so they cost no look.
    """
    if connection.get("kind") == "sample":
        return False
    try:
        state = hc.fasten_import_state(tenant)
    except HealthClawError:
        return True
    # "none" with a pending connection: connected, and the job not yet made.
    return state == "running" or (
        state == "none" and connection.get("status") == "pending")


def _empty_summary_note(hc: HealthClawClient, tenant: str,
                        arriving: bool = False) -> str:
    """Why four empty lists are empty, for the model.

    Empty from "nothing here" and empty from "nothing here yet" are the same
    four lists, and a model handed them told a person with records that none
    were connected. The count is the one the hub uses (totals only, no
    resources) and is taken only when every list came back empty. A count
    that fails says nothing either way, as on the chat page
    (careagents/intake_state.py): unknown is never zero.
    """
    try:
        total = hc.record_count(tenant)
    except HealthClawError:
        return ("Nothing came back, and we could not check whether records "
                "are on file. Never say the person has no records or that "
                "nothing is connected: say you could not read their records "
                "just now and offer to try again.")
    if total > 0 and arriving:
        return ("No conditions, medications or allergies came back yet, but "
                "some records are on file for this person, and more are "
                "still loading. Say plainly that their records are still "
                "loading and they can ask again shortly. Never say they "
                "have no records, no conditions, medications or allergies, "
                "or that nothing is connected. Offer to look at what has "
                "arrived.")
    if total > 0:
        return ("No conditions, medications or allergies came back, but "
                "some records are on file for this person. Never say they "
                "have no records or that nothing is connected. Say these "
                "lists are empty in what you can read, and offer to look at "
                "their other records.")
    return ("No records have arrived here yet. Say plainly that their records "
            "are not here yet: if they connected them recently, they are "
            "still loading, which can take up to 45 minutes, and they can "
            "ask again shortly. Never say they have no conditions, "
            "medications or allergies.")


def _propose_text(hc: HealthClawClient, tenant: str, args: object,
                  events: list, agent_id: str, origin: str,
                  sms_phone: Callable[[], str | None] | None) -> str:
    """propose_action. Every refusal stages nothing and says so; the number
    is never in what the model reads back."""
    def refuse(note):
        return json.dumps({"error": "not_proposed", "note": note})
    # Asked now, not when the turn started: the flag, a pause or the
    # number may have changed, and a model may call a tool it was not
    # offered at all.
    phone = sms_phone() if sms_phone else None
    if not phone:
        return refuse("Texting is not available for this person. Nothing "
                      "was sent or staged. If they want texts, they can "
                      "link their phone in CareAgents.")
    extra = sorted(set(args) - {"purpose"}) if isinstance(args, dict) else []
    if not isinstance(args, dict) or extra:
        # The count only: a name is as model-written as a value, and
        # either could carry what the model was fed.
        logger.warning("propose_action refused: %d unexpected argument(s)",
                       len(extra))
        return refuse("propose_action takes only a purpose. Nothing was "
                      "staged.")
    purpose = args.get("purpose")
    if purpose not in text_self.PURPOSES:
        return refuse("purpose must be one of: "
                      + ", ".join(text_self.PURPOSES) + ". Nothing was staged.")
    if any(a.get("kind") == "sms" for a in hc.pending_actions(tenant)):
        return refuse("A text is already waiting for the person's approval. "
                      "Nothing new was staged; point them to the one that "
                      "is waiting.")
    action_id = hc.propose_sms(tenant, phone,
                               text_self.body_for(purpose, origin))
    events.append({"type": "card", "kind": "review",
                   "action_id": action_id, "label": "Text message",
                   "review_url": f"/review/{agent_id}/{action_id}"})
    return json.dumps({"action_id": action_id,
                       "status": "awaiting_confirmation",
                       "note": text_self.APPROVAL_NOTE + (
                           " Nothing is sent until the person approves it "
                           "there. Do not propose another.")})


def _execute_tool(hc: HealthClawClient, tenant: str, name: str,
                  args: dict, events: list, agent_id: str = "",
                  surface: str = "", origin: str = "",
                  arriving: bool = False,
                  sms_phone: Callable[[], str | None] | None = None) -> str:
    on_text = surface in TEXT_SURFACES
    origin = (origin or "").rstrip("/")
    if name == "propose_action":
        return _propose_text(hc, tenant, args, events, agent_id, origin,
                             sms_phone)
    if name == "get_health_summary":
        parts = {}
        med_resolver = _medication_resolver(hc, tenant)
        failed = False
        # Statements get their own key and are never folded into
        # "medications": a merged list needs a dedup rule that code cannot
        # supply (#377, docs/2026-08-05-medicationstatement-support-decision.md).
        for rt, key in (("Condition", "conditions"),
                        ("MedicationRequest", "medications"),
                        ("MedicationStatement", "medication_statements"),
                        ("AllergyIntolerance", "allergies")):
            # One failed search used to raise out of the whole tool, and the
            # three that worked went with it.
            try:
                parts[key] = _summarize_bundle(
                    hc.search(tenant, rt), limit=SUMMARY_LIMIT,
                    resolve_ref=(med_resolver if rt in _MEDICATION_TYPES
                                 else None))
            except HealthClawError:
                parts[key] = {"could_not_read": True}
                failed = True
        if failed:
            parts["note"] = (
                "A list marked could_not_read could not be read just now, on "
                "our side. It is not absent: never say the person has none "
                "of that kind, or that nothing is connected. Give what the "
                "other lists show, say plainly which part you could not read "
                "this time, and offer to try again.")
        elif not any(parts.values()):
            parts["note"] = _empty_summary_note(hc, tenant, arriving)
        return json.dumps(parts)
    if name == "get_labs":
        labs = hc.interpret_labs(tenant)
        consumer = labs["consumer"] if isinstance(labs["consumer"], dict) else {}
        # Trend sentences (the KDIGO creatinine check, #867) are about how a
        # result moved, not where one reading sits against a range, so they
        # travel in their own key and never among the range lines. The
        # engine's consumer sentence only: the raw check in `summary` carries
        # ids, ratios and baselines the model has no use for.
        trends = [t["message"] for t in consumer.get("trends") or ()
                  if isinstance(t, dict) and isinstance(t.get("message"), str)]
        consumer = {k: v for k, v in consumer.items() if k != "trends"}
        out = {"consumer_summary": consumer,
               "disclaimer": labs["disclaimer"][:200]}
        latest = _latest_per_analyte(consumer, labs.get("bundle") or {})
        if latest is not None:
            # One undated line per reading read an old high as current and
            # buried the newest result (#875 QA). Every surface gets this.
            out["consumer_summary"] = {
                **{k: v for k, v in consumer.items() if k != "lines"},
                "latest": latest}
            out["note"] = (
                "Each analyte shows its LATEST reading, with its date; "
                "earlier readings are counted, not listed. Describe the "
                "latest as the person's current result, and an earlier high "
                "or low only as history.")
        not_evaluated = (_not_evaluated(consumer, labs.get("bundle") or {})
                         if consumer.get("unevaluated") else None)
        if not_evaluated:
            # The reading, plainly: its value, its date, and that there is
            # nothing in the records to compare it to (#937). Never a range or
            # a verdict of the model's own; "never inferred" holds here too.
            out["not_evaluated"] = not_evaluated
            # The engine's note gives the check's reason, and for a blood
            # pressure that reason is "carried no number" beside a reading
            # of 138/88: the readings above say it better.
            # Counted as the rows are, one per test, not per reading.
            pair = dict(zip(consumer["unevaluated_analytes"],
                            consumer["unevaluated_codes"]))
            out["consumer_summary"] = {
                **{k: v for k, v in out["consumer_summary"].items()
                   if k != "unevaluated_note"},
                "unevaluated_count": len(not_evaluated),
                "unevaluated_analytes": [r["analyte"] for r in not_evaluated],
                "unevaluated_codes": [pair[r["analyte"]]
                                      for r in not_evaluated]}
            out["note"] =((out["note"] + " ") if out.get("note") else "") + (
                "This lab answer is INCOMPLETE: "
                f"{len(not_evaluated)} test(s) in not_evaluated were not "
                "scored against a reference range. Zero flagged results here "
                "means nothing was SCORED as abnormal, not that nothing is "
                "abnormal. Each not_evaluated reading is that test's latest; "
                "earlier_readings are counted, not listed, so give no "
                "direction from them. Report the "
                + ("latest readings" if latest is not None else "lines")
                + " you were given AND, for each not_evaluated reading, give "
                "its value and date in plain words and say there is no "
                "reference range in the records to compare it to (where "
                "reference_range_in_records is true, say instead that the "
                "check could not compare it to the range given). A reading "
                "with value null is on file without a number you can give: "
                "say so. Do not say it 'could not be interpreted'. Do not "
                "call it normal, fine, high, low, or within range, and do not "
                "supply a range or target of your own: the person's clinician "
                "can say what it means for them.")
        elif consumer.get("unevaluated"):
            # The marker arriving is necessary and not sufficient. Care gaps
            # learned that a model handed lines plus an unevaluated note leads
            # with the lines (#417); handed "high: 0, critical: 0" plus a note,
            # it leads with the zeros, which is how four stage 2 readings were
            # summarised as nothing flagged (#689). Say what the zeros mean.
            out["note"] = ((out["note"] + " ") if out.get("note") else "") + (
                "This lab answer is INCOMPLETE: "
                f"{consumer.get('unevaluated_count')} result(s) were not "
                "evaluated. Zero flagged results here means nothing was "
                "SCORED as abnormal, not that nothing is abnormal. Report the "
                + ("latest readings" if latest is not None else "lines")
                + " you were given AND say, using unevaluated_note, which "
                "results were not evaluated and why. Do not describe an "
                "unevaluated result as normal, fine, or within range.")
        if trends:
            out["trends"] = trends
            out["note"] = ((out["note"] + " ") if out.get("note") else "") + (
                _TREND_NOTE + (
                    " In a text, keep the trend sentence whole even when you "
                    "shorten everything else." if on_text else ""))
        # A follow-up ("So is that good news?") is answered from here, not
        # from the timeline's note (QA on #953). Its own key, not `note`: a
        # fully scored answer carries no note, so the one that matters is
        # not read past as boilerplate.
        out["no_verdict_rule"] = _TREND_NO_VERDICT
        return json.dumps(out)
    if name == "show_lab_timeline":
        topic = str(args.get("topic") or "")
        labs = hc.interpret_labs(tenant)
        keys = labs_timeline.keys_for_topic(topic)
        series = labs_timeline.build_series(labs.get("bundle") or {}, keys,
                                            keep_withheld=True)
        # Only a series with readings can be drawn; one kept for its
        # withheld count alone has no chart to link to.
        if any(s["readings"] for s in series):
            events.append({"type": "card", "kind": "lab-timeline",
                           "topic": topic})
        # The other charted tests these records hold, by the chart's own
        # names: what the model may offer next (_ONLY_TESTS_ON_FILE).
        shown = {s["name"] for s in series}
        others = [s["name"] for s in labs_timeline.build_series(
                      labs.get("bundle") or {}, None, keep_withheld=True)
                  if s["name"] not in shown]
        if on_text:
            # A text thread cannot show the chart, so the words have to
            # carry it: first, latest and direction per plottable series.
            # The card still goes out; run_reply turns it into a link.
            return json.dumps({
                "chart_shown": False,
                "series": [_timeline_in_words(s) for s in series],
                "other_tests_on_file": others,
                "note": ("The chart cannot be shown in a text message. "
                         "Describe the trend in words in one or two "
                         "sentences, using first, latest and direction; a "
                         "link to the chart is included below your answer. A "
                         "series with trend_plottable false has a single "
                         "reading — say so, and never describe it as rising "
                         "or falling. A plottable series with no direction "
                         "has readings whose units differ or could not be "
                         "confirmed: give no numbers and no direction for "
                         "it. A series with readings_unit_unconfirmed "
                         "has that many readings whose unit could not be "
                         "confirmed: say they are on file but give no "
                         "numbers for them. " + _TREND_NO_VERDICT + " "
                         + _ONLY_TESTS_ON_FILE
                         if series else
                         "No lab series matched in the CONNECTED records. "
                         "That is not the same as the person never having "
                         "had this test — say so, and do not report it as "
                         "absent."),
            })
        # The chart carries every reading; the model gets the same first,
        # latest and direction a text gets, worked out here. Given only the
        # chart's shape, it answered "How has my A1c changed?" with "you can
        # see the direction in the chart", every time (#937). Still not every
        # value, and still no direction from a single reading or across units
        # that differ or could not be confirmed (_timeline_in_words).
        return json.dumps({
            "chart_shown": any(s["readings"] for s in series),
            "series": [_timeline_in_words(s) for s in series],
            "other_tests_on_file": others,
            "note": ("A chart is now visible to the person. Answer the "
                     "question in plain words in one or two sentences: for "
                     "each series with a direction, say it went from its "
                     "first reading to its latest, with dates, and whether "
                     "that is higher, lower or unchanged; then invite a "
                     "question. Do not list every value. A series with "
                     "trend_plottable "
                     "false has a single reading — say so, and never describe "
                     "it as rising or falling. A plottable series with no "
                     "direction has readings whose units differ or could not "
                     "be confirmed: give no numbers and no direction for it. "
                     "A series with readings_unit_unconfirmed has readings on "
                     "file whose unit could not be confirmed, so they are not "
                     "charted: say they exist, give no numbers. "
                     + _TREND_NO_VERDICT + " " + _ONLY_TESTS_ON_FILE
                     if series else
                     "No lab series matched in the CONNECTED records. That is "
                     "not the same as the person never having had this test — "
                     "say so, and do not report it as absent."),
        })
    if name == "get_care_gaps":
        gaps = hc.care_gaps(tenant)
        consumer = gaps.get("consumer") or {}
        out = {"consumer_summary": consumer}
        if consumer.get("unevaluated") and consumer.get("lines"):
            # Some rules were decided and some were not (#417). The
            # could-not-run note below would suppress the real due screenings
            # in `lines`; no note at all would sell four screenings as the
            # whole answer. Neither, so the partial case says it is partial.
            out["note"] = (
                "This preventive-care answer is PARTIAL: "
                f"{consumer.get('unevaluated_count')} screening(s) could not "
                f"be checked — reason: {consumer['unevaluated']}. Report the "
                "lines you were given AND say, using unevaluated_note, which "
                "screenings were not checked and why. Do not describe an "
                "unchecked screening as up to date or as not due.")
        elif consumer.get("unevaluated"):
            # The check produced no lines because it could not run, not
            # because nothing is outstanding. Those two arrive here looking
            # identical, and the second was being read out to people as the
            # first on the busiest button in the product (#389).
            out["note"] = (
                "The preventive-care check did not reach a verdict — reason: "
                f"{consumer['unevaluated']}. Say that plainly, using "
                "unevaluated_note. Do NOT tell the person they have no "
                "screenings due, and do not name or infer any screening from "
                "this result.")
        return json.dumps(out)
    if name == "search_records":
        rt = args.get("resource_type") or "Condition"
        return json.dumps(_summarize_bundle(
            hc.search(tenant, rt), limit=SUMMARY_LIMIT,
            resolve_ref=(_medication_resolver(hc, tenant)
                         if rt in _MEDICATION_TYPES else None)))
    if name == "start_intake_form":
        action_id = hc.start_form_action(tenant)
        events.append({"type": "card", "kind": "review",
                       "action_id": action_id,
                       # The review route is per agent; a bare action id
                       # named no page at all.
                       "review_url": f"/review/{agent_id}/{action_id}"})
        return json.dumps({
            "action_id": action_id, "status": "awaiting_confirmation",
            "note": ("Proposed. The person gets a link to review and "
                     "approve each item; nothing is generated until they "
                     "do. Say a link is included below."
                     if on_text else
                     "Proposed. A Review & approve card is now visible to "
                     "the person; nothing is generated until they approve "
                     "each item themselves.")})
    if name == "appointment_brief":
        raw = hc.fetch_appointment_brief(tenant)
        if raw is None:
            # The engine answered and has no brief. That says nothing about
            # whether the person has a visit, or any history.
            return json.dumps({
                "brief": None,
                "note": ("No visit brief could be built from the connected "
                         "records. That does not mean the person has no "
                         "visit or no history — say the brief is not "
                         "available here and do not report anything as "
                         "absent.")})
        sections = {}
        notes = []
        for section, fields in brief_mod.parse_sections(raw).items():
            sections[section] = [
                {"label": f.get("label"), "value": f.get("value")}
                for f in fields[:MAX_BRIEF_FIELDS] if isinstance(f, dict)]
            if len(fields) > MAX_BRIEF_FIELDS:
                # No counts, as in _summarize_bundle's marker: a model
                # handed "6 of 9" reads it to the person (#939).
                notes.append(
                    f"The {section.replace('-', ' ')} list is not complete: "
                    "more items are on file. Do not describe it as "
                    "complete, do not count it, and do not say how many "
                    "you were shown. Say in plain words that there are "
                    "more and that the full list is on their Visit brief "
                    "page. Do not offer to show the rest here: you cannot.")
        if brief_mod.care_gaps_marker(raw, "status") != brief_mod.CARE_GAPS_OK:
            notes.append("The screening review did not complete. Do not say "
                         "no screenings are due.")
        if sections.get("lab-trends"):
            notes.append(_TREND_NOTE)
        # The brief carries labs and vitals whatever its sections (#942).
        notes.append(_TREND_NO_VERDICT)
        events.append({"type": "card", "kind": "brief"})
        notes.append("Summarize in a few short lines; a link to the full "
                     "brief is included below your answer."
                     if on_text else
                     "Summarize briefly; the full brief is on the person's "
                     "Visit brief page.")
        return json.dumps({"sections": sections, "note": " ".join(notes)})
    if name == "check_form_status":
        action_id = str(args.get("action_id") or "")
        status = hc.action_status(tenant, action_id)
        outcome = {}
        try:
            outcome = json.loads(status.get("outcome_summary") or "{}")
        except ValueError:
            pass
        link = outcome.get("delivery_link")
        if status.get("status") == "completed" and link:
            events.append({"type": "card", "kind": "pdf", "url": link,
                           "action_id": action_id})
        if on_text:
            # The signed delivery link is a bearer URL to the document. A
            # model that echoes it would text it, so a text surface gets the
            # review page, which has the PDF button (#875 QA).
            out = {"status": status.get("status")}
            if status.get("status") == "completed" and origin and agent_id:
                out["form_link"] = (f"{origin}/review/{agent_id}/"
                                    f"{quote(action_id, safe='')}")
            return json.dumps(out)
        return json.dumps({"status": status.get("status"),
                           "delivery_link": link})
    return json.dumps({"error": f"unknown tool {name}"})


def run_turn(cfg, hc: HealthClawClient, tenant: str, system: str,
             history: list[dict], user_text: str, *, agent_id: str = "",
             sms_phone: Callable[[], str | None] | None = None):
    """Generator of UI events for one user message. Mutates `history`.
    `sms_phone` answers the number propose_action may text, or None; it is
    asked once here for the tool list and again if the tool is called."""
    _trim_history(history)
    history.append({"role": "user", "content": user_text})
    tools = tools_for_turn(sms_phone() if sms_phone else None)
    rounds = 0
    while True:
        try:
            turn = llm.complete(cfg, system, history, tools)
        except llm.LLMError as exc:
            yield {"type": "error", "text": failure_text(exc)}
            return

        if not turn.tool_calls:
            history.append({"role": "assistant", "content": turn.text})
            yield {"type": "text", "text": turn.text}
            return

        rounds += 1
        history.append({"role": "assistant", "content": turn.text,
                        "tool_calls": [{"id": c.id, "name": c.name,
                                        "arguments": c.arguments}
                                       for c in turn.tool_calls],
                        # Preserve provider-native call objects for replay
                        # (Gemini thought_signature); ignored by Anthropic.
                        "_openai_tool_calls": turn.raw_tool_calls,
                        # Caps the advisor per turn (careagents/routing.py).
                        "advisor_calls": getattr(turn, "advisor_calls", 0)})
        for call in turn.tool_calls:
            yield {"type": "tool", "name": call.name,
                   "label": TOOL_LABELS.get(call.name, call.name)}
            side_events: list[dict] = []
            try:
                result = _execute_tool(hc, tenant, call.name,
                                       call.arguments, side_events,
                                       agent_id=agent_id,
                                       sms_phone=sms_phone)
            except HealthClawError as exc:
                result = tool_error_result(exc)
            history.append({"role": "tool", "tool_call_id": call.id,
                            "content": result})
            for ev in side_events:
                yield ev

        if rounds >= MAX_TOOL_ROUNDS:
            # Budget spent. This used to append the nudge and keep looping, so
            # a model that kept calling tools was never actually stopped — it
            # just collected another nudge each round and spent indefinitely.
            # Ask once more with NO tools offered, so the only thing it can do
            # is answer, then return regardless of what comes back.
            history.append({"role": "user", "content": (
                "(system: tool budget reached — answer now with what you "
                "have)"),
                # Not the person's message: the turn (routing tier, advisor
                # cap) still starts at theirs (careagents/routing.py).
                "loop_note": True})
            try:
                final = llm.complete(cfg, system, history, [])
            except llm.LLMError as exc:
                yield {"type": "error", "text": failure_text(exc)}
                return
            history.append({"role": "assistant", "content": final.text})
            yield {"type": "text", "text": final.text}
            return


def run_turn_to_message(cfg, hc: HealthClawClient, tenant: str, system: str,
                        history: list[dict], user_text: str,
                        *, origin: str = "", agent_id: str = "") -> str:
    """Run one turn and collapse the streamed UI events into a single plain
    reply, for non-streaming surfaces (SMS / iMessage).

    Review and PDF cards become links back to the web app — the human approval
    gate always lives there, never inline in the message thread.
    """
    parts: list[str] = []
    extras: list[str] = []
    base = (origin or "").rstrip("/")
    for ev in run_turn(cfg, hc, tenant, system, history, user_text,
                       agent_id=agent_id):
        kind = ev.get("type")
        if kind == "text" and ev.get("text"):
            parts.append(ev["text"])
        elif kind == "error":
            return ev.get("text") or GENERIC_FAILURE_TEXT
        elif kind == "card" and ev.get("kind") == "review" and ev.get("label"):
            aid = ev.get("action_id", "")
            link = (f"{base}/review/{agent_id}/{aid}"
                    if base and agent_id and aid else "")
            extras.append(
                f"{ev['label']} waiting for your approval. Only the review "
                "page approves it"
                + (f": {link}" if link else ", in the CareAgents app."))
        elif kind == "card" and ev.get("kind") == "review":
            aid = ev.get("action_id", "")
            link = (f"{base}/review/{agent_id}/{aid}"
                    if base and agent_id and aid else "")
            extras.append(
                "I've prepared a form for your review — approve each item "
                + (f"here: {link}" if link else "in the CareAgents app."))
        elif kind == "card" and ev.get("kind") == "pdf" and ev.get("url"):
            extras.append(f"Your signed document is ready: {ev['url']}")
    reply = "\n\n".join([*parts, *extras]).strip()
    return reply or "…"
