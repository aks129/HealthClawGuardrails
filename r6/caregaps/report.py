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


def build_consumer_summary(results):
    lines = []
    for r in results:
        if r.get("status") in ("due", "up_to_date"):
            line = _consumer_line(r)
            if line:
                lines.append(line)
    return {"lines": lines, "note": _CONSUMER_NOTE}
