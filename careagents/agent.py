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

from careagents import llm
from careagents.healthclaw import HealthClawClient, HealthClawError

MAX_TOOL_ROUNDS = 6

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
    "get_care_gaps": "Checking preventive care gaps",
    "search_records": "Searching your records",
    "start_intake_form": "Preparing your intake form",
    "check_form_status": "Checking your form",
}


def _summarize_bundle(bundle: dict, limit: int = 12) -> list[dict]:
    """Compact, model-friendly view of a searchset bundle (already redacted)."""
    out = []
    for entry in (bundle.get("entry") or [])[:limit]:
        res = entry.get("resource") or {}
        rt = res.get("resourceType")
        if rt == "OperationOutcome":
            continue
        item = {"type": rt}
        code = res.get("code") or res.get("medicationCodeableConcept") or {}
        text = code.get("text") or " ".join(
            c.get("display", "") for c in (code.get("coding") or [])[:1])
        if text:
            item["name"] = text.strip()
        if res.get("status"):
            item["status"] = res["status"]
        vq = res.get("valueQuantity")
        if isinstance(vq, dict) and vq.get("value") is not None:
            item["value"] = f"{vq.get('value')} {vq.get('unit', '')}".strip()
        if res.get("effectiveDateTime"):
            item["date"] = str(res["effectiveDateTime"])[:10]
        out.append(item)
    return out


def _execute_tool(hc: HealthClawClient, tenant: str, name: str,
                  args: dict, events: list) -> str:
    if name == "get_health_summary":
        parts = {}
        for rt, key in (("Condition", "conditions"),
                        ("MedicationRequest", "medications"),
                        ("AllergyIntolerance", "allergies")):
            parts[key] = _summarize_bundle(hc.search(tenant, rt))
        return json.dumps(parts)
    if name == "get_labs":
        labs = hc.interpret_labs(tenant)
        return json.dumps({"consumer_summary": labs["consumer"],
                           "disclaimer": labs["disclaimer"][:200]})
    if name == "get_care_gaps":
        gaps = hc.care_gaps(tenant)
        return json.dumps({"consumer_summary": gaps["consumer"]})
    if name == "search_records":
        rt = args.get("resource_type") or "Condition"
        return json.dumps(_summarize_bundle(hc.search(tenant, rt)))
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
    history.append({"role": "user", "content": user_text})
    rounds = 0
    while True:
        try:
            turn = llm.complete(cfg, system, history, TOOLS)
        except llm.LLMError as exc:
            yield {"type": "error", "text": str(exc)}
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
            history.append({"role": "user", "content": (
                "(system: tool budget reached — answer now with what you "
                "have)")})


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
            return ev.get("text") or "Something went wrong on our side."
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
