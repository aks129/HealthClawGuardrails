"""One contact address, tappable wherever stage 1 shows it, and the chat's
terms sentence pointing at the hub (#856 patient-tester review)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from careagents import beta
from careagents.app import chat_links
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)

STATIC = pathlib.Path(__file__).resolve().parents[1] / "careagents" / "static"
MAILTO = '<a href="mailto:contactus@healthclaw.io">contactus@healthclaw.io</a>'


def test_the_consent_card_names_the_one_contact_address(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    card = c.get("/home").get_data(as_text=True).split(
        'id="consent-modal"', 1)[1].split('id="consent-agree"', 1)[0]
    assert MAILTO in card
    assert "support@healthclaw.io" not in card


def test_every_stage1_sentence_uses_the_contact_address():
    for text in (beta.PAUSED_TEXT, beta.PAUSED_RECORDS_TEXT,
                 beta.PAUSED_HUB_TEXT):
        assert "contactus@healthclaw.io" in text
        assert "support@" not in text


def test_the_terms_sentence_points_at_the_hub():
    assert "on your hub" in beta.TERMS_TEXT
    assert "home page" not in beta.TERMS_TEXT


def test_a_replayed_bubble_carries_both_links_and_nothing_else():
    out = str(chat_links(beta.TERMS_TEXT + " <i>x</i> " + beta.PAUSED_TEXT))
    assert '<a href="/home">your hub</a>' in out
    assert MAILTO in out
    assert "<i>" not in out and "&lt;i&gt;" in out


def test_the_replayed_chat_links_agent_bubbles_only(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    svc.set_paused("gene@example.com", True)
    r = c.post("/api/chat", json={
        "agent_id": agent_id, "request_id": "copy-1",
        "message": "mail contactus@healthclaw.io for your hub"},
        buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "copy-worker").run_once()
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert page.count(MAILTO) == 1          # the assistant's, not the person's
    assert "mail contactus@healthclaw.io for your hub" in page


def test_announced_lines_link_the_contact_address():
    src = (STATIC / "home.js").read_text()
    body = src.split("function announce(", 1)[1].split("\n  }", 1)[0]
    assert "el.textContent = text; linkContact(el);" in body


_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function textNode(t) { return { nodeType: 3, text: String(t) }; }
function element(tag) {
  const n = { nodeType: 1, tagName: tag.toUpperCase(), children: [],
              className: '', href: '',
              appendChild(c) { this.children.push(c); return c; } };
  Object.defineProperty(n, 'textContent', {
    set(v) { this.children = v === '' ? [] : [textNode(v)]; },
    get() { return this.children.map(c => c.nodeType === 3 ? c.text
                                                     : c.textContent).join(''); },
  });
  return n;
}
globalThis.document = { createElement: element, createTextNode: textNode };
function cut(from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + from);
  return src.slice(a, b);
}
const f = new Function(cut('function el(', 'function addUser(')
  + cut('const BOLD_OR_ITALIC', 'function renderMarkdown(')
  + 'return appendInline;')();
const p = element('p');
f(p, process.argv[2]);
process.stdout.write(JSON.stringify(p.children.map(c => c.nodeType === 3
  ? { text: c.text } : { tag: c.tagName, href: c.href, text: c.textContent })));
"""


def _inline(text):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(STATIC / "chat.js"), text],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_live_bubble_links_the_hub_and_the_address():
    got = _inline(beta.TERMS_TEXT + " Or write to contactus@healthclaw.io.")
    links = [n for n in got if n.get("tag") == "A"]
    assert links == [
        {"tag": "A", "href": "/home", "text": "your hub"},
        {"tag": "A", "href": "mailto:contactus@healthclaw.io",
         "text": "contactus@healthclaw.io"}]


def test_a_model_written_link_is_still_text():
    got = _inline("see [here](javascript:alert(1)) or https://x.example")
    assert all("tag" not in n for n in got)
