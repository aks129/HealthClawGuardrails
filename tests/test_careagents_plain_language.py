"""Plain words for a beta tester on a phone (#876, #877).

A patient tester at 375px found record jargon and safety-engineering words
on the screens a non-technical person reads: record types and ids on the
visit brief, a lowercase sentence from the screening check, "guardrailed",
"redacted" and "audit trail" on every page. Behaviour changes too: a failed
chat turn left the question with no answer, and a failed request sat in the
hub's Recently list for good.

Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, app, cfg, svc)

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Words a tester-facing CareAgents screen does not use.
JARGON = ("guardrail", "redact", "tenant", "FHIR", "audit trail")


def _visible(html: str) -> str:
    """Text a person reads: no comments, scripts, styles or tags."""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


# --- #877: the visit brief --------------------------------------------------

_SECTION = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"


def _brief():
    def section(name, field):
        return {"url": _SECTION + name, "extension": [
            {"url": "field", "valueString": json.dumps(field)}]}
    return {"resourceType": "Basic", "extension": [
        section("problems", {"label": "Hypertension", "value": "Active",
                             "sourceType": "Condition",
                             "sourceId": "demo-cond-htn"}),
        section("medications", {"label": "Lisinopril", "value": "Daily",
                                "sourceType": "MedicationRequest",
                                "sourceId": "demo-med-lis"}),
        section("labs", {"label": "Hemoglobin A1c", "value": "6.1 %",
                         "sourceType": "Observation",
                         "sourceId": "demo-obs-a1c"}),
        section("care-gaps", {"label": "Colorectal cancer screening",
                              "value": "You may be due.",
                              "sourceType": "MeasureReport",
                              "sourceId": "crc-screening"}),
        section("visits", {"label": "Office visit", "value": "Finished",
                           "sourceType": "Encounter",
                           "sourceId": "demo-enc-1"}),
    ]}


def _brief_page(app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief())
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_the_brief_shows_no_record_type_or_id(app, svc, monkeypatch):  # noqa: F811
    page = _visible(_brief_page(app, svc, monkeypatch))
    for word in ("Condition", "MedicationRequest", "Observation",
                 "MeasureReport", "Encounter", "demo-cond-htn",
                 "demo-med-lis", "demo-obs-a1c", "crc-screening",
                 "demo-enc-1", "read-only"):
        assert word not in page, word
    assert page.count("From your records") == 4
    # Preventive care is due because nothing was found: no record behind it.
    assert page.count("Based on your age and sex") == 1
    # The items themselves still show.
    assert "Hypertension" in page and "Hemoglobin A1c" in page


def test_a_lab_value_never_carries_upstream_free_text():
    """valueString is free text from the source system: there is no code to
    label it by, so the engine drops it. The brief route already strips it
    through apply_redaction; this is the engine not depending on that."""
    from r6.brief.engine import build_labs
    [field] = build_labs([{
        "resourceType": "Observation", "id": "o1",
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "4548-4"}]},
        "valueString": "Jane Doe: positive, see note",
        "effectiveDateTime": "2026-09-01T08:00:00Z"}])
    assert "Jane" not in field.value
    assert field.value == "Result not listed in your records (Sep 1, 2026)"


def test_no_upstream_text_or_display_reaches_the_brief():
    """#884 QA F3, the CLAUDE.md rule: labels come from r6/terminology.py by
    code. A canary in every text/display field the engine could read is
    absent from every field it writes, on unredacted input."""
    from r6.brief.engine import (
        UNLABELLED, build_labs, build_medications, build_problems,
        build_visits)
    loinc_a1c = {"system": "http://loinc.org", "code": "4548-4",
                 "display": "CANARY-1"}
    obs = {"resourceType": "Observation", "id": "o1",
           "code": {"coding": [loinc_a1c], "text": "CANARY-2"},
           "valueCodeableConcept": {
               "coding": [{"system": "urn:x", "code": "pos",
                           "display": "CANARY-3"}], "text": "CANARY-4"},
           "effectiveDateTime": "2026-09-01"}
    meds = [
        {"resourceType": "MedicationRequest", "id": "m1", "status": "active",
         "medicationCodeableConcept": {
             "coding": [{"system": "urn:x", "code": "zz",
                         "display": "CANARY-5"}], "text": "CANARY-6"},
         "dosageInstruction": [{"text": "CANARY-7"}]},
        {"resourceType": "MedicationRequest", "id": "m2", "status": "active",
         "medicationReference": {"reference": "Medication/x",
                                 "display": "CANARY-8"}},
    ]
    cond = {"resourceType": "Condition", "id": "c1",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": "urn:x", "code": "q",
                                 "display": "CANARY-9"}],
                     "text": "CANARY-10"}}
    enc = {"resourceType": "Encounter", "id": "e1", "status": "finished",
           "type": [{"coding": [{"system": "urn:x", "code": "v",
                                 "display": "CANARY-11"}],
                     "text": "CANARY-12"}]}
    fields = (build_labs([obs]) + build_medications(meds)
              + build_problems([cond]) + build_visits([enc]))
    for f in fields:
        assert "CANARY" not in f.label + f.value, f
    labs = build_labs([obs])
    assert labs[0].label == "Hemoglobin A1c"        # by code, from the table
    assert [m.label for m in build_medications(meds)] == [UNLABELLED] * 2


def test_a_blood_pressure_panel_shows_its_numbers():
    """#884 G7: the row showed a date and no reading."""
    from r6.brief.engine import build_labs
    [field] = build_labs([{
        "resourceType": "Observation", "id": "bp",
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "85354-9"}]},
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org",
                                  "code": "8480-6"}]},
             "valueQuantity": {"value": 120, "unit": "mmHg"}},
            {"code": {"coding": [{"system": "http://loinc.org",
                                  "code": "8462-4"}]},
             "valueQuantity": {"value": 80.0, "unit": "mmHg"}}],
        "effectiveDateTime": "2026-10-06T08:00:00Z"}])
    assert field.value == "120/80 mmHg (Oct 6, 2026)"


@pytest.mark.parametrize("vq,shown", [
    ({"value": float("nan"), "unit": "%"}, "Result not listed in your records"),
    ({"value": True, "unit": "%"}, "Result not listed in your records"),
    ({"value": "6.1", "unit": "%"}, "Result not listed in your records"),
    ({"value": 6.1, "unit": "%"}, "6.1 %"),     # the analyte's known unit
    ({"value": 6.1, "system": "http://unitsofmeasure.org",
      "code": "mmol/mol"}, "6.1 %"),           # off the list: known unit
    ({"value": 6.1, "system": "urn:other", "code": "mg/dL"}, "6.1 %"),
])
def test_a_lab_value_is_a_finite_number_with_a_coded_unit(vq, shown):
    """R884-1: never valueQuantity.unit, never a value that is not a
    finite number."""
    from r6.brief.engine import build_labs
    [field] = build_labs([{
        "resourceType": "Observation", "id": "o1",
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "4548-4"}]},
        "valueQuantity": vq, "effectiveDateTime": "2026-09-01"}])
    assert field.value == f"{shown} (Sep 1, 2026)"


def test_a_screening_note_starts_its_own_sentence():
    """"…every 5 years). no record found…" read as a typo on a phone."""
    from r6.caregaps.report import _consumer_line
    due = _consumer_line({
        "rule_id": "crc", "title": "Colorectal cancer screening",
        "cadence": "every 10 years", "status": "due",
        "note": "no record found in your connected data"})
    after = due["message"].split("). ", 1)[1]
    assert after[0].isupper(), due["message"]
    unsure = _consumer_line({
        "rule_id": "crc", "title": "Colorectal cancer screening",
        "cadence": "every 10 years", "status": "indeterminate",
        "applicable": True, "note": "we do not yet read colonoscopy reports"})
    after = unsure["message"].split("). ", 1)[1]
    assert after[0].isupper(), unsure["message"]


# --- #876: a failed chat turn is answered -----------------------------------

def test_a_failed_turn_leaves_an_answer_in_the_history(
        cfg, svc, monkeypatch):  # noqa: F811
    """Without one, the question showed alone on reload, and asking again
    put the same question twice in a row."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker
    _app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("model fell over")
    monkeypatch.setattr(agent_mod.llm, "complete", _boom)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "fail-1"}, buffered=False)
    assert r.status_code == 200
    r.close()
    w = RunWorker(cfg, fake, svc, "fail-worker")
    while w.run_once():
        pass
    rows = [row for rows in fake.logged.values() for row in rows]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["content"] == agent_mod.GENERIC_FAILURE_TEXT
    assert "model fell over" not in rows[1]["content"]
    # Replayed, the run writes it once.
    assert rows[1]["reply_to"] == rows[0]["id"]


def test_a_worker_that_lost_the_run_writes_no_failure_line(
        cfg, svc, monkeypatch):  # noqa: F811
    """The engine refused this worker's failure (its lease is gone), so the
    run may still finish elsewhere with a real answer."""
    from careagents import agent as agent_mod
    from careagents.healthclaw import HealthClawError
    from careagents.worker import RunWorker
    _app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("model fell over")

    def _refused(*_a, **_k):
        raise HealthClawError("worker does not own run", 409)
    monkeypatch.setattr(agent_mod.llm, "complete", _boom)
    monkeypatch.setattr(fake, "transition_agent_run", _refused)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "fail-2"}, buffered=False)
    r.close()
    RunWorker(cfg, fake, svc, "fail-worker").run_once()
    rows = [row for rows in fake.logged.values() for row in rows]
    assert [row["role"] for row in rows] == ["user"]


# --- #876: failed requests leave the hub -----------------------------------

def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_failed_request_leaves_the_hub_after_a_day(cfg, svc, monkeypatch):  # noqa: F811
    from tests.test_careagents_what_happened import (
        OutcomeClient, _app, _signed_in)
    fake = OutcomeClient(pending=[], recent=[
        {"id": "old-fail", "kind": "sms", "status": "failed",
         "updated_at": _iso(timedelta(days=2))},
        {"id": "new-fail", "kind": "sms", "status": "failed",
         "updated_at": _iso(timedelta(hours=2))},
        {"id": "old-check", "kind": "sms", "status": "needs_review",
         "updated_at": _iso(timedelta(days=2))},
        {"id": "undated-fail", "kind": "sms", "status": "failed",
         "updated_at": "not a date"},
    ])
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    ids = {i["id"] for i in c.get("/api/approvals/count").get_json()["recent"]}
    # A request somebody has to check stays; one we cannot date stays.
    assert ids == {"new-fail", "old-check", "undated-fail"}


# --- #876: copy ------------------------------------------------------------

def test_the_landing_page_speaks_plainly(app, svc):  # noqa: F811
    page = _visible(app.test_client().get("/").get_data(as_text=True))
    for word in ("redacts", "audit trail", "provenance", "step-up",
                 "tenant isolation", "guardrailed", "guardrails:"):
        assert word not in page, word


def test_the_header_and_footer_speak_plainly():
    base = _visible((ROOT / "careagents" / "templates" / "base.html")
                    .read_text())
    assert "guardrail" not in base.lower()
    assert "built by HealthClaw" in base


def test_chat_speaks_plainly():
    from careagents.agent import TOOL_LABELS
    assert all("redact" not in label for label in TOOL_LABELS.values())
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert '"guardrails "' not in js
    assert "provenance" not in js
    chat = (ROOT / "careagents" / "templates" / "chat.html").read_text()
    assert "guardrail" not in chat.lower()


def test_the_review_opens_in_the_same_tab_on_a_phone():
    """A new tab in an in-app browser can lose the sign-in."""
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    card = js[js.index("function addReviewCard"):js.index("function addPdfCard")]
    wide = 'matchMedia("(min-width: 700px)").matches) {'
    assert wide in card
    # The only new-tab line is inside the wide-screen branch.
    assert card.count('"_blank"') == 1
    assert card.index(wide) < card.index('"_blank"')


def test_the_review_page_says_about_you_not_demographics():
    page = _visible((ROOT / "templates" / "action_review.html").read_text())
    assert "Demographics" not in page
    assert "read-only" not in page
    assert "About you" in page


def test_a_failed_approval_says_we_could_not_finish():
    handlers = (ROOT / "templates" / "_review_handlers.html").read_text()
    arm = handlers[handlers.index("status === 'failed'"):]
    arm = arm[:arm.index("return;")]
    assert "We couldn't finish this request." in arm
    assert "went through and this request could not be" not in arm


def test_sign_in_says_it_is_also_sign_up():
    """The emails say "sign up"; the page they land on said only "Sign in"."""
    page = (ROOT / "careagents" / "templates" / "auth.html").read_text()
    assert "Sign in or sign up" in page


def test_the_local_stack_names_public_base_url():
    doc = (ROOT / "docs" / "development.md").read_text()
    block = doc[doc.index("## A local CareAgents stack"):]
    assert "PUBLIC_BASE_URL=" in block


# --- #884 patient tester, G7 ------------------------------------------------

def test_the_safety_pill_is_a_link_to_the_grade(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    pill = re.search(r'<a [^>]*id="trust-pill"[^>]*>', page)
    assert pill, "the pill is not a link"
    assert "$conformance" in pill.group(0)
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert 'pill.textContent = "Safety grade: " + grade;' in js


def test_the_review_card_names_the_assistant(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert 'window.CARE_AGENT_NAME = "Juniper";' in page
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert ('AGENT_NAME + " filled it in from your records. Check each " +\n'
            '        "medication and allergy. Nothing is made until you '
            'approve."') in js


def test_chat_bubbles_carry_no_stray_whitespace(cfg, svc, monkeypatch):  # noqa: F811
    """.msg is white-space: pre-wrap, so template indentation was drawn."""
    _app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    first = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    for bubble in re.findall(r'<div class="msg agent">.*?</div>', first, re.S):
        inner = bubble[len('<div class="msg agent">'):-len("</div>")]
        assert inner.startswith("<p>") and inner.endswith("</p>"), bubble
        assert "\n" not in inner, bubble
    fake.logged[(tenant, fake.conversation_id(agent_id))] = [
        {"id": "m1", "role": "user", "content": "how are my labs?"},
        {"role": "assistant", "content": "They look steady."}]
    again = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert '<div class="msg user"><p>how are my labs?</p></div>' in again
    assert '<div class="msg agent"><p>They look steady.</p></div>' in again


def test_the_brief_is_the_visit_brief_with_one_disclaimer(app, svc, monkeypatch):  # noqa: F811
    page = _brief_page(app, svc, monkeypatch)
    assert "<title>Visit brief — CareAgents</title>" in page
    assert "<h1>Visit brief</h1>" in page
    assert "Current conditions" in page and "Active problems" not in page
    assert "clinician" not in _visible(page)
    assert _visible(page).count("Not medical advice") == 1
    assert "decision support" not in _visible(page)


def test_the_screening_line_reads_as_sentences():
    from r6.caregaps.evaluate import evaluate_care_gaps
    from r6.caregaps.report import build_consumer_summary
    patient = {"resourceType": "Patient", "gender": "female",
               "birthDate": "1990-01-01"}
    lines = build_consumer_summary(evaluate_care_gaps(
        patient, as_of="2026-10-06"))["lines"]
    cervical = next(line for line in lines
                    if line["rule_id"] == "cervical-screening")
    assert cervical["message"] == (
        "You may be due for a cervical cancer screening (Pap test, every 3 "
        "years). We didn't find one in your records. You may already have "
        "had it elsewhere, so check with your doctor.")
    for line in lines:
        assert "clinician" not in line["message"]
        assert " — " not in line["message"]


def test_the_hub_and_sign_in_read_plainly(app, svc, monkeypatch):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")
    hub = _visible(c.get("/home").get_data(as_text=True))
    assert "Reads Sample records" not in hub
    assert "CareAgents keeps only your account, not your health records." \
        in " ".join(hub.split())
    assert ("We keep a list of when your records were looked at, with no "
            "health details. Deleting your records doesn't delete that "
            "list.") in " ".join(hub.split())
    auth = (ROOT / "careagents" / "templates" / "auth.html").read_text()
    assert "can't be phished" not in auth
    assert "can't be stolen by a fake site" in auth
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    assert '"ready."' not in js


def test_a_failed_turn_says_so_without_an_emoji():
    from careagents.agent import GENERIC_FAILURE_TEXT
    assert GENERIC_FAILURE_TEXT == (
        "Something went wrong on our side. Try asking again.")
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert "⚠️" not in js


def test_the_delete_confirmation_is_spaced():
    css = (ROOT / "careagents" / "static" / "careagents.css").read_text()
    assert ".modal-card .delete-warn + .field-label { margin-top: 20px; }" in css


def test_no_tester_screen_uses_the_jargon(app, svc, monkeypatch):  # noqa: F811
    """The hub, chat and brief, signed in on sample records."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    started = c.post("/api/connections/sample").get_json()
    agent = started["agent_id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: _brief())
    for path in ("/home", f"/chat?agent={agent}", f"/brief?agent={agent}"):
        page = _visible(c.get(path).get_data(as_text=True))
        for word in JARGON:
            assert word.lower() not in page.lower(), (path, word)
