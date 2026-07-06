import re

from r6.caregaps.report import build_caregaps_summary, build_consumer_summary

_BANNED = re.compile(r"diagnos|prescrib|treatment", re.IGNORECASE)


def _result(rule_id="bp-screening", title="Blood pressure check", cadence="yearly",
           source="uspstf", last_done=None, note="", applicable=True,
           status="due"):
    return {"rule_id": rule_id, "title": title, "cadence": cadence, "source": source,
            "last_done": last_done, "note": note, "applicable": applicable,
            "status": status}


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
