import re

from r6.caregaps.report import build_caregaps_summary, build_consumer_summary

_BANNED = re.compile(r"diagnos|prescrib|treatment", re.IGNORECASE)


def _result(rule_id="bp-screening", title="Blood pressure check", cadence="yearly",
           source="uspstf", last_done=None, note="", applicable=True,
           status="due", indeterminate_reason=None):
    out = {"rule_id": rule_id, "title": title, "cadence": cadence, "source": source,
           "last_done": last_done, "note": note, "applicable": applicable,
           "status": status}
    if indeterminate_reason:
        out["indeterminate_reason"] = indeterminate_reason
    return out


def _undecided(rule_id, title, why):
    """An indeterminate rule as the engine emits it — carrying the cause."""
    return _result(rule_id=rule_id, title=title, status="indeterminate",
                   applicable=None, indeterminate_reason=why)


def test_summary_counts_by_status():
    results = [
        _result(status="due"),
        _result(rule_id="lipid-screening", status="due"),
        _result(rule_id="mammography", status="up_to_date", last_done="2026-01-01"),
        _result(rule_id="cervical-screening", status="not_applicable", applicable=False),
        _result(rule_id="colorectal-screening", status="indeterminate", applicable=None),
    ]
    summary = build_caregaps_summary(results)
    assert summary["due"] == 2
    assert summary["up_to_date"] == 1
    assert summary["not_applicable"] == 1
    assert summary["indeterminate"] == 1
    assert summary["total"] == 5


def test_summary_gaps_lists_only_due_rules():
    results = [
        _result(status="due", note="no record found"),
        _result(rule_id="mammography", status="up_to_date", last_done="2026-01-01"),
    ]
    summary = build_caregaps_summary(results)
    assert summary["gaps"] == [
        {"rule_id": "bp-screening", "title": "Blood pressure check",
         "note": "no record found"},
    ]


def test_consumer_summary_due_line_mentions_title_and_clinician():
    results = [_result(status="due", note="confirm with your clinician")]
    consumer = build_consumer_summary(results)
    assert len(consumer["lines"]) == 1
    line = consumer["lines"][0]
    assert "blood pressure check" in line["message"].lower()
    assert "clinician" in consumer["note"].lower()


def test_consumer_summary_up_to_date_line_mentions_last_done():
    results = [_result(status="up_to_date", last_done="2026-03-01")]
    consumer = build_consumer_summary(results)
    line = consumer["lines"][0]
    assert "2026-03-01" in line["message"]
    assert "up to date" in line["message"].lower()


def test_consumer_summary_skips_not_applicable_and_indeterminate():
    results = [
        _result(rule_id="mammography", status="not_applicable", applicable=False),
        _result(rule_id="colorectal-screening", status="indeterminate", applicable=None),
    ]
    consumer = build_consumer_summary(results)
    assert consumer["lines"] == []


def test_consumer_summary_note_has_no_banned_words():
    results = [_result(status="due")]
    consumer = build_consumer_summary(results)
    assert not _BANNED.search(consumer["note"])
    for line in consumer["lines"]:
        assert not _BANNED.search(line["message"])


def test_consumer_summary_note_text():
    consumer = build_consumer_summary([])
    assert consumer["note"] == (
        "These are general preventive-care reminders based on published "
        "guidelines — not personalized medical advice. Your connected "
        "records may be incomplete, so confirm anything here with your "
        "clinician.")


# ─────────────────────────────────────────────
# Why the list is empty (#389)
# ─────────────────────────────────────────────

def test_unresolved_subject_travels_with_the_empty_list():
    """An unresolvable subject is carried, not swallowed. The caller cannot
    otherwise tell it apart from a clean sheet."""
    consumer = build_consumer_summary([], not_evaluated="no-patient")
    assert consumer["lines"] == []
    assert consumer["unevaluated"] == "no-patient"
    assert "not a finding" in consumer["unevaluated_note"]


def test_all_indeterminate_rules_are_not_reported_as_nothing_due():
    """Every rule filtered out for want of age or sex. The list is empty
    because nothing was decided, which is not the same as nothing being due.

    MUTATION: return the plain {"lines": [], "note": ...} -> red.
    """
    consumer = build_consumer_summary(
        [_result(status="indeterminate", applicable=None),
         _result(rule_id="mammography", status="not_applicable",
                 applicable=False)])
    assert consumer["unevaluated"] == "demographics-unavailable"


def test_no_lines_and_no_indeterminate_rules_claims_no_reason():
    """All rules decided and none outstanding: the emptiness is a finding,
    so it carries no excuse for itself."""
    consumer = build_consumer_summary(
        [_result(status="not_applicable", applicable=False)])
    assert "unevaluated" not in consumer


def test_unevaluated_notes_have_no_banned_words():
    for reason in ("no-patient", "ambiguous-patient", "check-incomplete"):
        consumer = build_consumer_summary([], not_evaluated=reason)
        assert not _BANNED.search(consumer["unevaluated_note"])
    for why in ("birth-date-unknown", "sex-unknown"):
        consumer = build_consumer_summary(
            [_undecided("mammography", "Breast cancer screening (mammogram)", why)])
        assert not _BANNED.search(consumer["unevaluated_note"])


# ─────────────────────────────────────────────
# A reason may only claim what was actually read (#417)
# ─────────────────────────────────────────────

def test_the_reason_names_only_the_demographic_that_was_actually_missing():
    """Reachable when a caller supplies a subject whose Patient really is
    missing a field — the one path where we read the record and can say what
    was not in it.

    One `demographics-unavailable` covering every case told a person whose
    birthDate was on file that it was not.
    """
    sex_only = build_consumer_summary(
        [_undecided("mammography", "Breast cancer screening (mammogram)",
                    "sex-unknown")])
    assert sex_only["unevaluated"] == "sex-unavailable"
    assert "date of birth" not in sex_only["unevaluated_note"]

    dob_only = build_consumer_summary(
        [_undecided("bp-screening", "Blood pressure check", "birth-date-unknown")])
    assert dob_only["unevaluated"] == "birth-date-unavailable"
    assert "sex" not in dob_only["unevaluated_note"]


def test_a_caller_reason_outranks_anything_the_rules_seem_to_say():
    """When the caller knows no Patient reached the evaluator, the rules'
    own causes are an artefact of that and must not be quoted back.

    Every rule below claims the date of birth was unknown. It was not — the
    caller simply never handed it over.
    """
    consumer = build_consumer_summary(
        [_undecided("bp-screening", "Blood pressure check", "birth-date-unknown"),
         _undecided("flu-immunization", "Influenza (flu) vaccine",
                    "birth-date-unknown")],
        not_evaluated="check-incomplete")
    assert consumer["unevaluated"] == "check-incomplete"
    assert "date of birth" not in consumer["unevaluated_note"]


def test_a_partial_list_carries_the_marker_alongside_its_lines():
    """Screenings decided and screenings not decided is not "the screenings".

    The marker was attached only when `lines` came back entirely empty, so a
    Patient with a birthDate and no gender had the two sex-gated rules dropped
    with no hint they existed. All-or-nothing is the wrong granularity for a
    completeness marker.

    MUTATION: guard the marker with `if not lines:` again -> red.
    """
    consumer = build_consumer_summary([
        _result(status="due"),
        _result(rule_id="flu-immunization", title="Influenza (flu) vaccine",
                status="due"),
        _undecided("cervical-screening", "Cervical cancer screening (Pap)",
                   "sex-unknown"),
        _undecided("mammography", "Breast cancer screening (mammogram)",
                   "sex-unknown"),
    ])
    assert len(consumer["lines"]) == 2
    assert consumer["unevaluated"] == "sex-unavailable"
    assert consumer["unevaluated_count"] == 2
    assert consumer["unevaluated_titles"] == [
        "Cervical cancer screening (Pap)",
        "Breast cancer screening (mammogram)"]
    # Counted and named — a partial answer has to say how much of it is missing.
    assert "2 screenings" in consumer["unevaluated_note"]
    for title in consumer["unevaluated_titles"]:
        assert title in consumer["unevaluated_note"]


def test_one_undecided_rule_reads_as_one():
    consumer = build_consumer_summary(
        [_undecided("mammography", "Breast cancer screening (mammogram)",
                    "sex-unknown")])
    assert "1 screening could not be checked" in consumer["unevaluated_note"]
    assert "screenings" not in consumer["unevaluated_note"]
