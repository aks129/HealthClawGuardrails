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
    for hidden in (".hub-head-actions", ".brief-print", ".chat-feedback"):
        assert hidden in text
    # A printed sample must still say it is one.
    assert ".sample-banner { position: static" in text
