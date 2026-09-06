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


def _unread(rule_id, title, evidence):
    """An indeterminate rule the check knowingly cannot fully read.

    `applicable` is True, as the engine emits it: the patient IS in the age
    band, and it is our own coverage that could not decide. This fixture used
    to inherit `applicable: None` from `_undecided`, which no test read until
    #436 made it load-bearing — a hand-written fixture drifting from the
    producer, which is the same shape as the brief's "due" key (#387/#435).
    Pinned against the real evaluator by
    `test_no_screening_reaches_the_patient_as_silence`.
    """
    out = _undecided(rule_id, title, "evidence-not-read")
    out["applicable"] = True
    out["unread_evidence"] = evidence
    return out


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


def test_an_undecided_screening_the_patient_is_eligible_for_gets_a_line():
    """#436 — an indeterminate screening used to give the patient no line.

    PIN FLIPPED: this test asserted `lines == []` for every indeterminate.
    #428 made colorectal screening indeterminate and its PR claimed the prompt
    to act survived the status change. It did not: `lines` was built only for
    `due` and `up_to_date`, so the patient saw the screening named in the
    unevaluated note and never a line telling them what to do about it.

    Three rows, and only the third earns a line:

      - `not_applicable` — the rule does not apply to this person. Silence is
        correct; a line would invent a screening they do not need.
      - `indeterminate` with `applicable: None` — we do not know whether it
        applies, because a demographic was missing. A line here would surface
        a screening that may well be for somebody else. It is named in
        `unevaluated_titles` instead, which claims nothing about eligibility.
      - `indeterminate` with `applicable: True` — the person IS eligible and
        the check could not decide. That is the one with something to say.

    MUTATION: build lines for `("due", "up_to_date")` again -> red.
    """
    results = [
        _result(rule_id="mammography", status="not_applicable", applicable=False),
        _undecided("cervical-screening", "Cervical cancer screening (Pap)",
                   "sex-unknown"),
        _unread("colorectal-screening", "Colorectal cancer screening",
                "stool-based tests (FIT or Cologuard)"),
    ]
    consumer = build_consumer_summary(results)
    assert [line["rule_id"] for line in consumer["lines"]] == [
        "colorectal-screening"]


def test_the_could_not_check_line_carries_its_own_status_and_the_rules_note():
    """It sits BESIDE the due lines rather than under them, so it needs a
    status of its own and a word for it a patient can read.

    `status` stays the engine's own value, so a caller reading `lines` and a
    caller reading `detail` or `summary` never disagree about one screening.
    `status_label` exists because the MCP App page renders a status by
    replacing underscores with spaces — which yields "due" and "up to date",
    and would yield "indeterminate".
    """
    results = [_unread("colorectal-screening", "Colorectal cancer screening",
                       "stool-based tests (FIT or Cologuard)")]
    line = build_consumer_summary(results)["lines"][0]
    assert line["status"] == "indeterminate"
    assert line["status_label"] == "could not check"
    assert line["title"] == "Colorectal cancer screening"
    # The rule's own note, written for a patient, reaches the patient.
    assert results[0]["note"] in line["message"]
    assert "could not check" in line["message"].lower()


def test_a_due_line_is_not_relabelled_by_the_could_not_check_line():
    """The other half of the same property: adding a third kind of line must
    not change the two that shipped."""
    consumer = build_consumer_summary([
        _result(status="due", note="confirm with your clinician"),
        _result(rule_id="mammography", title="Breast cancer screening",
                status="up_to_date", last_done="2026-03-01"),
    ])
    assert [line["status"] for line in consumer["lines"]] == [
        "due", "up_to_date"]
    for line in consumer["lines"]:
        assert "status_label" not in line


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


# ─────────────────────────────────────────────
# #425 — a coverage gap is not a gap in the record
# ─────────────────────────────────────────────

def test_a_coverage_gap_never_borrows_a_reason_about_the_person():
    """The reason has to name whose limitation it is.

    Colorectal screening is undecided because this check reads only
    colonoscopy and sigmoidoscopy procedures, never the stool-based tests that
    also satisfy it. Saying "your sex was not recorded" about that screening
    would be false about the patient; saying "we do not read everything" about
    a genuinely missing sex would excuse a real gap in the record. Both
    directions are #417 with the subject swapped.

    MUTATION: fold the coverage rules back into _demographics_marker -> red,
    the reason becomes demographics-unavailable and the note claims the
    person's record was short. Ran it, saw red.
    """
    results = [
        _result(status="due"),
        _undecided("cervical-screening", "Cervical cancer screening (Pap)",
                   "sex-unknown"),
        _unread("colorectal-screening", "Colorectal cancer screening",
                "stool-based tests (FIT or Cologuard)"),
    ]
    out = build_consumer_summary(results)

    assert out["unevaluated"] == "partly-unchecked"
    assert out["unevaluated_count"] == 2
    note = out["unevaluated_note"]
    # Each screening sits with its own cause.
    assert "sex was not recorded" in note
    assert "Cervical cancer screening (Pap)" in note
    assert "does not yet read stool-based tests (FIT or Cologuard)" in note
    assert "Colorectal cancer screening" in note
    # And neither cause is offered as a finding about the patient's care.
    assert "not a finding" in note


def test_a_coverage_gap_alone_reads_as_our_limit_not_the_record_s():
    """MUTATION: return the demographics fallback when no record cause exists
    -> red with demographics-unavailable. Ran it, saw red."""
    out = build_consumer_summary([
        _result(status="due"),
        _unread("colorectal-screening", "Colorectal cancer screening",
                "stool-based tests (FIT or Cologuard)"),
    ])
    assert out["unevaluated"] == "evidence-not-read"
    assert out["unevaluated_count"] == 1
    note = out["unevaluated_note"]
    assert "limit on the check" in note
    assert "not a finding that it is up to date" in note
    assert "your" not in note.lower().split("up to date")[0].replace(
        "your clinician", ""), "a coverage limit must not describe the patient"


def test_demographic_causes_alone_are_unchanged_by_the_split():
    """The existing shape must survive. MUTATION: route record causes through
    _coverage_marker -> red."""
    out = build_consumer_summary([
        _result(status="due"),
        _undecided("cervical-screening", "Cervical cancer screening (Pap)",
                   "sex-unknown"),
        _undecided("mammography", "Breast cancer screening (mammogram)",
                   "sex-unknown"),
    ])
    assert out["unevaluated"] == "sex-unavailable"
    assert out["unevaluated_count"] == 2
    assert "sex was not recorded" in out["unevaluated_note"]


# ─────────────────────────────────────────────
# Nothing is dropped in silence (#436)
# ─────────────────────────────────────────────

def _every_screening_is_accounted_for(results, consumer):
    """A line for every screening the person is eligible for; the rest named.

    Written twice. The first version accepted "has a line OR is named in
    `unevaluated_titles`", which was green BEFORE the fix — the marker already
    named colorectal — so it would have shipped as evidence for a change it
    could not see. That is the docs/2026-08-02-retro.md shape aimed at this
    file, and the reason to state the property in terms of what #436 is
    actually about:

      - `applicable is True` — the person is eligible and the screening has
        something to tell them, decided or not. It needs a LINE. Being named
        in the marker is not enough; that was the defect.
      - `applicable is None` — eligibility itself is undecided. Naming it is
        the most that can be said without claiming it applies to this person.
      - `not_applicable` — silence is correct.
    """
    lined = {line["rule_id"] for line in consumer["lines"]}
    named = set(consumer.get("unevaluated_titles") or [])
    for r in results:
        if r["status"] == "not_applicable":
            continue
        if r["applicable"] is True:
            assert r["rule_id"] in lined, (
                f"{r['rule_id']} ({r['status']}) applies to this person and "
                "got no patient-facing line")
        else:
            assert r["title"] in named, (
                f"{r['rule_id']} ({r['status']}) was dropped in silence: not "
                "decided, and not named as unchecked")


def test_no_screening_reaches_the_patient_as_silence():
    """Driven from the ENGINE's own output rather than hand-built rows.

    A fixture written here is free to drift from what the evaluator emits,
    which is how the brief came to require a "due" key the producer had never
    written (#387/#435). Two records, because they exercise different gates:
    a complete one, and one missing sex — routine in real feeds.

    MUTATION: drop the indeterminate branch from `_consumer_line` -> red on
    colorectal, which has a line and no other way to reach the patient.
    """
    from r6.caregaps.evaluate import evaluate_care_gaps

    for patient in ({"resourceType": "Patient", "gender": "female",
                     "birthDate": "1976-01-01"},
                    {"resourceType": "Patient", "birthDate": "1976-01-01"}):
        results = evaluate_care_gaps(patient, as_of="2026-07-01")
        consumer = build_consumer_summary(results)
        _every_screening_is_accounted_for(results, consumer)


def test_the_could_not_check_line_is_no_excuse_to_stop_naming_it():
    """A line AND the marker. The marker is what says how much of the answer
    is missing; a per-rule line does not replace a count of them (#417)."""
    from r6.caregaps.evaluate import evaluate_care_gaps

    results = evaluate_care_gaps(
        {"resourceType": "Patient", "gender": "female",
         "birthDate": "1976-01-01"}, as_of="2026-07-01")
    consumer = build_consumer_summary(results)
    assert [line["rule_id"] for line in consumer["lines"]
            if line["status"] == "indeterminate"] == ["colorectal-screening"]
    assert consumer["unevaluated"] == "evidence-not-read"
    assert consumer["unevaluated_titles"] == ["Colorectal cancer screening"]
