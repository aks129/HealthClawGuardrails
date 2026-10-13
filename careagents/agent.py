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
from urllib.parse import quote

from careagents import brief as brief_mod
from careagents import labs_timeline, llm
from careagents.healthclaw import HealthClawClient, HealthClawError

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
# Out of credit or quota. Says nothing about providers or billing: that is
# the operator's problem, and careagents/llm.py logs it for them.
UNAVAILABLE_TEXT = (
    "The assistant is unavailable right now. Nothing is wrong with your "
    "records — please try again later.")


def failure_text(exc: Exception) -> str:
    """What the person reads when a turn fails.

    A rate limit is not a defect, and saying "something went wrong on our
    side" for one is two failures at once: it invites a bug report for a bug
    that does not exist, and it leaves a patient wondering whether their
    health records are broken. Everything else stays generic on purpose —
    exception internals are never patient-facing text.
    """
    if isinstance(exc, llm.LLMOutOfCredit):
        return UNAVAILABLE_TEXT
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
                     "LDL, HDL, triglycerides, A1c, glucose). The chart "
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

TOOL_LABELS = {
    "get_health_summary": "Reading your records",
    "get_labs": "Interpreting your labs",
    "show_lab_timeline": "Charting your results over time",
    "get_care_gaps": "Checking preventive care gaps",
    "search_records": "Searching your records",
    "appointment_brief": "Preparing your visit brief",
    "start_intake_form": "Preparing your intake form",
    "check_form_status": "Checking your form",
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

#: Codes an AllergyIntolerance carries when it records that allergy
#: information is missing, not an allergy. Keyed by system and code, never
#: by label. "No known allergy" codes are deliberately absent: what to say
#: for one is a separate question, and it is never "no known allergies"
#: inferred here.
_ALLERGY_STATUS_CODES = frozenset({
    ("http://snomed.info/sct", "1631000175102"),  # Patient not asked
    *(("http://terminology.hl7.org/CodeSystem/v3-NullFlavor", c)
      for c in ("NI", "UNK", "ASKU", "NAV", "NASK")),
    *(("http://terminology.hl7.org/CodeSystem/data-absent-reason", c)
      for c in ("unknown", "asked-unknown", "temp-unknown", "not-asked",
                "asked-declined")),
})


def _is_allergy_status(res: dict) -> bool:
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    # Strings only: a list in `code` is unhashable, and ingest stores
    # whatever arrived (#885).
    keys = [(c["system"], c["code"])
            for c in (codings if isinstance(codings, list) else ())
            if isinstance(c, dict) and isinstance(c.get("system"), str)
            and isinstance(c.get("code"), str)]
    # EVERY coding, not any: penicillin coded beside a null flavor is a
    # penicillin allergy, and one status coding hid it (#945 security F1).
    return bool(keys) and all(k in _ALLERGY_STATUS_CODES for k in keys)


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
    for entry in entries[:limit]:
        res = entry.get("resource") or {}
        rt = res.get("resourceType")
        if rt == "OperationOutcome":
            continue
        item = {"type": rt}
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
        code = res.get("code") or res.get("medicationCodeableConcept") or {}
        text = code.get("text") or " ".join(
            c.get("display", "") for c in (code.get("coding") or [])[:1])
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
        out.append(item)

    if any(i.get("type") == "AllergyIntolerance"
           and not i.get("not_an_allergy") for i in out):
        # A recorded allergy is in the same list, so "your records don't
        # say whether you have allergies" would be false (#945 security F2).
        # Reworded rather than dropped: a dropped item would read as a
        # truncated list below.
        for i in out:
            if i.get("not_an_allergy"):
                i.pop("statement", None)
                i["note"] = ("This entry is not an allergy: it records that "
                             "allergy information was not collected at one "
                             "point. Report the allergies listed here; do "
                             "not mention this entry or quote it.")
    # The bundle's total, when the engine gives one: the entries are one page
    # (50 by default), so counting them calls a longer list complete.
    stated = bundle.get("total")
    if isinstance(stated, int) and not isinstance(stated, bool):
        total = max(total, stated)
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
            "note": ("This is not the whole list: more records of this kind "
                     "are on file. Do not describe it as complete, do not "
                     "count it, and do not say how many you were shown or "
                     "that you could only see part. Answer from what is "
                     "here, then say in plain words that there are more and "
                     "that they can ask you to narrow it down, for example "
                     "to one test or one condition."),
        })
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
    for c in ((resource.get("code") or {}).get("coding") or []):
        if isinstance(c, dict) and c.get("system") == _LOINC_SYSTEM:
            return c.get("code")
    return None


def _obs_flag(resource: dict) -> str | None:
    for i in resource.get("interpretation") or []:
        for c in (i or {}).get("coding") or []:
            if isinstance(c, dict) and c.get("system") == _INTERPRETATION:
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
    for c in resource.get("component") or []:
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
    out = []
    for name, r in zip(names, unscored):
        value, unit = _plain_value(r)
        rng = r.get("referenceRange")
        out.append({"analyte": name, "date": _obs_date(r), "value": value,
                    "unit": unit,
                    "reference_range_in_records": bool(
                        isinstance(rng, list) and rng)})
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
    out["first"] = {"date": first["date"], "value": first["value"],
                    "unit": first_unit}
    out["latest"] = {"date": latest["date"], "value": latest["value"],
                     "unit": latest_unit}
    out["direction"] = ("higher" if latest["value"] > first["value"]
                        else "lower" if latest["value"] < first["value"]
                        else "unchanged")
    return out


def _empty_summary_note(hc: HealthClawClient, tenant: str) -> str:
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
    if total > 0:
        return ("No conditions, medications or allergies came back, but "
                "records are on file for this person, such as lab results. "
                "Never say they have no records or that nothing is "
                "connected. Say these lists are empty in what you can read, "
                "and offer to look at their lab results.")
    return ("No records have arrived here yet. Say plainly that their records "
            "are not here yet: if they connected them recently, they are "
            "still loading, which can take up to 45 minutes, and they can "
            "ask again shortly. Never say they have no conditions, "
            "medications or allergies.")


def _execute_tool(hc: HealthClawClient, tenant: str, name: str,
                  args: dict, events: list, agent_id: str = "",
                  surface: str = "", origin: str = "") -> str:
    on_text = surface in TEXT_SURFACES
    origin = (origin or "").rstrip("/")
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
                    hc.search(tenant, rt),
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
            parts["note"] = _empty_summary_note(hc, tenant)
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
            out["consumer_summary"] = {
                k: v for k, v in out["consumer_summary"].items()
                if k != "unevaluated_note"}
            out["note"] =((out["note"] + " ") if out.get("note") else "") + (
                "This lab answer is INCOMPLETE: "
                f"{len(not_evaluated)} reading(s) in not_evaluated were not "
                "scored against a reference range. Zero flagged results here "
                "means nothing was SCORED as abnormal, not that nothing is "
                "abnormal. Report the "
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
        if on_text:
            # A text thread cannot show the chart, so the words have to
            # carry it: first, latest and direction per plottable series.
            # The card still goes out; run_reply turns it into a link.
            return json.dumps({
                "chart_shown": False,
                "series": [_timeline_in_words(s) for s in series],
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
                         "numbers for them."
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
            "note": ("A chart is now visible to the person. Answer the "
                     "question in plain words in one or two sentences: for "
                     "each series with a direction, say it went from its "
                     "first reading to its latest, with dates, and whether "
                     "that is higher, lower or unchanged; then invite a "
                     "question. Do not list every value, and do not call the "
                     "change good or bad. A series with trend_plottable "
                     "false has a single reading — say so, and never describe "
                     "it as rising or falling. A plottable series with no "
                     "direction has readings whose units differ or could not "
                     "be confirmed: give no numbers and no direction for it. "
                     "A series with readings_unit_unconfirmed has readings on "
                     "file whose unit could not be confirmed, so they are not "
                     "charted: say they exist, give no numbers."
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
            hc.search(tenant, rt),
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
                    "more, that the full list is on their Visit brief "
                    "page, and that they can ask you to narrow it down.")
        if brief_mod.care_gaps_marker(raw, "status") != brief_mod.CARE_GAPS_OK:
            notes.append("The screening review did not complete. Do not say "
                         "no screenings are due.")
        if sections.get("lab-trends"):
            notes.append(_TREND_NOTE)
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
             history: list[dict], user_text: str, *, agent_id: str = ""):
    """Generator of UI events for one user message. Mutates `history`."""
    _trim_history(history)
    history.append({"role": "user", "content": user_text})
    rounds = 0
    while True:
        try:
            turn = llm.complete(cfg, system, history, TOOLS)
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
                                       agent_id=agent_id)
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
