"""The brief renders the care-gaps contract its producer actually emits.

#387/#435: `build_care_gaps` required a "due" key carrying
{measure, reason, status, id}. `r6/caregaps/report.py::build_consumer_summary`
returns {"lines": [{rule_id, title, message}], "note": ...} plus optional
`unevaluated*` markers, and has never emitted "due" at any point in its
history. Every brief therefore rendered "the screening review returned an
unreadable result" — reproduced against production on 2026-08-06 — for a key
mismatch rather than an error.

Two copies of one fact drifting is Generator B in
`docs/2026-08-06-two-generators-three-laws.md`. The fix is Law 2: the producer
owns the shape, the brief imports and renders it, and these tests assert
against the PRODUCER's real output rather than a hand-written fixture that
would be free to drift the same way the old shape did.
"""

import pytest

from r6.brief.engine import CARE_GAPS_OK, CARE_GAPS_UNAVAILABLE, build_care_gaps
from r6.caregaps.report import build_consumer_summary


def _result(rule_id, title, status, **extra):
    return {"rule_id": rule_id, "title": title, "status": status,
            "cadence": "every 10 years", "note": "Ask your clinician.",
            "last_done": "2020-01-02", **extra}


def test_the_producers_real_output_is_readable_by_the_brief():
    """The bug, stated as a property. Built by the producer, not by hand —
    a fixture written here could drift exactly as the old "due" key did."""
    consumer = build_consumer_summary([_result("crc", "Colorectal screening",
                                               "due")])
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_OK, section.reason
    assert [f.label for f in section.fields] == ["Colorectal screening"]


def test_a_due_screening_carries_the_producers_own_sentence():
    """The message is written once, in report.py, for every surface."""
    consumer = build_consumer_summary([_result("crc", "Colorectal screening",
                                               "due")])
    section = build_care_gaps({"consumer": consumer})
    assert section.fields[0].value == consumer["lines"][0]["message"]
    assert section.fields[0].source_id == "crc"


def test_an_up_to_date_screening_is_not_reported_as_a_gap():
    """`lines` carries both due and up-to-date; the section is gaps only.

    MUTATION: render every line -> an up-to-date screening appears under
    "care gaps", which tells a patient to chase a screening they have had.
    """
    consumer = build_consumer_summary([
        _result("crc", "Colorectal screening", "due"),
        _result("a1c", "Diabetes A1c monitoring", "up_to_date"),
    ])
    section = build_care_gaps({"consumer": consumer})
    assert [f.label for f in section.fields] == ["Colorectal screening"]


def test_nothing_due_is_only_said_when_something_was_actually_checked():
    """An empty gap list from a whole evaluation is a real answer."""
    consumer = build_consumer_summary([_result("a1c", "Diabetes A1c",
                                               "up_to_date")])
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_OK
    assert section.fields == []


def test_an_unevaluated_check_never_renders_as_nothing_due():
    """Law 1, and the reason this is not a key rename.

    The brief passes patient=None, so with only the key fixed the section
    would render an EMPTY gap list — which #428 established is a clinical
    claim: "nothing is due" is only ever repeated from a result that made it.
    The producer already says so via `unevaluated`; the brief must not
    discard it.

    MUTATION: ignore consumer["unevaluated"] -> this goes green-with-ok and
    the patient is told nothing is outstanding by a check that read no record.
    """
    consumer = build_consumer_summary([], not_evaluated="no-patient")
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_UNAVAILABLE
    assert section.reason == consumer["unevaluated_note"]
    assert section.fields == []


def test_a_partial_answer_is_not_sold_as_a_whole_one():
    """#417 one layer up: some rules decided, some did not."""
    consumer = build_consumer_summary([
        _result("crc", "Colorectal screening", "due"),
        _result("mam", "Mammography", "indeterminate"),
    ])
    assert "unevaluated" in consumer, "producer changed; this test is stale"
    section = build_care_gaps({"consumer": consumer})
    assert [f.label for f in section.fields] == ["Colorectal screening"]
    assert section.status == CARE_GAPS_UNAVAILABLE
    assert "Mammography" in section.reason


@pytest.mark.parametrize("payload", [{}, {"consumer": None},
                                     {"consumer": {"note": "x"}}])
def test_a_payload_that_is_not_the_contract_is_still_unreadable(payload):
    """The guard stays — it was pointed at the wrong key, not wrong to exist."""
    assert build_care_gaps(payload).status == CARE_GAPS_UNAVAILABLE
