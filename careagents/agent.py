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

from careagents import labs_timeline, llm
from careagents.healthclaw import HealthClawClient, HealthClawError

MAX_TOOL_ROUNDS = 6

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
GENERIC_FAILURE_TEXT = "Something went wrong on our side."
RATE_LIMITED_TEXT = (
    "I'm getting more requests than I can answer right now. Nothing is wrong "
    "with your records — try asking again in a moment.")


def failure_text(exc: Exception) -> str:
    """What the person reads when a turn fails.

    A rate limit is not a defect, and saying "something went wrong on our
    side" for one is two failures at once: it invites a bug report for a bug
    that does not exist, and it leaves a patient wondering whether their
    health records are broken. Everything else stays generic on purpose —
    exception internals are never patient-facing text.
    """
    if isinstance(exc, llm.LLMRateLimited):
        return RATE_LIMITED_TEXT
    return GENERIC_FAILURE_TEXT


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
     "description": "Search the person's FHIR records by type.",
     "parameters": {"type": "object", "properties": {
         "resource_type": {"type": "string", "enum": [
             "Condition", "Observation", "MedicationRequest",
             "AllergyIntolerance", "Immunization", "Procedure"]},
     }, "required": ["resource_type"]}},
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
    "get_health_summary": "Reading your records — redacted view",
    "get_labs": "Interpreting your labs",
    "show_lab_timeline": "Charting your results over time",
    "get_care_gaps": "Checking preventive care gaps",
    "search_records": "Searching your records",
    "start_intake_form": "Preparing your intake form",
    "check_form_status": "Checking your form",
}


# How many DISTINCT Medication references one tool call may chase. Each is a
# separately audited read; a pathological bundle must not turn one chat
# message into an unbounded fan-out.
MAX_MEDICATION_DEREFS = 10


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
            raw = next((c.get("code") for c in (code.get("coding") or [])
                        if isinstance(c, dict) and c.get("code")), None)
            if raw:
                item["name"] = f"unlabeled record, code {raw}"
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
        if res.get("status"):
            item["status"] = res["status"]
        vq = res.get("valueQuantity")
        if isinstance(vq, dict) and vq.get("value") is not None:
            item["value"] = f"{vq.get('value')} {vq.get('unit', '')}".strip()
        if res.get("effectiveDateTime"):
            item["date"] = str(res["effectiveDateTime"])[:10]
        out.append(item)

    if total > len(out):
        # Deliberately shaped so it cannot be mistaken for a record: no
        # "type", no "name". The instruction is carried in the payload rather
        # than left to the system prompt alone, because this is the one place
        # the model can tell complete from partial.
        out.append({
            "truncated": True,
            "shown": len(out),
            "total": total,
            "note": (f"Only {len(out)} of {total} records are shown. Do not "
                     f"describe this list as complete or as all the person "
                     f"has; say more exist and offer to narrow the search."),
        })
    return out


def _execute_tool(hc: HealthClawClient, tenant: str, name: str,
                  args: dict, events: list) -> str:
    if name == "get_health_summary":
        parts = {}
        med_resolver = _medication_resolver(hc, tenant)
        for rt, key in (("Condition", "conditions"),
                        ("MedicationRequest", "medications"),
                        ("AllergyIntolerance", "allergies")):
            parts[key] = _summarize_bundle(
                hc.search(tenant, rt),
                resolve_ref=med_resolver if rt == "MedicationRequest" else None)
        return json.dumps(parts)
    if name == "get_labs":
        labs = hc.interpret_labs(tenant)
        return json.dumps({"consumer_summary": labs["consumer"],
                           "disclaimer": labs["disclaimer"][:200]})
    if name == "show_lab_timeline":
        topic = str(args.get("topic") or "")
        labs = hc.interpret_labs(tenant)
        keys = labs_timeline.keys_for_topic(topic)
        series = labs_timeline.build_series(labs.get("bundle") or {}, keys)
        if series:
            events.append({"type": "card", "kind": "lab-timeline",
                           "topic": topic})
        # The model gets SHAPE, not the readings: how many series, how many
        # points, whether a trend is even plottable. The chart carries the
        # numbers. Handing them over too would invite the model to restate
        # every value and, worse, to narrate a direction from a single point.
        return json.dumps({
            "chart_shown": bool(series),
            "series": [{"name": s["name"], "readings": len(s["readings"]),
                        "trend_plottable": s["trend_plottable"]}
                       for s in series],
            "note": ("A chart is now visible to the person. Describe what it "
                     "shows in one or two sentences and invite a question; do "
                     "not list every value. A series with trend_plottable "
                     "false has a single reading — say so, and never describe "
                     "it as rising or falling."
                     if series else
                     "No lab series matched in the CONNECTED records. That is "
                     "not the same as the person never having had this test — "
                     "say so, and do not report it as absent."),
        })
    if name == "get_care_gaps":
        gaps = hc.care_gaps(tenant)
        consumer = gaps.get("consumer") or {}
        out = {"consumer_summary": consumer}
        if consumer.get("unevaluated"):
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
                         if rt == "MedicationRequest" else None)))
    if name == "start_intake_form":
        action_id = hc.start_form_action(tenant)
        events.append({"type": "card", "kind": "review",
                       "action_id": action_id,
                       "review_url": f"/review/{action_id}"})
        return json.dumps({
            "action_id": action_id, "status": "awaiting_confirmation",
            "note": ("Proposed. A Review & approve card is now visible to "
                     "the person; nothing is generated until they approve "
                     "each item themselves.")})
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
        return json.dumps({"status": status.get("status"),
                           "delivery_link": link})
    return json.dumps({"error": f"unknown tool {name}"})


def run_turn(cfg, hc: HealthClawClient, tenant: str, system: str,
             history: list[dict], user_text: str):
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
                        "_openai_tool_calls": turn.raw_tool_calls})
        for call in turn.tool_calls:
            yield {"type": "tool", "name": call.name,
                   "label": TOOL_LABELS.get(call.name, call.name)}
            side_events: list[dict] = []
            try:
                result = _execute_tool(hc, tenant, call.name,
                                       call.arguments, side_events)
            except HealthClawError as exc:
                result = json.dumps({"error": str(exc)})
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
                "have)")})
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
    for ev in run_turn(cfg, hc, tenant, system, history, user_text):
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
