"""The CareAgents /brief page as something a patient hands a clinician.

The engine half is tests/test_brief_visit_handoff.py. Here: a review that ran
but could not decide one screening reaches the page as that screening, not
as "We couldn't check your screenings just now" (item 3), and the page can
be printed or saved (item 6). Synthetic patient only. Its own file because
tests/test_careagents.py is held by many worktrees (lane check).
"""

import json
import re

from r6.brief.engine import CARE_GAPS_UNAVAILABLE, build_care_gaps
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from tests.careagents_consent_helpers import consented
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

_CARE_GAPS_SECTION = ("https://healthclaw.io/fhir/StructureDefinition/"
                      "brief-section-care-gaps")
_UNAVAILABLE_COPY = "We couldn't check your screenings just now."

# 56, male. Blood pressure, cholesterol and a flu shot are current, so
# nothing is due; colorectal is eligible and undecided.
_AS_OF = "2026-10-26"
_PATIENT = {"resourceType": "Patient", "id": "syn-h",
            "birthDate": "1970-06-01", "gender": "male"}
_RECENT = "2026-09-26"


def _brief_from_real_engine():
    consumer = build_consumer_summary(evaluate_care_gaps(
        _PATIENT, as_of=_AS_OF,
        observations=[
            {"resourceType": "Observation", "effectiveDateTime": _RECENT,
             "code": {"coding": [{"code": "85354-9"}]}},
            {"resourceType": "Observation", "effectiveDateTime": _RECENT,
             "code": {"coding": [{"code": "2093-3"}]}}],
        immunizations=[
            {"resourceType": "Immunization", "occurrenceDateTime": _RECENT,
             "vaccineCode": {"coding": [{"code": "140"}]}}]))
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_UNAVAILABLE
    sub = [{"url": "field", "valueString": json.dumps({
        "label": f.label, "value": f.value, "sourceType": f.source_type,
        "sourceId": f.source_id})} for f in section.fields]
    sub.append({"url": "status", "valueString": section.status})
    sub.append({"url": "reason", "valueString": section.reason})
    return section, {"resourceType": "Basic", "extension": [
        {"url": _CARE_GAPS_SECTION, "extension": sub}]}


def _page(app, svc, monkeypatch, brief):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant, **_: brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_a_review_that_ran_names_the_screening_it_could_not_check(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    """MUTATION: keep only `due` lines in build_care_gaps -> the page says
    "We couldn't check your screenings just now" again."""
    section, brief = _brief_from_real_engine()
    body = _page(app, svc, monkeypatch, brief)
    assert "Colorectal cancer screening" in body
    assert "We could not check whether you are due for" in body
    assert section.reason in body
    assert _UNAVAILABLE_COPY not in body


def test_the_brief_can_be_printed(app, svc, monkeypatch):  # noqa: F811
    """A Print button (shown by brief.js) and a print-only stylesheet that
    hides the app's own controls."""
    body = _page(app, svc, monkeypatch, {"resourceType": "Basic"})
    assert re.search(r'<button[^>]*id="brief-print"', body)
    assert re.search(
        r'<link[^>]*href="[^"]*brief-print\.css[^"]*"[^>]*media="print"',
        body)
    assert re.search(r'<script src="[^"]*/brief\.js[^"]*"', body)

    css = app.test_client().get("/static/brief-print.css")
    text = css.get_data(as_text=True)
    css.close()
    for hidden in (".hub-head-actions", ".brief-tools", ".chat-feedback"):
        assert hidden in text
    # The date stays on paper.
    assert "brief-asof" not in text
    # A printed sample must still say it is one.
    assert ".sample-banner { position: static" in text


def test_the_demographic_causes_match_the_engine():
    """CareAgents imports nothing from the engine, so it mirrors the codes;
    this holds the mirror equal to r6/caregaps/report.py."""
    from careagents.brief import DEMOGRAPHIC_CAUSES
    from r6.caregaps.report import _DEMOGRAPHIC_REASONS, _UNKNOWN_DEMOGRAPHICS
    engine = {code for code, _ in _DEMOGRAPHIC_REASONS.values()}
    engine.add(_UNKNOWN_DEMOGRAPHICS[0])
    assert DEMOGRAPHIC_CAUSES == engine


def test_a_caller_or_engine_failure_still_reads_just_now(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    """Only a demographic cause quotes the reason; a review that never ran
    keeps today's message (tests/test_careagents_brief_partial.py)."""
    brief = {"resourceType": "Basic", "extension": [{
        "url": _CARE_GAPS_SECTION, "extension": [
            {"url": "status", "valueString": "unavailable"},
            {"url": "reason", "valueString": "the screening review could "
                                             "not be completed"},
            {"url": "cause", "valueString": "no-patient"}]}]}
    body = _page(app, svc, monkeypatch, brief)
    assert _UNAVAILABLE_COPY in body
    assert "could not be completed" not in body


def test_the_brief_says_as_of_when(app, svc, monkeypatch):  # noqa: F811
    """#1038 F6: the day the records were read, on screen and on paper."""
    from datetime import datetime, timezone

    from careagents.app import _brief_as_of
    assert _brief_as_of(datetime(2026, 10, 6, tzinfo=timezone.utc)) == (
        "Oct 6, 2026")
    body = _page(app, svc, monkeypatch, {"resourceType": "Basic"})
    # The read instant, for brief.js to show in the viewer's time zone, and
    # the UTC date as the no-script fallback.
    import re
    m = re.search(r'<div class="hub-sub brief-asof" data-as-of="([^"]+)">'
                  r'As of ([^<]+)</div>', body)
    assert m, "no As of line"
    instant = datetime.fromisoformat(m.group(1))
    assert instant.tzinfo is not None
    assert m.group(2) == _brief_as_of(instant)

