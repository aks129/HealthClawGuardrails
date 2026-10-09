"""A calm chat page for a first-timer (2026-10 patient-tester walk, 375px).

The header carried the assistant's name, its persona label ("Calm Guide"),
"← hub", "Waiting for you" and "Safety grade: A": five things to read
before the first question. The hub link, the waiting list and the grade
now sit in one ⋯ menu, and the persona label is gone.

The made-up-records line showed twice on every sample answer: the sticky
banner, then again at the top of each reply. It now shows once in the web
view, on the banner. The worker still opens every stored and texted answer
with it (tests/test_careagents_sample_framing*.py, unchanged); the page
drops only that exact leading copy, and only while the banner is there.
So a sample answer can never render without the label in view.

The opening message asks "Want me to walk through what they show?" and
now has a one-tap "Yes, walk me through". After an error the suggested
questions come back. All data is synthetic.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from careagents import beta
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, _login, cfg, svc)
from tests.test_careagents_sample_framing import _real_chat_app

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "careagents" / "static" / "chat.js"
CSS = (ROOT / "careagents" / "static" / "careagents.css").read_text()


def _head(page: str) -> str:
    return page[page.index('<div class="chat-head">'):page.index('id="log"')]


def _menu(page: str) -> str:
    m = re.search(r'<details class="chat-menu">.*?</details>', page, re.S)
    assert m, "no menu"
    return m.group(0)


# --- the header ---------------------------------------------------------------

def test_hub_waiting_and_grade_live_in_one_menu(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    head = _head(page)
    menu = _menu(page)
    assert head.count("<details") == 1
    assert re.search(r'<summary[^>]*aria-label="Menu"[^>]*>⋯</summary>', menu)
    assert 'href="/home"' in menu
    assert f'href="/agents/{agent_id}/approvals"' in menu
    assert "Waiting for you" in menu
    # The grade keeps its id, link and the words chat.js writes into it.
    assert re.search(r'<a [^>]*id="trust-pill"[^>]*href="/safety"', menu)
    # Nothing to tap in the header outside the menu.
    outside = head.replace(menu, "")
    assert "<a " not in outside and "pill" not in outside


def test_the_header_has_no_persona_label(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.personas import PERSONAS
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    head = _head(c.get(f"/chat?agent={agent_id}").get_data(as_text=True))
    assert "chat-sub" not in head
    assert PERSONAS["calm"]["name"] not in head
    assert "Juniper" in head


def test_the_menu_offers_a_passkey_until_there_is_one(cfg, svc, monkeypatch):  # noqa: F811
    """A first run skips the passkey prompt; this is where it waits."""
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    menu = _menu(c.get(f"/chat?agent={agent_id}").get_data(as_text=True))
    assert 'href="/auth?enroll=1">Add a passkey</a>' in menu
    monkeypatch.setattr(svc, "has_passkey", lambda account_id: True)
    menu = _menu(c.get(f"/chat?agent={agent_id}").get_data(as_text=True))
    assert "Add a passkey" not in menu


def test_the_menu_fits_a_phone():
    """A ⋯ button at the right of the header, its list under it and inside
    the screen, and a 44px target."""
    rule = CSS[CSS.index(".chat-menu-btn {"):]
    rule = rule[:rule.index("}")]
    assert "min-width: 44px" in rule and "min-height: 44px" in rule
    lst = CSS[CSS.index(".chat-menu-list {"):]
    lst = lst[:lst.index("}")]
    assert "position: absolute" in lst and "right: 0" in lst


# --- the opening message ------------------------------------------------------

def test_the_opening_question_has_a_yes(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert "Want me to walk through what they show?" in page
    btn = re.search(r'<button[^>]*class="quick-reply"[^>]*>([^<]*)</button>',
                    page)
    assert btn and btn.group(1).strip() == "Yes, walk me through"
    # What it sends names the records, so the model knows what "yes" is.
    assert 'data-ask="Walk me through what these made-up records show."' \
        in btn.group(0)
    # It answers the greeting, so it sits before the other ideas.
    assert page.index('class="quick-reply"') < page.index('id="starters"')


def test_a_returning_visit_has_no_yes(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, tenant, _cid = _chat_app(cfg, svc, monkeypatch)
    conv = fake.conversation_id(agent_id)
    fake.logged.setdefault((tenant, conv), []).extend([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"}])
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert 'class="quick-reply"' not in page
    # The ideas are on the page, hidden until an error brings them back.
    assert re.search(r'<div class="starters" id="starters" hidden>', page)


# --- the made-up line, once per view --------------------------------------------

def _with_history(c, fake, agent_id, tenant, answer):
    conv = fake.conversation_id(agent_id)
    fake.logged.setdefault((tenant, conv), []).extend([
        {"role": "user", "content": "What do my labs say?"},
        {"role": "assistant", "content": answer}])
    return c.get(f"/chat?agent={agent_id}").get_data(as_text=True)


def test_a_replayed_sample_answer_shows_the_line_once(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, tenant, _cid = _chat_app(cfg, svc, monkeypatch)
    body = "Creatinine rose from 0.8 to 1.3 mg/dL in 6 days."
    page = _with_history(c, fake, agent_id, tenant,
                         f"{beta.SAMPLE_FRAME}\n\n{body}")
    assert page.count(beta.SAMPLE_FRAME) == 1
    assert page.index(beta.SAMPLE_FRAME) < page.index('id="log"')
    assert f"<p>{body}</p>" in page


def test_only_the_exact_leading_line_is_dropped(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, fake, agent_id, tenant, _cid = _chat_app(cfg, svc, monkeypatch)
    later = f"Creatinine rose.\n\n{beta.SAMPLE_FRAME}"
    page = _with_history(c, fake, agent_id, tenant, later)
    assert page.count(beta.SAMPLE_FRAME) == 2      # banner + the body's own
    alone = _with_history(c, fake, agent_id, tenant, beta.SAMPLE_FRAME)
    assert alone.count(beta.SAMPLE_FRAME) >= 2     # never an empty bubble


def test_a_real_page_never_drops_anything(cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    tenant = fake.tenants[-1]
    said = f"{beta.SAMPLE_FRAME}\n\nCreatinine rose."
    page = _with_history(c, fake, agent_id, tenant, said)
    assert "sample-banner" not in page
    assert beta.SAMPLE_FRAME in page            # the model's words, as said


def test_the_banner_is_always_there_on_a_sample_chat(cfg, svc, monkeypatch):  # noqa: F811
    """The guarantee the single display rests on: every sample chat page,
    first visit or returning, renders the banner above the log, and its
    words are exactly the line the worker writes."""
    _app, c, fake, agent_id, tenant, _cid = _chat_app(cfg, svc, monkeypatch)
    for page in (c.get(f"/chat?agent={agent_id}").get_data(as_text=True),
                 _with_history(c, fake, agent_id, tenant,
                               f"{beta.SAMPLE_FRAME}\n\nx")):
        m = re.search(r'<p class="beta-banner sample-banner"[^>]*>([^<]*)</p>',
                      page)
        assert m and m.group(1) == beta.SAMPLE_FRAME
        assert page.index(m.group(0)) < page.index('id="log"')


# --- chat.js, under node --------------------------------------------------------

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const m = src.match(/function withoutFrame\([\s\S]*?\n  }\n/);
if (!m) throw new Error('withoutFrame not found');
const cases = JSON.parse(process.argv[2]);
const out = [];
for (const [banner, text] of cases) {
  const document = { querySelector: (sel) =>
    sel === '.sample-banner' && banner !== null
      ? { textContent: banner } : null };
  const fn = new Function('document', m[0] + 'return withoutFrame;')(document);
  out.push(fn(text));
}
process.stdout.write(JSON.stringify(out));
"""


def _strip(cases):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    run = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(CHAT_JS), json.dumps(cases)],
        capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_a_live_sample_answer_drops_only_the_banners_own_words():
    f = beta.SAMPLE_FRAME
    body = "Creatinine rose."
    got = _strip([
        [f"\n  {f}\n", f"{f}\n\n{body}"],     # banner on the page: once
        [None, f"{f}\n\n{body}"],             # no banner: never dropped
        ["Something else.", f"{f}\n\n{body}"],  # not the banner's words
        [f, f"{body}\n\n{f}"],                # not at the start
        [f, f"{f} Actually yours.\n\n{body}"],  # not the whole first line
        [f, f"{f}\n\n  "],                    # nothing left: keep it
    ])
    assert got == [body, f"{f}\n\n{body}", f"{f}\n\n{body}",
                   f"{body}\n\n{f}", f"{f} Actually yours.\n\n{body}",
                   f"{f}\n\n  "]


def test_every_agent_bubble_goes_through_the_drop():
    js = CHAT_JS.read_text()
    fn = js[js.index("function addAgentText("):]
    fn = fn[:fn.index("\n  }\n")]
    assert "withoutFrame(text)" in fn


def test_an_error_brings_the_suggestions_back():
    js = CHAT_JS.read_text()
    send = js[js.index("async function send("):]
    send = send[:send.index("\n  }\n")]
    assert "starters.remove()" not in send
    assert "hideSuggestions()" in send
    catch = send[send.index("} catch (e) {"):]
    assert "showSuggestions()" in catch
    assert "if (failed) showSuggestions();" in send
    events = js[js.index('ev.type === "error"'):]
    assert "failed = true" in events[:events.index("}")]
    assert '.quick-reply' in js
