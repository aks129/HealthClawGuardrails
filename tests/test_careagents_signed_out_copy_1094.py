"""A browser signed out by another device is told so (#1094 patient walk).

- Chat: a 401 says "You were signed out" with a link to /auth, once, not
  "Connection hiccup" on every retry. Run under node: the real send() loop
  cut from chat.js, a minimal DOM, and a fetch that answers 401.
- /auth: a session the generation check ended says why, once. A session
  from before the generation existed gets the shorter line. A plain visit
  to /auth says neither.

Synthetic addresses only.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from careagents.app import _SESSION_GEN
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import made  # noqa: F401  (fixture)

pytestmark = pytest.mark.extension

EMAIL = "signedout1094@example.test"
CHAT_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "chat.js")
ELSEWHERE = "because you signed out somewhere else"
EXPIRED = "You were signed out. Sign in"

_HARNESS = r"""
// Drives the real send() cut from chat.js, with the real el and
// addSignedOut, against a fetch that answers 401 every time.
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cutFn(name) {
  const m = src.match(new RegExp('(async )?function ' + name
                                 + '\\([\\s\\S]*?\\n  }\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
class Text { constructor(t) { this.data = t; }
  get textContent() { return this.data; } }
class Elem {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(t) { this.children = t ? [new Text(t)] : []; }
  set className(_) {}
  remove() {}
  focus() {}
}
const document = {
  createElement: t => new Elem(t),
  createTextNode: t => new Text(t),
};
const calls = [];
const said = [];
async function fetch(url, opts) {
  calls.push(url);
  const r = {status: 401, ok: false, body: null,
             headers: {get: () => null},
             json: async () => ({error: 'sign in'})};
  r.clone = () => r;
  return r;
}
const env = {
  document, fetch, window: {}, log: new Elem('div'),
  box: {value: '', focus() {}}, sendBtn: {disabled: false},
  scroll() {}, hideSuggestions() {}, showSuggestions() {}, addUser() {},
  addTyping: () => new Elem('div'),
  addAgentText: (t) => said.push(t),
  addLimitFeedback() {}, limitText: () => '',
  consumeEvents: async () => { throw new Error('not reached'); },
  pause: async () => {},
};
const names = Object.keys(env);
const code = 'let busy = false; const AGENT = "a"; const CONVERSATION = "c";\n'
  + cutFn('el') + cutFn('addSignedOut') + cutFn('send') + 'return send;';
const send = new Function(...names, code)(...names.map(k => env[k]));
send('Explain my latest labs').then(() => {
  process.stdout.write(JSON.stringify({
    calls: calls.length, said,
    log: env.log.children.map(c => c.textContent),
    link: (env.log.children[0] || {children: []}).children
      .filter(c => c.tagName === 'A').map(c => ({text: c.textContent,
                                                 href: c.href}))}));
});
"""


def test_a_401_in_chat_says_signed_out_once_and_does_not_retry():
    if shutil.which("node") is None:
        # The only thing the patient sees in this state: in CI a missing
        # node is a failure, not a skip that reads like a pass.
        if os.environ.get("CI"):
            pytest.fail("node is not installed, so the chat 401 is untested")
        pytest.skip("node is not installed")
    out = json.loads(subprocess.run(
        ["node", "-e", _HARNESS, "--", str(CHAT_JS)],
        capture_output=True, text=True, check=True).stdout)
    assert out["calls"] == 1, out
    assert not any("Connection hiccup" in t for t in out["said"]), out
    assert out["log"] == ["You were signed out. Sign in again to keep going."]
    assert out["link"] == [{"text": "Sign in again", "href": "/auth"}]


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    assert c.get("/home").status_code == 200
    return c


def _copy(app, c):
    other = app.test_client()
    other.set_cookie("session", c.get_cookie("session").value)
    return other


def test_auth_says_why_once_after_a_sign_out_elsewhere(made, monkeypatch):  # noqa: F811
    app, svc = made()
    phone = _signed_in(app, svc, monkeypatch)
    laptop = _copy(app, phone)
    phone.post("/logout")
    r = laptop.get("/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    first = laptop.get("/auth").get_data(as_text=True)
    assert ELSEWHERE in first
    again = laptop.get("/auth").get_data(as_text=True)
    assert ELSEWHERE not in again and EXPIRED not in again


def test_auth_says_the_short_line_once_for_a_session_from_before(
        made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as s:
        s.pop(_SESSION_GEN)
    c.get("/home")
    first = c.get("/auth").get_data(as_text=True)
    assert EXPIRED in first and ELSEWHERE not in first
    assert EXPIRED not in c.get("/auth").get_data(as_text=True)


def test_a_plain_visit_or_your_own_sign_out_gives_no_reason(
        made, monkeypatch):  # noqa: F811
    app, svc = made()
    fresh = app.test_client().get("/auth").get_data(as_text=True)
    assert 'id="signed-out-why"' not in fresh
    c = _signed_in(app, svc, monkeypatch)
    c.post("/logout")
    assert 'id="signed-out-why"' not in c.get("/auth").get_data(as_text=True)


PARTIAL = "We couldn't sign out your other devices"


def test_a_sign_out_that_misses_the_other_devices_says_so_once(
        made, monkeypatch):  # noqa: F811
    """The buttons promise every device. When the database write fails,
    this browser is signed out and the landing page says the rest was not."""
    from sqlalchemy.exc import OperationalError
    app, svc = made()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    c = _signed_in(app, svc, monkeypatch)

    def down(_account_id):
        raise OperationalError("UPDATE ca_accounts", {}, Exception("locked"))
    monkeypatch.setattr(svc, "end_sessions", down)
    r = c.post("/logout")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert PARTIAL in c.get("/").get_data(as_text=True)
    assert PARTIAL not in c.get("/").get_data(as_text=True)
    assert c.get("/home").status_code == 302


def test_a_sign_out_that_reaches_every_device_says_nothing_extra(
        made, monkeypatch):  # noqa: F811
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    c.post("/logout")
    assert PARTIAL not in c.get("/").get_data(as_text=True)
