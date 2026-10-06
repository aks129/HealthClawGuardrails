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
    assert page.count("From your records") == 5
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
    assert field.value == "2026-09-01"


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
