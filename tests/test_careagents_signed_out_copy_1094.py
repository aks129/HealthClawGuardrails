"""A browser signed out by another device is told so (#1094 patient walk).

- Chat: a 401 says "You were signed out" with a link to /auth, once, not
  "Connection hiccup" on every retry. Run under node against the real
  function cut from chat.js, with a minimal DOM.
- /auth: a session the generation check ended says why, once. A session
  from before the generation existed gets the shorter line. A plain visit
  to /auth says neither.

Synthetic addresses only.
"""
from __future__ import annotations

import json
import pathlib
import re
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
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cutFn(name) {
  const m = src.match(new RegExp('function ' + name + '\\([\\s\\S]*?\\n  }\\n'));
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
}
const document = {
  createElement: t => new Elem(t),
  createTextNode: t => new Text(t),
};
const log = new Elem('div');
const scroll = () => {};
const code = cutFn('el') + cutFn('addSignedOut') + 'return addSignedOut;';
new Function('document', 'log', 'scroll', code)(document, log, scroll)();
const msg = log.children[0];
const link = msg.children.find(c => c.tagName === 'A');
process.stdout.write(JSON.stringify({
  count: log.children.length, text: msg.textContent,
  link: link ? {text: link.textContent, href: link.href} : null}));
"""


def test_a_401_in_chat_says_signed_out_with_the_way_back():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = json.loads(subprocess.run(
        ["node", "-e", _HARNESS, "--", str(CHAT_JS)],
        capture_output=True, text=True, check=True).stdout)
    assert out["count"] == 1
    assert out["text"] == "You were signed out. Sign in again to keep going."
    assert out["link"] == {"text": "Sign in again", "href": "/auth"}


def test_the_chat_loop_stops_on_a_401_before_it_retries():
    """The 401 branch comes before the line that turns a bad response into
    a retry, and it returns, so "Connection hiccup" is never reached."""
    src = CHAT_JS.read_text()
    branch = re.search(r"if \(resp\.status === 401\) \{\s*typing\.remove\(\);"
                       r"\s*addSignedOut\(\);\s*return;\s*\}", src)
    retry = src.index('throw new Error("event stream unavailable")')
    assert branch and branch.start() < retry


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
