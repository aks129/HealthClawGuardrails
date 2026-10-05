"""After Approve, failure, delete and disconnect, say what happened (#847).

Patient-tester walks at 375px found the journey going quiet at its ends: an
approval with no route to its result, a failed request vanishing from the
hub, a delete with no count, a disconnect with no confirmation, and a chat
review card that a reload removed. Each test here pins one of those states
to a sentence a person can act on.

CareAgents stores nothing new for any of it. The hub's "done" and "didn't
finish" lines are the engine's answer, read on every visit, like the
pending list beside them.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from careagents.healthclaw import HealthClawError
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- the engine: recently finished requests --------------------------------

def _propose(client, tenant_headers, kind="sms", to="Dr. Smith"):
    r = client.post("/r6/actions/propose", json={
        "kind": kind, "payload": {"to": to, "phone": "617-555-0100",
                                  "body": "Reminder."}}, headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["id"]


def _set(app, action_id, **fields):
    from models import db
    from r6.actions.models import ProposedAction
    with app.app_context():
        ProposedAction.query.filter_by(id=action_id).update(
            fields, synchronize_session=False)
        db.session.commit()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_the_engine_lists_recently_finished_requests(
        client, app, tenant_headers, other_tenant_headers):
    """A finished request stays visible after its 30-minute proposal window:
    the window is on `updated_at`, never `expires_at`."""
    done = _propose(client, tenant_headers, to="Done")
    failed = _propose(client, tenant_headers, to="Failed")
    waiting = _propose(client, tenant_headers, to="Waiting")
    old = _propose(client, tenant_headers, to="Old")
    long_ago = _now() - timedelta(hours=3)
    _set(app, done, status="completed", expires_at=long_ago,
         outcome_summary=json.dumps({"delivery_link": "https://x/pdf"}))
    _set(app, failed, status="failed", expires_at=long_ago)
    _set(app, waiting, status="awaiting_confirmation")
    _set(app, old, status="completed", updated_at=_now() - timedelta(days=30))

    r = client.get("/r6/actions?status=recent", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    got = {a["to"]: a["status"] for a in body["actions"]}
    assert got == {"Done": "completed", "Failed": "failed"}
    # Summaries only: no payload, no outcome, nothing a list should carry.
    for a in body["actions"]:
        assert set(a) == {"id", "kind", "to", "status", "expires_at",
                          "updated_at"}
    assert client.get("/r6/actions?status=recent",
                      headers=other_tenant_headers).get_json()["count"] == 0
    # The pending list is unchanged, and other statuses still refuse.
    assert client.get("/r6/actions?status=completed",
                      headers=tenant_headers).status_code == 400


def test_the_recent_list_persists_nothing(client, app, tenant_headers):
    from models import db
    from r6.models import AuditEventRecord
    action_id = _propose(client, tenant_headers)
    _set(app, action_id, status="failed")
    with app.app_context():
        before = AuditEventRecord.query.count()
    client.get("/r6/actions?status=recent", headers=tenant_headers)
    with app.app_context():
        db.session.rollback()
        assert AuditEventRecord.query.count() == before


# --- the CareAgents side, faked engine --------------------------------------

class OutcomeClient(FakeClient):
    """A FakeClient whose tenants have finished requests to report."""

    def __init__(self, recent=None, recent_error=False, pending=None):
        super().__init__()
        self._recent = recent if recent is not None else []
        self._recent_error = recent_error
        self._pending = pending
        self.status_calls = []

    def pending_actions(self, tenant):
        if self._pending is not None:
            return list(self._pending)
        return super().pending_actions(tenant)

    def recent_actions(self, tenant):
        if self._recent_error:
            raise HealthClawError("recent unavailable", 503)
        return list(self._recent)

    def action_status(self, tenant, action_id):
        self.status_calls.append(action_id)
        for a in self._recent:
            if a["id"] == action_id:
                return {**a, "outcome_summary": a.get("_outcome", "")}
        return super().action_status(tenant, action_id)


def _app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    started = c.post("/api/connections/sample").get_json()
    return c, started["agent_id"], started["id"]


def _link(hours_left: float) -> str:
    exp = int(_now().replace(tzinfo=timezone.utc).timestamp()
              + hours_left * 3600)
    return f"https://hc.example/r6/sdc/documents/d1?t=x&exp={exp}&sig=s"


def test_a_finished_form_is_reported_done_with_its_pdf_link(
        cfg, svc, monkeypatch):
    # Built once: two _link(20) calls straddling a second boundary differ
    # in `exp` by one, which failed CI on Postgres (#856).
    link = _link(20)
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-done", "kind": "form-fill", "to": None,
        "status": "completed",
        "_outcome": json.dumps({"delivery_link": link})}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    body = c.get("/api/approvals/count").get_json()
    assert body["count"] == 0
    [item] = body["recent"]
    assert item["label"] == "Intake form"
    assert item["state"] == "done"
    assert item["link"] == link
    assert item["agent_name"] and item["chat"] == f"/chat?agent={agent}"


def test_an_expired_pdf_link_is_not_offered(cfg, svc, monkeypatch):
    """The signed link lasts a day; a dead link is worse than none."""
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-done", "kind": "form-fill", "to": None,
        "status": "completed",
        "_outcome": json.dumps({"delivery_link": _link(-1)})}])
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    [item] = c.get("/api/approvals/count").get_json()["recent"]
    assert item["state"] == "done"
    assert "link" not in item


def test_a_non_http_link_is_never_offered(cfg, svc, monkeypatch):
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-done", "kind": "form-fill", "to": None,
        "status": "completed",
        "_outcome": json.dumps({"delivery_link": "javascript:alert(1)"})}])
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    [item] = c.get("/api/approvals/count").get_json()["recent"]
    assert "link" not in item


@pytest.mark.parametrize("status,state", [
    ("failed", "failed"), ("needs_review", "needs_review"),
    ("unknown", "unknown"), ("executing", "in_progress")])
def test_a_request_that_did_not_finish_stays_on_the_hub(
        cfg, svc, monkeypatch, status, state):
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-x", "kind": "sms", "to": "Dr. Lee", "status": status}])
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    body = c.get("/api/approvals/count").get_json()
    [item] = body["recent"]
    assert item["state"] == state
    assert item["label"] == "Text message"
    assert "link" not in item
    # Only a completed form-fill has a result to look up.
    assert fake.status_calls == []


def test_a_recent_lookup_that_fails_is_said_and_never_hides_the_count(
        cfg, svc, monkeypatch):
    fake = OutcomeClient(recent_error=True)
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    r = c.get("/api/approvals/count")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] >= 1
    assert body["recent_unavailable"] is True
    assert "recent" not in body


def test_the_hub_script_renders_done_and_didnt_finish_with_a_next_step():
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    for words in ("Done", "Didn't finish", "Open the PDF",
                  "to try again", "Couldn't check what happened"):
        assert words in js, words


# --- the review page: plain words and a route to the result ----------------

def _page(name):
    import re
    t = ROOT / "templates"
    raw = (t / name).read_text(encoding="utf-8")
    return re.sub(r'{%\s*include\s+"([^"]+)"\s*%}',
                  lambda m: (t / m.group(1)).read_text(encoding="utf-8"), raw)


def test_the_next_step_after_approve_is_plain_words():
    from r6.actions import review
    for s in (review._FORM_NEXT_STEP, review._APPROVE_NEXT_STEP):
        assert "claimed" not in s and "executed" not in s, s
        assert "action's own status" not in s, s


@pytest.mark.parametrize("name", ["action_review.html", "action_approve.html"])
def test_an_approval_that_worked_goes_on_to_its_result(name):
    """The success branch printed next_step and stopped: no link, no outcome.
    It now reads the status, whose completed arm offers the PDF."""
    page = _page(name)
    ok = page.index("if (res.r.ok)")
    branch = page[ok:page.index("return;", ok)]
    assert "checkStatus()" in branch, branch
    poll = page[page.index("function checkStatus()"):]
    done = poll[poll.index("status === 'completed'"):]
    done = done[:done.index("return;")]
    assert "delivery_link" in done
    assert "Open your form (PDF)" in done
    assert "/^https?:\\/\\//" in done, "only an http(s) link becomes a link"


@pytest.mark.parametrize("name", ["action_review.html", "action_approve.html"])
def test_a_failed_request_names_the_next_step(name):
    poll = _page(name)
    poll = poll[poll.index("function checkStatus()"):]
    arm = poll[poll.index("status === 'failed'"):]
    arm = arm[:arm.index("return;")]
    assert "Ask your assistant to try again" in arm, arm


def test_the_review_page_speaks_to_the_person_not_about_the_patient():
    page = (ROOT / "templates" / "action_review.html").read_text()
    visible = page.split("{% block scripts %}")[0]
    import re
    visible = re.sub(r"<!--.*?-->|{#.*?#}", "", visible, flags=re.S)
    assert "the patient" not in visible
    assert "patient's" not in visible
    assert "(patient confirmed)" not in visible
    assert "affirmatively" not in visible
    # The attestation itself is untouched: never pre-checked, same field.
    assert 'name="nka" id="nka" value="true">' in page


def test_the_nka_refusals_quote_the_label_the_page_shows(
        client, app, tenant_headers):
    from r6.actions import review
    import inspect
    src = inspect.getsource(review.review_submit)
    assert "I have no known allergies" in src
    assert "(patient confirmed)" not in src


# --- records delete: the count the consent box promises --------------------

class PurgeClient(FakeClient):
    def __init__(self, detail):
        super().__init__()
        self._detail = detail

    def purge_tenant(self, tenant):
        super().purge_tenant(tenant)
        return {"deleted": True, "rows_deleted": 999, "detail": self._detail}


def test_records_delete_uses_the_count_the_card_showed(
        cfg, svc, monkeypatch):
    """The patient tester's card said 9 records and the message said 15
    stored items. The count is the one the card showed, never the engine's
    count of every resource it removed."""
    fake = PurgeClient({"resources": 15, "action_events": 7})
    c, _, conn = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    svc.mark_synced(conn, 9)
    body = c.delete(f"/api/connections/{conn}").get_json()
    assert body["deleted"] is True
    assert body["records_deleted"] == 9
    assert body["message"].startswith("All 9 records in Sample records "
                                      "were deleted.")
    assert "15" not in body["message"]
    assert "PHI" not in body["message"]


def test_records_delete_without_a_count_does_not_invent_one(
        cfg, svc, monkeypatch):
    fake = PurgeClient({"resources": 15})
    c, _, conn = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    with svc.session() as s:
        from careagents.models import Connection
        s.query(Connection).filter_by(id=conn).update({"last_count": None})
    body = c.delete(f"/api/connections/{conn}").get_json()
    assert body["deleted"] is True
    assert body["records_deleted"] is None
    assert body["message"].startswith("Your records in Sample records were "
                                      "deleted.")
    assert "15" not in body["message"]


def test_one_record_is_singular(cfg, svc, monkeypatch):
    c, _, conn = _signed_in(_app(cfg, svc, PurgeClient({})), svc, monkeypatch)
    svc.mark_synced(conn, 1)
    msg = c.delete(f"/api/connections/{conn}").get_json()["message"]
    assert msg.startswith("The 1 record in Sample records was deleted.")


def test_records_delete_says_the_assistant_went_with_them(
        cfg, svc, monkeypatch):
    """Deleting a connection deletes the assistants that read it. The
    patient tester found the assistant gone with nothing saying so."""
    c, _, conn = _signed_in(_app(cfg, svc, PurgeClient({})), svc, monkeypatch)
    body = c.delete(f"/api/connections/{conn}").get_json()
    assert body["assistants_deleted"] == ["Juniper"]
    assert "Juniper was deleted too" in body["message"]


def test_the_delete_dialog_names_the_assistant_that_goes_with_it(
        cfg, svc, monkeypatch):
    c, _, conn = _signed_in(_app(cfg, svc, FakeClient()), svc, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    btn = html[html.index(f'class="conn-delete" data-conn="{conn}"'):]
    btn = btn[:btn.index(">")]
    assert 'data-readers="Juniper"' in btn
    assert 'id="delete-readers"' in html
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    ask = js[js.index("function askToDelete"):]
    ask = ask[:ask.index("return dlg.result;")]
    assert "reads these records and will be deleted too" in ask


# --- disconnect: ask first, then say what happened -------------------------

def test_disconnect_answers_with_a_sentence(cfg, svc, monkeypatch):
    fake = FakeClient()
    c, _, conn = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    body = c.post(f"/api/connections/{conn}/disconnect").get_json()
    assert body["status"] == "revoked"
    assert "stay" in body["message"] and "Disconnected" in body["message"]


def test_the_hub_asks_before_disconnecting_and_carries_the_result_over():
    html = (ROOT / "careagents" / "templates" / "home.html").read_text()
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    assert 'id="disconnect-modal"' in html
    assert 'id="hub-notice"' in html
    dis = js[js.index('querySelectorAll(".conn-disconnect")'):]
    dis = dis[:dis.index("});\n  });")]
    # The dialog resolves before any request goes out.
    assert dis.index("askToDisconnect") < dis.index("/disconnect")
    assert "carryNotice(" in dis
    dele = js[js.index('querySelectorAll(".conn-delete")'):]
    dele = dele[:dele.index("});\n  });")]
    assert "carryNotice(" in dele


def test_disconnect_names_the_assistant_that_reads_those_records(
        cfg, svc, monkeypatch):
    """A revoked connection is not a pathway to its requests (#215), so an
    assistant reading it can still chat but nothing it prepares can be
    approved. The dialog says so before the tap, not after."""
    app = _app(cfg, svc, FakeClient())
    c, agent, _ = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    other = svc.add_connection(account_id, "fasten", "tenant-other", "Clinic")
    assert c.post(f"/api/agents/{agent}/connection",
                  json={"connection_id": other}).status_code == 200
    html = c.get("/home").get_data(as_text=True)
    btn = html[html.index(f'class="conn-disconnect" data-conn="{other}"'):]
    btn = btn[:btn.index(">")]
    assert 'data-readers="Juniper"' in btn
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    ask = js[js.index("function askToDisconnect"):]
    ask = ask[:ask.index("return dlg.result;")]
    assert "can't be approved" in ask


# --- chat: the review card survives a reload --------------------------------

def test_a_pending_review_is_rendered_on_reload(cfg, svc, monkeypatch):
    fake = OutcomeClient(pending=[{"id": "act-9", "kind": "form-fill",
                                   "to": None,
                                   "status": "awaiting_confirmation"}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert 'data-pending-reviews=' in html
    assert "act-9" in html


def test_a_pending_lookup_that_fails_still_renders_the_chat(
        cfg, svc, monkeypatch):
    class Down(FakeClient):
        def pending_actions(self, tenant):
            raise HealthClawError("down", 503)
    c, agent, _ = _signed_in(_app(cfg, svc, Down()), svc, monkeypatch)
    r = c.get(f"/chat?agent={agent}")
    assert r.status_code == 200
    assert "act-" not in r.get_data(as_text=True)


def test_chat_js_draws_the_cards_it_was_given_on_load():
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    assert "pendingReviews" in js
    assert "addReviewCard(" in js[js.index("pendingReviews"):]


def test_only_the_intake_form_card_waits_for_a_pdf():
    """A card for any other request has no PDF coming; polling for one ran
    every four seconds until the page closed."""
    js = (ROOT / "careagents" / "static" / "chat.js").read_text()
    card = js[js.index("function addReviewCard"):]
    card = card[:card.index("\n  }\n")]
    assert "if (!label) watchForm(actionId);" in card


# --- copy -------------------------------------------------------------------

def test_a_new_account_is_not_welcomed_back(cfg, svc, monkeypatch):
    c = _app(cfg, svc, FakeClient()).test_client()
    _login(c, svc, monkeypatch)
    assert "Welcome back" not in c.get("/home").get_data(as_text=True)


def test_visit_brief_and_smart_health_link_are_explained(cfg, svc, monkeypatch):
    from careagents import connectors
    fake = FakeClient()
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    assert "summary of your records to bring to an appointment" in html
    shl = connectors._BY_ID["shl"]
    assert "QR code" in shl["blurb"] and "SMART Health Link" in shl["blurb"]


def test_the_account_delete_box_says_it_cannot_be_undone(cfg, svc, monkeypatch):
    c = _app(cfg, svc, FakeClient()).test_client()
    _login(c, svc, monkeypatch)
    html = c.get("/settings").get_data(as_text=True)
    box = html[html.index('id="delete-modal"'):]
    box = box[:box.index("</div>\n</div>")]
    assert "can't be undone" in box


def test_the_orphan_queue_names_the_real_step(cfg, svc, monkeypatch):
    """Two connections, one assistant: the other connection's requests are
    reached by switching that assistant, not by a chat that cannot see
    them (QA on #843)."""
    fake = FakeClient()
    app = _app(cfg, svc, fake)
    c, agent, sample = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    other = svc.add_connection(account_id, "fasten", "tenant-other", "Clinic")
    # The assistant reads the other connection; the sample's queue is orphaned.
    assert c.post(f"/api/agents/{agent}/connection",
                  json={"connection_id": other}).status_code == 200
    body = c.get("/api/approvals/count").get_json()
    orphan = [q for q in body["queues"] if not q.get("href")]
    assert orphan and orphan[0]["has_assistant"] is True


def test_a_failure_on_records_no_assistant_reads_names_the_switch(
        cfg, svc, monkeypatch):
    """The orphan-queue rule, for a failed request: with an assistant on
    other records, "Start a chat" is the wrong step (found in the #847
    walk)."""
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-x", "kind": "sms", "to": "Dr. Lee", "status": "failed"}])
    app = _app(cfg, svc, fake)
    c, agent, _ = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    other = svc.add_connection(account_id, "fasten", "tenant-other", "Clinic")
    assert c.post(f"/api/agents/{agent}/connection",
                  json={"connection_id": other}).status_code == 200
    items = c.get("/api/approvals/count").get_json()["recent"]
    orphan = [i for i in items if "chat" not in i]
    assert orphan, items
    assert orphan[0]["has_assistant"] is True
    assert orphan[0]["records"] == "Sample records"
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    assert "switch your assistant to" in js[js.index("const recentLine"):]


# --- #853 review round -------------------------------------------------------

def test_a_form_is_not_said_to_have_gone_anywhere(cfg, svc, monkeypatch):
    """Only a PDF was made. "Intake form to Intake portal" read as though it
    had been sent (patient tester on #853)."""
    fake = OutcomeClient(pending=[], recent=[{
        "id": "act-done", "kind": "form-fill", "to": "Intake portal",
        "status": "completed",
        "_outcome": json.dumps({"delivery_link": _link(20)})}])
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    [item] = c.get("/api/approvals/count").get_json()["recent"]
    assert "to" not in item
    js = (ROOT / "careagents" / "static" / "home.js").read_text()
    line = js[js.index("const recentLine"):js.index("function showRecent")]
    assert '(r.link ? "ready." : "Done.")' in line


@pytest.mark.parametrize("name", ["action_review.html", "action_approve.html"])
def test_a_ready_form_says_what_to_do_with_it(name):
    poll = _page(name)
    poll = poll[poll.index("function checkStatus()"):]
    done = poll[poll.index("status === 'completed'"):]
    done = done[:done.index("return;")]
    assert "Your form is ready." in done
    assert "to save it, print it or send it to your new doctor." in done


@pytest.mark.parametrize("name", ["action_review.html", "action_approve.html"])
def test_approve_and_decline_go_away_once_approved(name):
    page = _page(name)
    ok = page.index("if (res.r.ok)")
    branch = page[ok:page.index("return;", ok)]
    assert "btn.hidden = true;" in branch
    assert "document.getElementById('decline-btn').hidden = true;" in branch


def test_the_review_shell_styles_a_disabled_button_and_honours_hidden():
    shell = (ROOT / "templates" / "review_base.html").read_text()
    assert ".btn:disabled" in shell
    assert "[hidden] { display: none !important; }" in shell


def test_the_review_page_names_the_box_the_person_sees():
    page = (ROOT / "templates" / "action_review.html").read_text()
    assert '"No known allergies"' not in page
    assert page.count('"I have no known allergies"') >= 2


def test_the_shared_link_tile_does_not_promise_what_is_coming_soon():
    from careagents import connectors
    shl = connectors._BY_ID["shl"]
    assert shl["tier"] == "soon"
    assert "Open it here" not in shl["blurb"]
    assert "coming soon" in shl["blurb"].lower()


def test_the_hub_lists_at_most_five_recent_requests(cfg, svc, monkeypatch):
    recent = [{"id": f"act-{i}", "kind": "sms", "to": "Dr. Lee",
               "status": "failed",
               "updated_at": f"2026-10-01T10:0{i}:00Z"} for i in range(8)]
    fake = OutcomeClient(pending=[], recent=recent)
    c, _, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    items = c.get("/api/approvals/count").get_json()["recent"]
    assert [i["id"] for i in items] == [f"act-{i}" for i in (7, 6, 5, 4, 3)]


_NOTICE_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const key = src.match(/const NOTICE_KEY = [^;]+;/)[0];
const block = src.match(
  /\(function showCarriedNotice\(\) \{[\s\S]*?\n  \}\)\(\);/)[0];
const store = {};
const storage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const shown = [];
const el = { scrollIntoView() {} };
const run = new Function('sessionStorage', '$', 'announce',
  key + '\nsessionStorage.setItem(NOTICE_KEY, "Disconnected.");\n'
  + block + '\n' + block);
run(storage, () => el, (_el, text) => { shown.push(text); });
process.stdout.write(JSON.stringify({ shown, left: Object.keys(store) }));
"""


def test_a_carried_notice_is_shown_once_and_then_cleared():
    """A second load must not repeat "Disconnected." forever (QA on #853)."""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _NOTICE_HARNESS, "--",
         str(ROOT / "careagents" / "static" / "home.js")],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"shown": ["Disconnected."], "left": []}
