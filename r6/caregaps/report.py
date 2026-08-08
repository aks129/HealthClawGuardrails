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

# Why part or all of the consumer list is missing, in the person's own words.
# The reason itself travels as `unevaluated` so a caller can branch on it
# without matching prose.
#
# CALLER reasons — the caller knows the rules never got a record to read, and
# the engine cannot see that. Nothing here describes the person's data,
# because nothing of their data was looked at. Fixed prose: there is no
# finding to report.
_NOT_EVALUATED_NOTES = {
    "no-patient": (
        "There is no patient record connected here yet, so this check had "
        "nothing to read. Nothing was examined, and that is not a finding "
        "that you have no screenings outstanding."),
    "ambiguous-patient": (
        "More than one patient record is connected here, so this check could "
        "not tell whose preventive care to look at. Nothing was examined, "
        "and that is not a finding that you have no screenings outstanding."),
    "check-incomplete": (
        "This check could not be completed for your record right now, so "
        "nothing here was decided either way. That is a limit on the check "
        "and not something found in your record, and it is not a finding "
        "that you have no screenings outstanding."),
}

# ENGINE reasons — the rules did read a record and could not decide. Only
# valid on that path, and the clause names ONLY what was missing from it. One
# `demographics-unavailable` covering every case told a person whose birthDate
# was on file that it was not; a sharper reason on a record we never opened is
# the same false statement with better aim (#417).
_DEMOGRAPHIC_REASONS = {
    frozenset({"birth-date-unknown"}): (
        "birth-date-unavailable",
        "your date of birth was not available to this check"),
    frozenset({"sex-unknown"}): (
        "sex-unavailable",
        "your sex was not recorded in the records this check can read"),
}
# Both missing, or a cause the engine did not record. Claims more than either
# of the above, so it is the fallback and never the common path.
_UNKNOWN_DEMOGRAPHICS = (
    "demographics-unavailable",
    "your date of birth and sex were not available to this check")


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
    # `status` travels with the line so a consumer can separate due from
    # up-to-date without re-deriving it from the prose. The brief's care-gaps
    # section is gaps only, and parsing the message to work that out would be
    # a second copy of a fact this module already owns (#387).
    if status == "due":
        return {"rule_id": r.get("rule_id"), "title": title, "status": status,
                "message": f"You may be due for {title.lower()} ({cadence}). {note}"}
    if status == "up_to_date":
        return {"rule_id": r.get("rule_id"), "title": title, "status": status,
                "message": (f"Your {title.lower()} looks up to date "
                            f"(last on {r.get('last_done')}).")}
    return None


def _name_all(titles):
    if len(titles) == 1:
        return titles[0]
    return f"{', '.join(titles[:-1])} and {titles[-1]}"


def _demographics_marker(undecided, titles):
    """Reason + prose for rules that DID read a record and could not decide."""
    causes = frozenset(
        r.get("indeterminate_reason") for r in undecided) - {None}
    reason, cause = _DEMOGRAPHIC_REASONS.get(causes, _UNKNOWN_DEMOGRAPHICS)
    n = len(undecided)
    noun, pronoun = ("screening", "it is") if n == 1 else ("screenings", "they are")
    named = f": {_name_all(titles)}" if titles else ""
    return reason, (
        f"{n} {noun} could not be checked because {cause}{named}. That is not "
        f"a finding that {pronoun} up to date.")


#: A rule that could not decide because WE do not read all the evidence that
#: satisfies it — not because the record was missing anything (#425). Kept
#: apart from the demographic causes on purpose: "your sex was not recorded"
#: is a statement about the person, and applying it to a screening that failed
#: for our own reason is the #417 defect with a different subject.
_COVERAGE_CAUSE = "evidence-not-read"


def _coverage_marker(undecided, titles):
    """Reason + prose for rules this check knowingly cannot fully read."""
    evidence = sorted({r["unread_evidence"] for r in undecided
                       if r.get("unread_evidence")})
    n = len(undecided)
    noun, pronoun = ("screening", "it is") if n == 1 else ("screenings", "they are")
    named = f": {_name_all(titles)}" if titles else ""
    what = _name_all(evidence) if evidence else "every test that can satisfy them"
    return _COVERAGE_CAUSE, (
        f"{n} {noun} could not be checked because this check does not yet read "
        f"{what}{named}. That is a limit on the check and not a finding that "
        f"{pronoun} up to date.")


def _unevaluated_marker(results, not_evaluated):
    """What was not evaluated and why, or None when the answer is whole.

    An empty list is ambiguous by construction: "we looked and nothing is
    outstanding" and "we never got far enough to look" render identically,
    and the second was reaching patients as the first (#389, after #379 and
    #381). Same posture as r6/labs/interpret.py `_indeterminate` — say what
    could not be decided rather than emitting a clean result.

    A PARTIAL list is that same defect one size down, and the marker used to
    be attached only when `lines` came back entirely empty. A Patient with a
    birthDate and no gender — routine in real feeds — showed four screenings
    with the two sex-gated ones dropped in silence (#417). All-or-nothing is
    the wrong granularity for a completeness marker, so it rides on any
    undecided rule and says how many and which.

    A caller reason OUTRANKS the rules' own causes, and that ordering is the
    whole point. When no record reached the engine, every rule reports the
    date of birth as unknown — an artefact of the call, not a fact about the
    person. Quoting it back produced "Your date of birth and sex were not
    available to this check" for records holding both (#417). While we are not
    looking at someone's demographics, no reason we give about their
    demographics can be true, however precisely aimed.
    """
    undecided = [r for r in results if r.get("status") == "indeterminate"]
    if not not_evaluated and not undecided:
        return None
    titles = [r["title"] for r in undecided if r.get("title")]
    if not_evaluated:
        reason, note = not_evaluated, _NOT_EVALUATED_NOTES[not_evaluated]
    else:
        # Two kinds of undecided, and they must not borrow each other's
        # reason. The record failed to say something, or this check failed to
        # read something. Reporting a coverage gap as "your sex was not
        # recorded" would be false about the person; reporting a demographic
        # gap as a coverage limit would excuse us from a real one.
        coverage = [r for r in undecided
                    if r.get("indeterminate_reason") == _COVERAGE_CAUSE]
        record = [r for r in undecided if r not in coverage]
        if coverage and record:
            _, record_note = _demographics_marker(
                record, [r["title"] for r in record if r.get("title")])
            _, coverage_note = _coverage_marker(
                coverage, [r["title"] for r in coverage if r.get("title")])
            reason = "partly-unchecked"
            note = f"{record_note} {coverage_note}"
        elif coverage:
            reason, note = _coverage_marker(coverage, titles)
        else:
            reason, note = _demographics_marker(record, titles)
    return {"unevaluated": reason, "unevaluated_count": len(undecided),
            "unevaluated_titles": titles, "unevaluated_note": note}


def build_consumer_summary(results, not_evaluated=None):
    """`not_evaluated` is the caller's reason the rules never got a record to
    read — no patient, an ambiguous one, or a resolved one the route did not
    hand over. The caller knows all three and the engine can see none of them,
    since an unidentifiable patient produces exactly the rule results a
    healthy one does."""
    lines = []
    for r in results:
        if r.get("status") in ("due", "up_to_date"):
            line = _consumer_line(r)
            if line:
                lines.append(line)
    out = {"lines": lines, "note": _CONSUMER_NOTE}
    marker = _unevaluated_marker(results, not_evaluated)
    if marker:
        out.update(marker)
    return out
