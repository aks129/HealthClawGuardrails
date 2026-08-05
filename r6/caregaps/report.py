# r6/caregaps/report.py
"""Report builders for preventive-care gaps — pure (no Flask/DB).

build_caregaps_summary() is the clinician view (counts + the list of due
rules). build_consumer_summary() is the plain-language, outcomes-oriented
consumer view. Neither summary may be placed in audit detail (PHI).
"""

_CONSUMER_NOTE = (
    "These are general preventive-care reminders based on published "
    "guidelines — not personalized medical advice. Your connected "
    "records may be incomplete, so confirm anything here with your "
    "clinician.")

# Why the consumer list came back empty, in the person's own words. One entry
# per reason; the reason itself travels as `unevaluated` so a caller can
# branch on it without matching prose.
_UNEVALUATED_NOTES = {
    "no-patient": (
        "There is no patient record connected here yet, so this check had "
        "nothing to read. Nothing was examined, and that is not a finding "
        "that you have no screenings outstanding."),
    "ambiguous-patient": (
        "More than one patient record is connected here, so this check could "
        "not tell whose preventive care to look at. Nothing was examined, "
        "and that is not a finding that you have no screenings outstanding."),
    "demographics-unavailable": (
        "Your date of birth and sex were not available to this check, so it "
        "could not work out which screenings apply to you. That is not a "
        "finding that you have no screenings outstanding."),
}


def build_caregaps_summary(results):
    buckets = {"due": 0, "up_to_date": 0, "not_applicable": 0, "indeterminate": 0}
    gaps = []
    for r in results:
        status = r.get("status")
        if status in buckets:
            buckets[status] += 1
        if status == "due":
            gaps.append({"rule_id": r.get("rule_id"), "title": r.get("title"),
                        "note": r.get("note")})
    return {**buckets, "total": len(results), "gaps": gaps}


def _consumer_line(r):
    title, cadence, note = r.get("title"), r.get("cadence"), r.get("note")
    status = r.get("status")
    if status == "due":
        return {"rule_id": r.get("rule_id"), "title": title,
                "message": f"You may be due for {title.lower()} ({cadence}). {note}"}
    if status == "up_to_date":
        return {"rule_id": r.get("rule_id"), "title": title,
                "message": (f"Your {title.lower()} looks up to date "
                            f"(last on {r.get('last_done')}).")}
    return None


def _unevaluated_reason(results, unresolved):
    """Why there are no consumer lines, or None when the emptiness is a finding.

    An empty list is ambiguous by construction: "we looked and nothing is
    outstanding" and "we never got far enough to look" render identically,
    and the second was reaching patients as the first (#389, after #379 and
    #381). So the reason travels with the emptiness. Same posture as
    r6/labs/interpret.py `_indeterminate` — say what could not be decided
    rather than emitting a clean result.
    """
    if unresolved:
        return unresolved
    if any(r.get("status") == "indeterminate" for r in results):
        return "demographics-unavailable"
    return None


def build_consumer_summary(results, unresolved=None):
    """`unresolved` is the subject-resolution failure, if any — the caller
    knows about it and the engine cannot see it, since an unidentifiable
    patient produces exactly the same rule results as a healthy one."""
    lines = []
    for r in results:
        if r.get("status") in ("due", "up_to_date"):
            line = _consumer_line(r)
            if line:
                lines.append(line)
    out = {"lines": lines, "note": _CONSUMER_NOTE}
    if not lines:
        reason = _unevaluated_reason(results, unresolved)
        if reason:
            out["unevaluated"] = reason
            out["unevaluated_note"] = _UNEVALUATED_NOTES[reason]
    return out
