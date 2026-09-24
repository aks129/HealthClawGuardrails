"""A partial screening review still shows what is due.

Once the brief evaluates care gaps for the tenant's own Patient (#795), a
real record produces a section that is `unavailable` AND carries due items:
someone 45 or older with nothing on file is due for several screenings, and
colorectal cannot be decided because stool tests are not read. The page used
to gate every item on status == "ok", so that person saw no due item at all,
only "Screening review unavailable".

The due items are the engine's own lines; the note is the engine's own
sentence naming what could not be checked and why. Nothing here is inferred,
and an unavailable section with no items keeps today's message.

Its own file because `tests/test_careagents.py` is held by many worktrees
(lane check). Synthetic patient only.
"""

import json

from r6.brief.engine import CARE_GAPS_UNAVAILABLE, build_care_gaps
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

_CARE_GAPS_SECTION = ("https://healthclaw.io/fhir/StructureDefinition/"
                      "brief-section-care-gaps")
_UNAVAILABLE_COPY = "Screening review unavailable"
_NO_GAPS_COPY = "no preventive care items"

# 56, female, nothing on file: colorectal is eligible and undecided (stool
# tests are not read); the other adult screenings are due.
_PATIENT = {"resourceType": "Patient", "id": "syn-1",
            "birthDate": "1970-06-01", "gender": "female"}
_AS_OF = "2026-09-23"


def _brief(status, fields=(), reason=None):
    """The care-gaps section in the wire shape r6/brief/routes.py emits."""
    sub = [{"url": "field", "valueString": json.dumps(f)} for f in fields]
    sub.append({"url": "status", "valueString": status})
    if reason:
        sub.append({"url": "reason", "valueString": reason})
    return {"resourceType": "Basic",
            "extension": [{"url": _CARE_GAPS_SECTION, "extension": sub}]}


def _partial_section():
    """Built from the real evaluator, producer and brief engine, so a drift in
    any of them shows up here rather than in a hand-written payload."""
    consumer = build_consumer_summary(
        evaluate_care_gaps(_PATIENT, as_of=_AS_OF))
    section = build_care_gaps({"consumer": consumer})
    assert section.status == CARE_GAPS_UNAVAILABLE, "fixture is no longer partial"
    assert section.fields, "fixture no longer has due screenings"
    fields = [{"label": f.label, "value": f.value,
               "sourceType": f.source_type, "sourceId": f.source_id}
              for f in section.fields]
    return section, fields


def _page(app, svc, monkeypatch, brief):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_ok_with_items_lists_them_and_adds_no_note(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    field = {"label": "Influenza (flu) vaccine", "value": "You may be due",
             "sourceType": "MeasureReport", "sourceId": "flu-vaccine"}
    body = _page(app, svc, monkeypatch, _brief("ok", [field]))
    assert "Influenza (flu) vaccine" in body
    assert "could not be checked" not in body
    assert _UNAVAILABLE_COPY not in body
    assert _NO_GAPS_COPY not in body


def test_unavailable_with_items_shows_them_and_says_what_was_not_checked(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    """MUTATION: gate the item list on care_gaps_ok again -> every due item
    vanishes behind "Screening review unavailable"."""
    section, fields = _partial_section()
    body = _page(app, svc, monkeypatch,
                 _brief(section.status, fields, section.reason))

    for f in fields:
        assert f["label"] in body, f"due item {f['label']!r} not shown"
    # The engine's own sentence, verbatim: what could not be checked and why.
    assert "Colorectal cancer screening" in section.reason
    assert section.reason in body
    # Partial is not whole, and never "nothing due".
    assert _NO_GAPS_COPY not in body


def test_unavailable_with_items_and_no_note_still_says_it_is_partial(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    """An engine that sent no reason must not leave the list looking whole."""
    _, fields = _partial_section()
    body = _page(app, svc, monkeypatch, _brief("unavailable", fields))
    assert fields[0]["label"] in body
    assert "could not be checked" in body
    assert _NO_GAPS_COPY not in body


def test_unavailable_without_items_keeps_todays_message(  # noqa: F811
        app, svc, monkeypatch):  # noqa: F811
    reason = "There is no patient record connected here yet."
    body = _page(app, svc, monkeypatch, _brief("unavailable", reason=reason))
    assert _UNAVAILABLE_COPY in body
    assert _NO_GAPS_COPY not in body
    # Unchanged from before: the no-items state does not quote the reason.
    assert reason not in body


def test_parse_care_gaps_reason():
    from careagents.app import _parse_care_gaps_reason
    assert _parse_care_gaps_reason(_brief("unavailable", reason="why")) == "why"
    assert _parse_care_gaps_reason(_brief("ok")) == ""
    assert _parse_care_gaps_reason({}) == ""
    assert _parse_care_gaps_reason(None) == ""
    assert _parse_care_gaps_reason({"extension": "garbage"}) == ""
