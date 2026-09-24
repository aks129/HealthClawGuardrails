"""scripts/beta_acceptance.py rows 7–12 (#677), driven against the real chain.

The script talks HTTP to a CareAgents origin. Here that origin is the
CareAgents Flask app, whose HealthClaw client is relayed onto the real
engine's WSGI (the pattern tests/test_pending_approvals.py uses), so each row
asserts what the product actually answers — not what a fake says it would.

The one thing not driven is the model. Rows 9–10 start from the review card
a chat turn returns; the form is proposed here the way the agent's
`start_intake_form` tool proposes it (`HealthClawClient.start_form_action`),
and the row functions take its id from there.
"""
import importlib.util
import json
from pathlib import Path

import pytest
import requests as _requests

from tests.test_careagents import _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "beta_acceptance.py"
_spec = importlib.util.spec_from_file_location("beta_acceptance", _SCRIPT)
ba = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ba)

BASE = "http://careagents"
TENANT = "ca-acceptance"
#: A test-only shared value for the approval-token mint; not a credential.
_MINT = "test-mint-" + "value"
ALLERGY = {"resourceType": "AllergyIntolerance", "id": "acc-allergy-1",
           "patient": {"reference": "Patient/demo-patient-rivera"},
           "clinicalStatus": {"coding": [{"code": "active"}]},
           "code": {"coding": [{"system": "http://snomed.info/sct",
                                "code": "91936005"}]}}


def _to_requests(resp):
    r = _requests.Response()
    r.status_code = resp.status_code
    r._content = resp.get_data() or b""
    r.headers.update(resp.headers.to_wsgi_list())
    return r


class _Browser:
    """A requests-shaped session over a Flask test client — what the script
    holds when it talks to a real origin."""

    def __init__(self, client, base):
        self.client, self.base = client, base

    def _path(self, url):
        return url.replace(self.base, "", 1)

    def get(self, url, params=None, timeout=None, allow_redirects=True, **_):
        return _to_requests(self.client.get(self._path(url),
                                            query_string=params or {},
                                            follow_redirects=allow_redirects))

    def post(self, url, json=None, timeout=None, stream=False, **_):
        return _to_requests(self.client.post(self._path(url), json=json))

    def delete(self, url, timeout=None, **_):
        return _to_requests(self.client.delete(self._path(url)))


class Chain:
    """Engine + CareAgents + a signed-in person with the sample source and
    an agent — the state the script reaches by row 6."""

    def __init__(self, cfg, svc, monkeypatch, *, allergy=False):
        from careagents.app import create_app
        from careagents.healthclaw import HealthClawClient
        from main import create_app as engine_create_app
        from models import db
        from r6.models import R6Resource

        monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{TENANT}")
        monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
        monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", _MINT)
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://engine")
        self.engine_app = engine_create_app({
            "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "LEGACY_BOOT_ON_CREATE": False})
        with self.engine_app.app_context():
            db.create_all()
            from r6.actions.registry import _clear
            from r6.actions.rails import register_all
            _clear()
            register_all()
        engine = self.engine_app.test_client()
        self.engine = engine

        class _Relay:
            def post(self, url, json=None, headers=None, timeout=None, data=None):
                return _to_requests(engine.post(url.replace("http://engine", ""),
                                                json=json, data=data,
                                                headers=headers or {}))

            def get(self, url, params=None, headers=None, timeout=None):
                # A signed download link carries its own query string.
                extra = {"query_string": params} if params else {}
                return _to_requests(engine.get(url.replace("http://engine", ""),
                                               headers=headers or {}, **extra))

        self.relay = _Relay()
        self.hc = HealthClawClient(base="http://engine", mint_secret=_MINT)
        self.hc.http = self.relay
        self.hc.new_tenant_id = lambda: TENANT
        self.app = create_app(config=cfg, client=self.hc, accounts=svc)
        self.app.config["TESTING"] = True
        self.svc, self.monkeypatch = svc, monkeypatch
        self.s = self.browser()
        assert self.s is not None
        r = self.s.post(f"{BASE}/api/connections/sample")
        assert r.status_code == 200, r.text
        self.conn = r.json()["id"]
        if allergy:
            with self.engine_app.app_context():
                db.session.add(R6Resource("AllergyIntolerance",
                                          json.dumps(ALLERGY),
                                          resource_id=ALLERGY["id"],
                                          tenant_id=TENANT))
                db.session.commit()
        r = self.s.post(f"{BASE}/api/agents", json={
            "name": "Acceptance", "persona": "calm", "connection_id": self.conn})
        assert r.status_code == 200, r.text
        self.agent = r.json()["id"]

    def browser(self):
        c = self.app.test_client()
        _login(c, self.svc, self.monkeypatch, email="acceptance@example.com")
        return _Browser(c, BASE)

    def start_form(self):
        return self.hc.start_form_action(TENANT)


@pytest.fixture
def run():
    return ba.Run(None)


def _only(run):
    assert len(run.steps) == 1, run.steps
    return run.steps[0]


# --- row 7 -------------------------------------------------------------------

def test_smbp_row_is_unavailable_and_says_why(run):
    assert ba.row_smbp(run) is False
    step = _only(run)
    assert step["status"] == "UNAVAILABLE"
    assert "step-up" in step["detail"] and "/r6/smbp" in step["detail"]


# --- row 8 -------------------------------------------------------------------

def test_care_gaps_brief_row_reports_the_section_it_is_shown(cfg, svc,
                                                             monkeypatch, run):
    """Against the real engine the brief's screening review does not run: it
    resolves no patient (the stopgap in r6/brief/routes.py) and says so. The
    row records that as UNAVAILABLE — not a pass, and not "nothing due".
    When the brief learns its subject, this becomes a PASS with sourced
    items, and this test is the one to update."""
    chain = Chain(cfg, svc, monkeypatch)
    assert ba.row_care_gaps(chain.s, BASE, chain.agent, run) is False
    step = _only(run)
    assert step["status"] == "UNAVAILABLE"
    assert "did not run" in step["detail"]


def _page_session(page):
    class _S:
        def get(self, url, **_):
            r = _requests.Response()
            r.status_code, r._content = 200, page.encode()
            return r
    return _S()


def test_care_gaps_brief_row_passes_sourced_items_and_an_honest_none(run):
    due = ('<h2>Preventive care due</h2><div class="brief-section">'
           '<div class="brief-field"><span class="brief-label">Screening</span>'
           '<span class="brief-source">from Observation '
           '<span class="brief-source-id">o1</span></span></div></div>'
           '<h2>Recent and upcoming visits</h2>')
    assert ba.row_care_gaps(_page_session(due), BASE, "a", run)
    assert run.steps[-1]["due_items"] == 1
    none = ('<h2>Preventive care due</h2><p class="brief-empty">We found no '
            'preventive care items based on your current records.</p>')
    assert ba.row_care_gaps(_page_session(none), BASE, "a", run)


def test_care_gaps_chat_row_needs_the_tool_and_the_word(run):
    def turn(tools, text):
        events = [{"type": "tool", "name": t} for t in tools]
        return _ChatSession(_Stream(200, events + [{"type": "text", "text": text}]))
    assert ba.row_care_gaps_chat(turn(["get_care_gaps"], "Two screenings are due."),
                                 BASE, "a", run, 1)
    assert not ba.row_care_gaps_chat(turn([], "Two screenings are due."),
                                     BASE, "a", run, 1)
    assert not ba.row_care_gaps_chat(turn(["get_care_gaps"], "All good."),
                                     BASE, "a", run, 1)
    assert not ba.row_care_gaps_chat(_ChatSession(_Stream(429, text="limit")),
                                     BASE, "a", run, 1)
    assert [s["status"] for s in run.steps] == ["PASS", "FAIL", "FAIL",
                                                "UNAVAILABLE"]


def test_care_gaps_row_never_passes_a_review_that_did_not_run(cfg, svc,
                                                                monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    from careagents.healthclaw import HealthClawError

    def outage(tenant):
        raise HealthClawError("down", 503)
    monkeypatch.setattr(chain.hc, "fetch_appointment_brief", outage)
    assert ba.row_care_gaps(chain.s, BASE, chain.agent, run) is False
    assert _only(run)["status"] == "UNAVAILABLE"


def test_care_gaps_row_fails_an_item_with_no_source(run):
    page = ('<h2>Preventive care due</h2><div class="brief-section">'
            '<div class="brief-field"><span class="brief-label">Screening</span>'
            '</div></div><h2>Recent and upcoming visits</h2>')
    assert ba.row_care_gaps(_page_session(page), BASE, "a", run) is False
    assert _only(run)["status"] == "FAIL"


# --- rows 9 and 10 -----------------------------------------------------------

def test_form_rows_review_approve_and_deliver_the_pdf(cfg, svc, monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    action_id = chain.start_form()

    page = ba.row_form_review(chain.s, BASE, chain.agent, action_id, run)
    assert page is not None, run.steps
    assert page["meds"] >= 1 and page["allergies"] == 1
    assert run.steps[-1]["status"] == "PASS"

    asked = []
    decisions = ba.row_form_approve(
        chain.s, BASE, chain.agent, action_id, page, run,
        attest_nka=lambda: asked.append(1) or True, fetch=chain.relay.get)
    assert run.steps[-1]["status"] == "PASS", run.steps[-1]
    # An allergy was on file and confirmed; nobody was asked to attest NKA.
    assert asked == [] and "nka" not in decisions
    assert decisions["allergy-0"] == "confirm"
    assert run.steps[-1]["medications_in_pdf"] == page["meds"]


def test_form_approve_never_attests_no_known_allergies_itself(cfg, svc,
                                                                monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)            # sample: no allergy on file
    action_id = chain.start_form()
    page = ba.row_form_review(chain.s, BASE, chain.agent, action_id, run)
    assert page is not None and page["allergies"] == 0, run.steps

    for nobody in (None, lambda: False):
        assert ba.row_form_approve(chain.s, BASE, chain.agent, action_id, page,
                                   run, attest_nka=nobody) is None
        assert run.steps[-1]["status"] == "UNAVAILABLE"
        assert "never ticks it" in run.steps[-1]["detail"]
    # Nothing was submitted: the form is still waiting for the person.
    status = chain.s.get(f"{BASE}/api/form/{action_id}",
                         params={"agent": chain.agent}).json()
    assert status["status"] == "awaiting_confirmation"


def test_form_approve_with_a_person_attesting_nka_delivers(cfg, svc,
                                                            monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    action_id = chain.start_form()
    page = ba.row_form_review(chain.s, BASE, chain.agent, action_id, run)
    decisions = ba.row_form_approve(chain.s, BASE, chain.agent, action_id, page,
                                    run, attest_nka=lambda: True,
                                    fetch=chain.relay.get)
    assert decisions["nka"] == "true"
    assert run.steps[-1]["status"] == "PASS", run.steps[-1]


def test_form_review_fails_when_the_gate_accepts_silence(run):
    """If the server ever took a submit with no allergy answer, the row
    must fail rather than read the acceptance as progress."""
    review = ('<div class="med-row x" data-row-index="0">\n<div class="me-3">\n'
              '<div class="fw-bold">Med</div></div>'
              '<input type="radio" name="med-0" value="yes"></div>'
              '<input class="form-check-input" type="checkbox" name="nka" id="nka" value="true">')

    class _S:
        def get(self, url, **_):
            r = _requests.Response()
            r.status_code = 200
            r._content = (f"/review/a/x {review}").encode()
            return r

        def post(self, url, **_):
            r = _requests.Response()
            r.status_code, r._content = 200, b"{}"
            return r
    assert ba.row_form_review(_S(), BASE, "a", "x", run) is None
    assert run.steps[-1]["status"] == "FAIL"
    assert "not 422" in run.steps[-1]["detail"]


def test_form_review_fails_a_prechecked_nka_box(run):
    review = ('/review/a/x <input type="radio" name="med-0" value="yes">'
              '<input type="checkbox" name="nka" id="nka" value="true" checked>')

    class _S:
        def get(self, url, **_):
            r = _requests.Response()
            r.status_code, r._content = 200, review.encode()
            return r
    assert ba.row_form_review(_S(), BASE, "a", "x", run) is None
    assert "pre-ticked" in run.steps[-1]["detail"]


# --- row 11 ------------------------------------------------------------------

def test_restart_row_finds_the_outcome_and_refuses_a_second_submit(
        cfg, svc, monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    action_id = chain.start_form()
    page = ba.row_form_review(chain.s, BASE, chain.agent, action_id, run)
    decisions = ba.row_form_approve(chain.s, BASE, chain.agent, action_id, page,
                                    run, fetch=chain.relay.get)
    assert run.steps[-1]["status"] == "PASS", run.steps[-1]

    run.steps.clear()
    assert ba.row_restart(chain.browser, BASE, chain.conn, chain.agent,
                          action_id, decisions, None, run) is not None
    by = {s["step"]: s for s in run.steps}
    assert by["restart: signed in again"]["status"] == "PASS"
    # The sample source says there is nothing to re-pull; recorded as that.
    assert by["reconnect"]["status"] == "UNAVAILABLE"
    assert by["restart: earlier turn still there"]["status"] == "UNAVAILABLE"
    retry = by["retry: form not duplicated"]
    assert retry["status"] == "PASS", retry
    assert "HTTP 404" in retry["detail"] or "HTTP 409" in retry["detail"]


def test_restart_row_says_an_unsubmitted_form_is_still_waiting(cfg, svc,
                                                                monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    action_id = chain.start_form()
    ba.row_restart(chain.browser, BASE, chain.conn, chain.agent, action_id,
                   None, None, run)
    retry = {s["step"]: s for s in run.steps}["retry: form not duplicated"]
    assert retry["status"] == "PASS"
    assert "awaiting_confirmation" in retry["detail"]


def test_restart_row_does_not_read_an_outage_as_still_waiting(cfg, svc,
                                                               monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    action_id = chain.start_form()
    from careagents.healthclaw import HealthClawError

    def down(tenant, action_id):
        raise HealthClawError("down", 503)
    monkeypatch.setattr(chain.hc, "action_status", down)
    ba.row_restart(chain.browser, BASE, chain.conn, chain.agent, action_id,
                   None, None, run)
    retry = {s["step"]: s for s in run.steps}["retry: form not duplicated"]
    assert retry["status"] == "UNAVAILABLE"


# --- row 12 ------------------------------------------------------------------

def test_delete_row_states_scope_and_observes_it_gone(cfg, svc, monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    action_id = chain.start_form()
    assert ba.row_delete(chain.s, BASE, chain.conn, chain.agent, action_id, run)
    deleted, gone = run.steps
    assert deleted["status"] == "PASS" and deleted["rows_deleted"] > 0
    assert gone["status"] == "PASS"
    assert gone["detail"] == ("hub gone, labs gone, second delete gone, "
                              "form gone")
    # And from the engine's side, which the script cannot reach: the
    # tenant's records are gone, not just unlinked.
    r = chain.engine.get("/r6/fhir/MedicationRequest",
                         headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 200 and not r.get_json().get("entry")


def test_delete_row_needs_the_hub_to_have_shown_the_connection(run):
    """'Not on the hub afterwards' only means something if it was there."""
    class _S:
        def get(self, url, **_):
            r = _requests.Response()
            r.status_code = 404 if "/api/" in url else 200
            r._content = b"<html>no connections here</html>"
            return r

        def delete(self, url, **_):
            r = _requests.Response()
            r.status_code = 200
            r._content = json.dumps({"deleted": True, "unlinked": True,
                                     "audit_retained": True,
                                     "rows_deleted": 3}).encode()
            r.headers["content-type"] = "application/json"
            return r
    ba.row_delete(_S(), BASE, "conn-1", "a", None, run)
    assert run.steps[-1]["status"] == "FAIL"
    assert "hub STILL THERE" in run.steps[-1]["detail"]


def test_delete_row_does_not_pass_an_unconfirmed_purge(cfg, svc, monkeypatch, run):
    chain = Chain(cfg, svc, monkeypatch)
    from careagents.healthclaw import HealthClawError

    def lost(tenant):
        raise HealthClawError("purge failed", 502)
    monkeypatch.setattr(chain.hc, "purge_tenant", lost)
    assert ba.row_delete(chain.s, BASE, chain.conn, chain.agent, None, run) is False
    assert _only(run)["status"] == "UNAVAILABLE"


# --- the chat helper ---------------------------------------------------------

class _Stream:
    def __init__(self, status, events=(), text=""):
        self.status_code, self.text = status, text
        self._lines = [f"data: {json.dumps(e)}" for e in events]

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class _ChatSession:
    def __init__(self, resp):
        self.resp = resp

    def post(self, url, **_):
        return self.resp


@pytest.mark.parametrize("resp, status", [
    (_Stream(429, text='{"error":"daily_limit_reached"}'), "UNAVAILABLE"),
    (_Stream(503, text='{"error":"run_workers_unavailable"}'), "UNAVAILABLE"),
    (_Stream(200, [{"type": "error", "text": (
        "I'm getting more requests than I can answer right now.")}]), "UNAVAILABLE"),
    (_Stream(200, [{"type": "error", "text": "Something went wrong."}]), "FAIL"),
    (_Stream(200, []), "FAIL"),
    (_Stream(500, text="boom"), "FAIL"),
])
def test_chat_turn_separates_the_environment_from_the_product(resp, status):
    assert ba.chat_turn(_ChatSession(resp), BASE, "a", "q", 1)["status"] == status


def test_chat_turn_returns_the_review_card():
    turn = ba.chat_turn(_ChatSession(_Stream(200, [
        {"type": "tool", "name": "start_intake_form"},
        {"type": "card", "kind": "review", "action_id": "act-1"},
        {"type": "text", "text": "I've prepared your form."}])), BASE, "a", "q", 1)
    assert turn["status"] == "PASS"
    assert [c["action_id"] for c in turn["cards"]] == ["act-1"]
