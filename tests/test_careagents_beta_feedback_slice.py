"""The first beta-feedback slice: error pages, context on a report, and
"This wasn't right" on a chat answer, with counts for the digest.

What these pin, most of all, is that no PHI goes anywhere new:

- the 500 count is by endpoint name, never the URL, its query or the
  exception's text;
- a report's context is a fixed list of fields, each from a fixed set;
- a flag carries a run id, a reason code and a capped note, and a body
  with anything else (the answer, the question) is refused;
- nothing a tester types is kept in a table or written to a log.

Synthetic data only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import text as sql

from careagents import feedback, funnel
from careagents.models import Base
from tests.test_careagents_beta_signup import _visible
from tests.test_careagents_feedback import (  # noqa: F401 - fixtures
    _signed_in, made, sent)
from tests.test_careagents_funnel import _svc_cfg

MARKER = "okapi-marker-4417"
RUN = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"
_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAT_JS = _ROOT / "careagents" / "static" / "chat.js"


def _boom_app(made, **env):  # noqa: F811
    """An app with two routes that raise, one page and one JSON. The
    exception text carries the marker, which must go nowhere."""
    app, svc = made(**env)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom-test")
    def boom_test():
        raise RuntimeError(MARKER)

    @app.get("/api/boom-test")
    def api_boom_test():
        raise RuntimeError(MARKER)
    return app, svc


def _details(svc):
    with svc.session() as s:
        return sorted((r.day, r.kind, r.detail, r.count)
                      for r in s.query(funnel.IssueDetailDay).all())


def _all_rows(svc):
    with svc.engine.connect() as conn:
        return {t.name: repr(conn.execute(
                    sql(f'SELECT * FROM "{t.name}"')).all())
                for t in Base.metadata.sorted_tables}


def _own_run(app, svc, c, run_id=RUN):
    """A run of this tester's own assistant, in the fake engine, with the
    engine's UUID shape. Returns (agent_id, tenant)."""
    conn = c.post("/api/connections/sample").get_json()
    agent_id = conn["agent_id"]
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    tenant = svc.get_agent_context(account_id, agent_id)["tenant"]
    fake = app.extensions["careagents_runtime"]["client"]
    fake.runs[run_id] = {"id": run_id, "tenant_id": tenant,
                         "agent_id": agent_id, "status": "completed"}
    return agent_id, tenant


def _flag(client, **over):
    body = {"run_id": RUN, "reason": "wrong", "note": ""}
    body.update(over)
    return client.post("/feedback/flag", json=body)


# --- 404 ----------------------------------------------------------------------

def test_a_missing_page_says_so_plainly_with_a_way_home(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get("/no-such-page")
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    assert "That page isn't here." in _visible(body)
    assert 'href="/"' in body and "Back to CareAgents" in body
    assert ('href="/feedback?screen=not_found&amp;error=404"' in body)
    assert 'name="viewport"' in body                  # phone width


def test_a_signed_in_404_goes_back_to_the_assistant(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    body = c.get("/no-such-page").get_data(as_text=True)
    assert 'href="/home"' in body and "Back to your assistant" in body


def test_a_json_route_keeps_a_json_404(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get("/api/no-such-thing")
    assert r.status_code == 404
    assert r.get_json() == {"error": "not_found"}


def test_a_hidden_page_is_not_named_by_its_404(made, monkeypatch):  # noqa: F811
    # /admin/funnel hides itself with a 404; the report link must not say
    # which page it was.
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    r = c.get("/admin/funnel")
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    assert "admin" not in body.lower()
    assert "screen=not_found" in body


# --- 500 ----------------------------------------------------------------------

def test_a_crash_shows_the_plain_page_and_is_counted_by_route(made):  # noqa: F811
    app, svc = _boom_app(made)
    c = app.test_client()
    r = c.get(f"/boom-test?q={MARKER}")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert "Something broke on our side." in _visible(body)
    assert 'href="/feedback?screen=boom_test&amp;error=500"' in body
    assert MARKER not in body
    c.get("/boom-test")
    today = funnel.utc_day()
    assert _details(svc) == [(today, "server_error", "boom_test", 2)]
    assert funnel.daily(svc.session, days=1)["days"][0]["server_error"] == 2


def test_a_crash_leaves_no_url_query_or_exception_text_anywhere(
        made, caplog):  # noqa: F811
    app, svc = _boom_app(made)
    with caplog.at_level(logging.DEBUG, logger="careagents"):
        app.test_client().get(f"/boom-test?q={MARKER}")
    for name, rows in _all_rows(svc).items():
        assert MARKER not in rows, name
        assert "/boom-test" not in rows, name
    assert MARKER not in "".join(
        r.getMessage() for r in caplog.records
        if r.name.startswith("careagents"))


def test_a_json_route_keeps_a_json_500(made):  # noqa: F811
    app, svc = _boom_app(made)
    r = app.test_client().get("/api/boom-test")
    assert r.status_code == 500
    assert r.get_json() == {"error": "server_error"}
    assert [d[2] for d in _details(svc)] == ["api_boom_test"]


def test_a_crash_page_survives_a_count_that_fails(made, monkeypatch):  # noqa: F811
    app, _ = _boom_app(made)

    def broken(*a, **k):
        raise RuntimeError("database is gone")
    monkeypatch.setattr(funnel, "count_detail", broken)
    r = app.test_client().get("/boom-test")
    assert r.status_code == 500
    assert "Something broke on our side." in r.get_data(as_text=True)


def test_an_endpoint_not_of_this_app_counts_as_unknown(made):  # noqa: F811
    from careagents.errors import known_endpoint
    app, _ = made()
    assert known_endpoint(app, "feedback_page") == "feedback_page"
    for bad in ("/home", "home?x=1", None, "static", "no_such_view", 5):
        assert known_endpoint(app, bad) is None


def test_the_chat_error_page_links_a_report(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    r = c.get("/agents/agent_nope/approvals")
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    m = re.search(r'id="report-problem" href="/feedback\?screen=(\w+)"', body)
    assert m and m.group(1) in app.view_functions


# --- count_detail refuses anything off its lists --------------------------------

@pytest.mark.parametrize("kind,detail", [
    ("server_error", "/home?q=okapi"), ("server_error", "a b"),
    ("server_error", ""), ("server_error", "x" * 65),
    ("answer_flagged", "diabetes"), ("answer_flagged", ""),
    ("chat_error", "wrong")])
def test_count_detail_refuses_a_detail_off_its_list(made, kind, detail):  # noqa: F811
    _, svc = made()
    with pytest.raises(ValueError):
        funnel.count_detail(svc.session, kind, detail)
    assert _details(svc) == []
    assert funnel.daily(svc.session, days=1)["window"]["server_error"] == 0


# --- context on /feedback --------------------------------------------------------

def test_a_report_link_puts_its_checked_context_on_the_form(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get(
        f"/feedback?screen=chat&error=500&run_id={RUN}")
    body = r.get_data(as_text=True)
    form = body[body.index("<form"):body.index(">", body.index("<form"))]
    assert 'data-ctx-screen="chat"' in form
    assert 'data-ctx-error="500"' in form
    assert f'data-ctx-run-id="{RUN}"' in form
    assert "Nothing from your records." in _visible(body)


@pytest.mark.parametrize("query", [
    f"screen={MARKER}", f"screen=/chat?x={MARKER}", f"error={MARKER}",
    "error=200", f"run_id={MARKER}", f"run_id=run-{MARKER}",
    f"{MARKER}=chat"])
def test_a_report_link_never_echoes_what_is_off_the_lists(made, query):  # noqa: F811
    app, _ = made()
    body = app.test_client().get(f"/feedback?{query}").get_data(as_text=True)
    assert MARKER not in body
    assert "data-ctx-" not in body
    assert 'id="feedback-context"' not in body


def test_the_plain_form_has_no_context(made):  # noqa: F811
    app, _ = made()
    body = app.test_client().get("/feedback").get_data(as_text=True)
    assert "data-ctx-" not in body


def test_the_email_puts_the_context_apart_from_the_testers_words(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    _own_run(app, svc, c)
    r = c.post("/feedback", json={
        "stuck": "The chat froze", "change": "", "trust": "", "real": "",
        "context": {"screen": "chat", "error": "500", "run_id": RUN}})
    assert r.status_code == 200
    (msg,) = sent
    assert msg["subject"] == "Tester feedback — CareAgents"
    text = msg["text"]
    ctx_at = text.index("== Added by the app (no health details) ==")
    words_at = text.index("== In the tester's own words ==")
    block = text[ctx_at:words_at]
    assert "Screen: chat" in block and "Error code: 500" in block
    assert f"Chat answer (run id): {RUN}" in block
    assert re.search(r"Sent \(UTC\): \d{4}-\d\d-\d\d \d\d:\d\d:\d\d", block)
    assert "Build: " in block
    assert "The chat froze" not in block
    assert text.index("The chat froze") > words_at
    assert "<hr>" in msg["html"]
    html_ctx = msg["html"][:msg["html"].index("<hr>")]
    assert "The chat froze" not in html_ctx and "Screen: chat" in html_ctx
    for header in ("to", "subject", "reply_to", "from"):
        assert RUN not in str(msg.get(header))


def test_the_build_is_the_servers_own(made, sent):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    app.extensions["careagents_runtime"]["config"].build_sha = "fedcba987654"
    r = app.test_client().post("/feedback", json={
        "stuck": "hi", "context": {"screen": "chat"}},
        headers={"X-Real-IP": "203.0.113.20"})
    assert r.status_code == 200
    assert "Build: fedcba987654" in sent[0]["text"]


@pytest.mark.parametrize("context", [
    {"screen": MARKER}, {"screen": "/chat"}, {"screen": "static"},
    {"error": MARKER}, {"error": 500}, {"error": "200"},
    {"run_id": MARKER}, {"run_id": "run-1"}, {"run_id": RUN.upper()},
    {"build": "abc1234"}, {"time": "now"}, {"note": MARKER},
    {"answer": MARKER}, ["chat"], MARKER])
def test_context_off_the_lists_is_refused_and_nothing_sent(
        made, sent, context):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json={
        "stuck": "hi", "context": context},
        headers={"X-Real-IP": "203.0.113.21"})
    assert r.status_code == 400 and r.get_json() == {"error": "invalid"}
    assert sent == []


def test_a_report_without_context_still_says_build_and_time(made, sent):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json={"stuck": "hi"},
                               headers={"X-Real-IP": "203.0.113.22"})
    assert r.status_code == 200
    block = sent[0]["text"].split("== In the tester's own words ==")[0]
    assert "Build: " in block and "Sent (UTC): " in block
    assert "Screen:" not in block


# --- "This wasn't right" ------------------------------------------------------

def test_a_flag_needs_a_sign_in(made, sent):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = _flag(app.test_client())
    assert r.status_code == 401
    assert sent == []


def test_a_flag_is_emailed_and_counted_by_reason(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    _own_run(app, svc, c)
    r = _flag(c, reason="unsafe", note="It told me to stop a pill")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    (msg,) = sent
    assert msg["to"] == ["contactus@healthclaw.io"]
    assert msg["subject"] == "Answer flagged — CareAgents"
    assert msg["reply_to"] == "gene@example.com"
    text = msg["text"]
    words_at = text.index("== In the tester's own words ==")
    block = text[:words_at]
    assert f"Chat answer (run id): {RUN}" in block
    assert "Reason: Felt unsafe or worrying (unsafe)" in block
    assert "It told me to stop a pill" in text[words_at:]
    assert "It told me to stop a pill" not in block
    today = funnel.utc_day()
    assert _details(svc) == [(today, "answer_flagged", "unsafe", 1)]
    assert funnel.daily(svc.session, days=1)["days"][0]["answer_flagged"] == 1


@pytest.mark.parametrize("extra", [
    "answer", "question", "text", "message", "content", "context", "screen"])
def test_a_flag_with_any_other_field_is_refused(  # noqa: F811
        made, sent, monkeypatch, extra):  # noqa: F811
    # The answer's words and the question can never ride along.
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    r = _flag(c, **{extra: MARKER})
    assert r.status_code == 400 and r.get_json() == {"error": "invalid"}
    assert sent == []
    assert _details(svc) == []


def test_a_flag_note_is_capped(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    assert _flag(c, note="n" * (feedback.NOTE_MAX + 1)).get_json() == {
        "error": "too_long"}
    assert _flag(c, note="n" * feedback.RAW_NOTE_MAX + "n").status_code == 400
    assert sent == []
    assert _flag(c, note="n" * feedback.NOTE_MAX).status_code == 200
    assert len(sent) == 1
    assert feedback.NOTE_MAX <= 300


@pytest.mark.parametrize("over", [
    {"reason": "bad"}, {"reason": None}, {"reason": MARKER},
    {"run_id": MARKER}, {"run_id": "run-1"}, {"run_id": None},
    {"note": 5}, {"note": ["x"]}])
def test_a_malformed_flag_is_refused(made, sent, monkeypatch, over):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    assert _flag(c, **over).status_code == 400
    assert sent == []


def test_a_flag_is_json_only_and_size_checked(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    form = c.post("/feedback/flag", data=f"run_id={RUN}&reason=wrong",
                  content_type="application/x-www-form-urlencoded")
    assert form.status_code == 415
    big = c.post("/feedback/flag", data=json.dumps(
        {"run_id": RUN, "reason": "wrong", "note": "x" * 10_000}),
        content_type="application/json")
    assert big.status_code == 413
    assert sent == []


def test_flags_have_their_own_rate_limit(made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    for _ in range(feedback.FLAGS_PER_WINDOW):
        assert _flag(c).status_code == 200
    r = _flag(c)
    assert r.status_code == 429 and r.get_json() == {"error": "rate_limited"}
    # The feedback form is still open.
    assert c.post("/feedback", json={"stuck": "hi"}).status_code == 200


def test_a_flag_that_did_not_go_is_not_counted(made, sent, monkeypatch):  # noqa: F811
    app, svc = made()                       # no provider key: NOT_SENT
    c = _signed_in(app, svc, monkeypatch)
    r = _flag(c)
    assert r.status_code == 503 and r.get_json() == {"error": "not_sent"}
    assert _details(svc) == []


def test_a_flag_note_is_never_kept_or_logged(made, monkeypatch, caplog):  # noqa: F811
    from careagents import mail

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 500
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        _flag(c, note=MARKER)
        _flag(c, note=MARKER * 100)             # refused as too long
        _flag(c, note=MARKER, answer=MARKER)    # refused as invalid
    assert MARKER not in caplog.text
    for name, rows in _all_rows(svc).items():
        assert MARKER not in rows, name


# --- chat.js builds the flag from three values ---------------------------------

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cutConst(name) {
  const m = src.match(new RegExp('const ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
function cutFn(name) {
  const m = src.match(new RegExp('function ' + name + '\\([\\s\\S]*?\\n  }\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = [];
    this.attrs = {}; this.listeners = {}; this.parent = null; this._text = '';
    this.hidden = false; this.disabled = false; this.value = ''; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  insertAdjacentElement(_where, c) { c.parent = this.parent;
    this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, c);
    return c; }
  remove() { if (this.parent) this.parent.children.splice(
    this.parent.children.indexOf(this), 1); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  scrollIntoView() { scrolled.push(this.cls || this.tagName); }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  set className(v) { this.cls = v; }
  get textContent() { return this._text + this.children.map(
    c => c.textContent).join(''); }
  set textContent(t) { this._text = t || ''; this.children = []; }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
  querySelectorAll(sel) { const c = sel.slice(1);
    return this.all().filter(n => n !== this && (n.cls || '').split(' ').includes(c)); }
}
const document = { createElement: t => new Node(t) };
let sentBody = null;
const scrolled = [];
async function fetch(url, opts) { sentBody = opts.body;
  return { ok: true, json: async () => ({ ok: true }) }; }
const code = cutFn('el') + cutConst('FLAG_REASONS') + cutConst('NOTE_MAX')
  + cutFn('flagBody') + cutConst('FLAG_ERRORS') + cutFn('addFlag')
  + 'return addFlag;';
const addFlag = new Function('document', 'fetch', 'scroll', code)(
  document, fetch, () => {});
const args = JSON.parse(process.argv[2]);
(async () => {
  const log = new Node('div');
  const question = log.appendChild(new Node('div'));
  question.textContent = args.question;
  const bubble = log.appendChild(new Node('div'));
  bubble.textContent = args.answer;
  addFlag(bubble, args.run);
  const find = c => log.all().find(n => (n.cls || '').split(' ').includes(c));
  find('flag-open').listeners.click();
  const reasons = log.querySelectorAll('.flag-reason');
  reasons.find(r => r.textContent === args.label).listeners.click();
  find('flag-note').value = args.note;
  await find('flag-send').listeners.click();
  console.log(JSON.stringify({ body: sentBody, bubble: bubble.textContent,
    scrolled: scrolled,
    after: log.children.indexOf(bubble) + 1 === log.children.indexOf(
      find('flag')) }));
})();
"""


def _run_flag_js(**args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", _HARNESS, str(CHAT_JS),
                          json.dumps(args)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_flag_sends_the_run_id_reason_and_note_only():
    answer = f"Your A1c is 7.9 {MARKER}-answer"
    question = f"What is my A1c? {MARKER}-question"
    out = _run_flag_js(run=RUN, label="Confusing", note="Too long",
                       answer=answer, question=question)
    body = json.loads(out["body"])
    assert body == {"run_id": RUN, "reason": "confusing",
                    "note": "Too long"}
    assert MARKER not in out["body"]
    # The control sits beside the bubble; the answer's text is untouched.
    assert out["after"] is True
    assert out["bubble"] == answer
    # Opening the sheet brings its Send row into view (QA G7 on #980).
    assert "flag-actions" in out["scrolled"]


def test_the_flag_note_is_cut_to_the_cap_in_the_page_too():
    out = _run_flag_js(run=RUN, label="Something else",
                       note="z" * (feedback.NOTE_MAX + 50),
                       answer="a", question="q")
    body = json.loads(out["body"])
    assert body["reason"] == "other"
    assert len(body["note"]) == feedback.NOTE_MAX


def test_the_page_offers_the_same_reasons_the_server_takes():
    src = CHAT_JS.read_text()
    shown = re.findall(r'\["(\w+)", "[^"]+"\]', src[src.index(
        "const FLAG_REASONS"):src.index("const NOTE_MAX")])
    assert tuple(shown) == funnel.FLAG_REASONS
    assert f"const NOTE_MAX = {feedback.NOTE_MAX};" in src


def test_only_a_model_answer_gets_the_control():
    # Error frames and the 429 sentence are not answers: no run id passed.
    src = CHAT_JS.read_text()
    assert 'addAgentText(ev.text, state.runId);' in src
    calls = re.findall(r"(?<!function )addAgentText\(([^;{]*)\);", src)
    # A run id with no failure code is the flag; only the answer frame.
    flagged = [c for c in calls if "runId" in c and c.count(",") == 1]
    assert flagged == ["ev.text, state.runId"]


# --- the digest -------------------------------------------------------------------

def test_the_digest_lists_yesterdays_errors_by_route_and_flags_by_reason(
        made, monkeypatch):  # noqa: F811
    from careagents import mail
    _, svc = made()
    now = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc)
    yday = now - dt.timedelta(days=1)
    for _ in range(3):
        funnel.count_detail(svc.session, "server_error", "chat", yday)
    funnel.count_detail(svc.session, "server_error", "home", yday)
    funnel.count_detail(svc.session, "server_error", "home", now)  # today
    funnel.count_detail(svc.session, "answer_flagged", "wrong", yday)
    funnel.count_detail(svc.session, "answer_flagged", "wrong", yday)
    funnel.count_detail(svc.session, "answer_flagged", "unsafe", yday)
    got = []

    def fake_send(_cfg, to, subject, html, text, reply_to=None):
        got.append(text)
        return mail.SENT
    monkeypatch.setattr(mail, "send_message", fake_send)
    # Not _cfg(): on Postgres it drops the tables made() just built.
    cfg = _svc_cfg(svc, CARE_DIGEST_TO="owner@example.com")
    assert funnel.send_digest_if_due(svc.session, cfg, now) == mail.SENT
    (text,) = got
    errors = text[text.index("Server errors by route"):
                  text.index("Flagged answers by reason")]
    assert errors.split("\n")[1:3] == ["chat: 3", "home: 1"]
    flags = text[text.index("Flagged answers by reason"):]
    for line in ("Wrong: 2", "Confusing: 0", "Didn't do what I asked: 0",
                 "Felt unsafe or worrying: 1", "Something else: 0"):
        assert line in flags
    assert "server_error: 4" in text and "answer_flagged: 3" in text


def test_a_quiet_day_says_none(made):  # noqa: F811
    _, svc = made()
    report = funnel.daily(svc.session, days=1)
    text = funnel.digest_text(report, report["days"][0]["day"])
    assert "Server errors by route\nnone" in text
    assert "Wrong: 0" in text


def test_a_detail_kind_survives_racing_inserts(made):  # noqa: F811
    _, svc = made()
    day = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    for _ in range(5):
        funnel.count_detail(svc.session, "answer_flagged", "other", day)
    assert _details(svc) == [("2026-10-15", "answer_flagged", "other", 5)]


def test_uuid_shape_is_what_the_engine_mints():
    assert feedback._RUN_ID.match(str(uuid.uuid4()))


def test_the_raw_note_cap_holds_even_when_cleaning_would_shrink_it(
        made, sent, monkeypatch):  # noqa: F811
    # Control characters are dropped by clean(); measured raw, they still
    # count, so a padded note cannot slip a big body past the cap.
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    padded = "\x00" * (feedback.RAW_NOTE_MAX + 1) + "short"
    assert _flag(c, note=padded).get_json() == {"error": "too_long"}
    assert sent == []


# --- slice 1 additions: the tester's walk ----------------------------------

def test_a_405_gets_the_same_plain_page(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get("/logout")
    assert r.status_code == 405
    body = r.get_data(as_text=True)
    assert "That page isn't here." in _visible(body)
    assert "screen=not_found&amp;error=405" in body
    assert "POST" in r.headers.get("Allow", "")


def test_a_json_route_keeps_a_json_405(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get("/api/chat")
    assert r.status_code == 405
    assert r.get_json() == {"error": "method_not_allowed"}


def test_chat_error_codes_are_on_the_list(made):  # noqa: F811
    app, _ = made()
    body = app.test_client().get(
        f"/feedback?screen=chat&error=chat_error&run_id={RUN}"
    ).get_data(as_text=True)
    assert 'data-ctx-error="chat_error"' in body


def _fail(text=None):
    from careagents.agent import GENERIC_FAILURE_TEXT
    return {"role": "assistant", "content": text or GENERIC_FAILURE_TEXT}


def _ask(q):
    return {"role": "user", "content": q}


def test_a_wall_of_failed_turns_replays_once():
    from careagents.agent import RATE_LIMITED_TEXT
    from careagents.errors import collapse_failed_turns
    good = {"role": "assistant", "content": "Here is what I found."}
    past = [_ask("q1"), good, _ask("q2"), _fail(), _ask("q3"), _fail(),
            _ask("q4"), _fail(RATE_LIMITED_TEXT), _fail(), _ask("q5"), good]
    out = collapse_failed_turns(past)
    assert [m["content"] for m in out] == [
        "q1", good["content"], "q4", RATE_LIMITED_TEXT, "q5", good["content"]]
    assert [bool(m.get("failed")) for m in out] == [
        False, False, False, True, False, False]
    assert collapse_failed_turns([_fail(), _fail()]) == [
        dict(_fail(), failed=True)]


def test_the_chat_replays_one_failure_with_a_report_link(
        made, monkeypatch):  # noqa: F811
    from careagents.agent import GENERIC_FAILURE_TEXT
    app, svc = made()
    fake = app.extensions["careagents_runtime"]["client"]
    c = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    past = [_ask("first try"), _fail(), _ask("second try"), _fail()]
    monkeypatch.setattr(fake, "recent_messages", lambda *a, **k: past)
    page = c.get(f"/chat?agent={conn['agent_id']}").get_data(as_text=True)
    assert page.count(GENERIC_FAILURE_TEXT) == 1
    assert "first try" not in page and "second try" in page
    assert ('class="report-link" href="/feedback?screen=chat&amp;'
            'error=chat_error"') in page
    # The suggestion chips are still offered, shown as after a live error.
    assert re.search(r'id="starters"\s*>', page)
    # A history that ends well keeps them hidden, as before.
    past.append({"role": "assistant", "content": "Here it is."})
    page = c.get(f"/chat?agent={conn['agent_id']}").get_data(as_text=True)
    assert 'id="starters" hidden' in page


def test_the_live_error_bubble_gets_a_report_link_not_a_flag():
    src = CHAT_JS.read_text()
    assert 'addAgentText(ev.text, state.runId, "chat_error");' in src
    assert 'null, "connection");' in src
    body = src[src.index("function addReport"):]
    body = body[:body.index("\n  }\n")]
    assert 'screen: "chat"' in body and "run_id" in body
    # Pointers only: nothing from the bubble goes in the link.
    assert "textContent" not in body
    assert body.count("bubble") == 2        # the parameter, and placement


def test_a_signed_out_tester_may_leave_an_address_for_a_reply(made, sent):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    form = c.get("/feedback").get_data(as_text=True)
    assert 'name="reply_email"' in form and "(optional)" in form
    r = c.post("/feedback", json={"stuck": "hi",
                                  "reply_email": " tester@example.test "},
               headers={"X-Real-IP": "203.0.113.40"})
    assert r.status_code == 200
    (msg,) = sent
    assert msg["reply_to"] == "tester@example.test"
    assert "left an address" in msg["text"]
    assert "tester@example.test" not in msg["text"]
    for name, rows in _all_rows(svc).items():
        assert "tester@example.test" not in rows, name


@pytest.mark.parametrize("bad", [
    "not-an-email", "a@b", "x@example.test\r\nBcc: evil@example.com",
    "two@@example.test", 5, ["a@example.test"], "a" * 250 + "@example.test"])
def test_a_bad_reply_address_is_refused(made, sent, bad):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json={
        "stuck": "hi", "reply_email": bad},
        headers={"X-Real-IP": "203.0.113.41"})
    assert r.status_code == 400 and r.get_json() == {"error": "bad_email"}
    assert sent == []


def test_a_signed_in_tester_is_never_asked_for_an_address(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    assert 'name="reply_email"' not in c.get("/feedback").get_data(
        as_text=True)
    r = c.post("/feedback", json={"stuck": "hi",
                                  "reply_email": "other@example.test"})
    assert r.status_code == 200
    assert sent[0]["reply_to"] == "gene@example.com"


def test_a_reply_address_is_never_logged(made, monkeypatch, caplog):  # noqa: F811
    from careagents import mail

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 500
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    app, _ = made(RESEND_API_KEY="re_test")
    with caplog.at_level(logging.DEBUG):
        app.test_client().post("/feedback", json={
            "stuck": "hi", "reply_email": f"{MARKER}@example.test"},
            headers={"X-Real-IP": "203.0.113.42"})
    assert MARKER not in caplog.text


def test_the_thanks_page_says_when_they_will_hear_back(made, monkeypatch):  # noqa: F811
    app, svc = made()
    anon = app.test_client().get("/feedback/thanks").get_data(as_text=True)
    assert "We read every note." in _visible(anon)
    assert "within a day" not in anon
    left = app.test_client().get("/feedback/thanks?reply=1")
    assert "usually reply within a day" in _visible(left.get_data(
        as_text=True))
    c = _signed_in(app, svc, monkeypatch)
    assert "usually reply within a day" in c.get(
        "/feedback/thanks").get_data(as_text=True)


@pytest.mark.parametrize("back,href,label", [
    ("/chat?agent=agent_abc", "/chat?agent=agent_abc", "Back to your chat"),
    ("/brief?agent=agent_abc", "/brief?agent=agent_abc",
     "Back to your visit brief"),
    ("/agents/agent_abc/approvals", "/agents/agent_abc/approvals",
     "Back to where you were"),
    ("/home", "/home", "Back to your hub"),
    ("https://evil.example/chat", "/home", "Back to your hub"),
    ("//evil.example/chat", "/home", "Back to your hub"),
    ("javascript:alert(1)", "/home", "Back to your hub"),
    ("/chat?agent=x&q=okapi", "/chat?agent=x", "Back to your chat"),
    ("/chat?agent=<script>", "/chat", "Back to your chat"),
    ("/admin/funnel", "/home", "Back to your hub")])
def test_the_thanks_page_goes_back_where_they_came_from(
        made, monkeypatch, back, href, label):  # noqa: F811
    from urllib.parse import quote
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    body = c.get(f"/feedback/thanks?back={quote(back, safe='')}").get_data(
        as_text=True)
    m = re.search(r'id="feedback-back" href="([^"]*)">([^<]*)<', body)
    assert m and m.group(1).replace("&amp;", "&") == href
    assert m.group(2) == label
    assert "evil" not in body and "okapi" not in body
    assert "<script>" not in body


def test_the_form_learns_where_the_tester_came_from(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    body = c.get("/feedback", headers={
        "Referer": "http://localhost/chat?agent=agent_abc&x=okapi"}
    ).get_data(as_text=True)
    assert 'data-back="/chat?agent=agent_abc"' in body
    assert "okapi" not in body
    other = c.get("/feedback", headers={
        "Referer": "https://evil.example/chat"}).get_data(as_text=True)
    assert "data-back" not in other


def test_the_visit_brief_links_the_form(made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    r = c.get(f"/brief?agent={conn['agent_id']}")
    assert r.status_code == 200
    assert '<a href="/feedback">Tell us</a>' in r.get_data(as_text=True)


# --- review round on #980 ------------------------------------------------------

def _account_id(c):
    with c.session_transaction() as sess:
        return sess["account_id"]


def _block(text):
    return text.split("== In the tester's own words ==")[0]


def test_both_mails_carry_the_account_id_and_never_its_address(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    aid = _account_id(c)
    assert c.post("/feedback", json={"stuck": "hi"}).status_code == 200
    assert _flag(c).status_code == 200
    report, flag = sent
    for msg in (report, flag):
        block = _block(msg["text"])
        assert f"Account: {aid}" in block
        assert "gene@example.com" not in block
        assert "gene@example.com" not in msg["text"]
        assert msg["reply_to"] == "gene@example.com"
        assert aid not in str(msg["subject"])


def test_a_signed_out_report_has_no_account_line(made, sent):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    app.test_client().post("/feedback", json={"stuck": "hi"},
                           headers={"X-Real-IP": "203.0.113.50"})
    assert "Account:" not in sent[0]["text"]


def test_a_flag_on_someone_elses_run_drops_the_run_id(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    other = _signed_in(app, svc, monkeypatch, email="other@example.com")
    _own_run(app, svc, other)                    # RUN is the other's
    c = _signed_in(app, svc, monkeypatch)
    c.post("/api/connections/sample")
    r = _flag(c, reason="wrong", note="Not right")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    (msg,) = sent
    assert RUN not in msg["text"] and RUN not in msg["html"]
    assert "Not right" in msg["text"]
    assert "Reason: Wrong (wrong)" in msg["text"]
    assert _details(svc) == [(funnel.utc_day(), "answer_flagged", "wrong", 1)]


def test_a_report_on_someone_elses_run_drops_the_run_id(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    other = _signed_in(app, svc, monkeypatch, email="other@example.com")
    _own_run(app, svc, other)
    c = _signed_in(app, svc, monkeypatch)
    r = c.post("/feedback", json={"stuck": "broke", "context": {
        "screen": "chat", "error": "chat_error", "run_id": RUN}})
    assert r.status_code == 200
    block = _block(sent[0]["text"])
    assert RUN not in sent[0]["text"]
    assert "Screen: chat" in block and "Error code: chat_error" in block


def test_a_signed_out_report_never_carries_a_run_id(made, sent):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json={
        "stuck": "broke", "context": {"run_id": RUN, "screen": "chat"}},
        headers={"X-Real-IP": "203.0.113.51"})
    assert r.status_code == 200
    assert RUN not in sent[0]["text"]


def test_a_run_of_the_same_tenant_but_another_agent_is_dropped(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    _, tenant = _own_run(app, svc, c)
    fake = app.extensions["careagents_runtime"]["client"]
    fake.runs[RUN]["agent_id"] = "agent_someone_else"
    assert _flag(c).status_code == 200
    assert RUN not in sent[0]["text"]


def test_an_engine_that_cannot_answer_drops_the_run_id(
        made, sent, monkeypatch):  # noqa: F811
    from careagents.healthclaw import HealthClawError
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    _own_run(app, svc, c)
    fake = app.extensions["careagents_runtime"]["client"]

    def down(*a, **k):
        raise HealthClawError("engine down", 0)
    monkeypatch.setattr(fake, "get_agent_run", down)
    r = _flag(c, note="still goes")
    assert r.status_code == 200
    assert RUN not in sent[0]["text"] and "still goes" in sent[0]["text"]


def test_the_run_lookup_is_labelled_with_the_account(made, sent, monkeypatch):  # noqa: F811
    from careagents import healthclaw
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    _own_run(app, svc, c)
    fake = app.extensions["careagents_runtime"]["client"]
    real, seen = fake.get_agent_run, []

    def labelled(*a, **k):
        seen.append(healthclaw.agent_header())
        return real(*a, **k)
    monkeypatch.setattr(fake, "get_agent_run", labelled)
    assert _flag(c).status_code == 200
    assert seen == [f"careagents:{_account_id(c)}"]
    assert RUN in sent[0]["text"]


@pytest.mark.parametrize("bad", [
    "Support<atk@example.test>", "Support <atk@example.test>",
    '"Support"@example.test', "<atk@example.test>", "atk@example.test>"])
def test_a_reply_address_must_be_bare(made, sent, bad):  # noqa: F811
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json={
        "stuck": "hi", "reply_email": bad},
        headers={"X-Real-IP": "203.0.113.52"})
    assert r.status_code == 400 and r.get_json() == {"error": "bad_email"}
    assert sent == []


def test_the_digest_and_the_page_name_the_ruled_reasons():
    assert funnel.FLAG_LABELS == (
        ("wrong", "Wrong"), ("confusing", "Confusing"),
        ("not_what_asked", "Didn't do what I asked"),
        ("unsafe", "Felt unsafe or worrying"),
        ("other", "Something else"))
    src = CHAT_JS.read_text()
    block = src[src.index("const FLAG_REASONS"):src.index("const NOTE_MAX")]
    pairs = re.findall(r'\["(\w+)", "([^"]+)"\]', block)
    assert tuple(pairs) == funnel.FLAG_LABELS


def test_the_flag_sheet_keeps_send_in_view():
    src = CHAT_JS.read_text()
    body = src[src.index("function addFlag"):]
    assert "row.scrollIntoView" in body
    assert 'note.addEventListener("focus"' in body
    css = (_ROOT / "careagents" / "static" / "careagents.css").read_text()
    rule = css[css.index(".flag-actions {"):]
    rule = rule[:rule.index("}")]
    assert "position: sticky" in rule and "bottom: 0" in rule
