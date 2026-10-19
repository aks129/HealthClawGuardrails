"""Trust breakers from the 2026-10-18 patient-tester walk at 375px.

Each test pins one screen to what a returning tester needs to see: a way to
sign in after signing out, the emailed code first, the finished form in the
chat, a link to the visit brief, an honest funnel, and a "Saved" in view.
"""

from __future__ import annotations

import pathlib
import re

from careagents import funnel
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture
from tests.test_careagents_what_happened import OutcomeClient, _app, _signed_in

cfg = _cfg_fixture
svc = _svc_fixture

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "careagents" / "static"


# --- 1. a returning user can sign in from the landing page -------------------

def test_the_signed_out_landing_page_offers_sign_in(cfg, svc):
    page = _app(cfg, svc, FakeClient()).test_client().get("/").get_data(
        as_text=True)
    assert 'href="/auth">Get started</a>' in page
    assert re.search(r'<a[^>]*href="/auth"[^>]*>Sign in</a>', page)
    # Beside the button, before the note: above the fold at 375x560.
    assert page.index(">Sign in</a>") < page.index("Start with made-up")


def test_a_signed_in_landing_page_has_no_sign_in(cfg, svc, monkeypatch):
    c = _app(cfg, svc, FakeClient()).test_client()
    _login(c, svc, monkeypatch)
    page = c.get("/").get_data(as_text=True)
    assert ">Sign in</a>" not in page


# --- 2. the emailed code leads; the passkey is the second way ----------------

def test_sign_in_leads_with_the_emailed_code(cfg, svc):
    page = _app(cfg, svc, FakeClient()).test_client().get("/auth").get_data(
        as_text=True)
    email_btn = re.search(r'<button[^>]*id="email-btn"[^>]*>', page).group(0)
    passkey = re.search(r'<button[^>]*id="passkey-btn"[^>]*>', page).group(0)
    assert "btn-primary" in email_btn and "btn-primary" not in passkey
    assert page.index('id="email-btn"') < page.index('id="passkey-btn"')
    assert "face or fingerprint" not in page
    assert "8-digit code" in page


# --- 3. an approved form shows as done in the chat ---------------------------

def _done_form(id_="act-done"):
    return {"id": id_, "kind": "form-fill", "to": None,
            "status": "completed"}


def test_an_approved_form_is_shown_done_in_the_chat(cfg, svc, monkeypatch):
    fake = OutcomeClient(pending=[], recent=[_done_form()])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert 'data-done-form="act-done"' in html


def test_a_waiting_form_wins_over_a_done_one(cfg, svc, monkeypatch):
    """A new form waiting for review is the one to act on; an older done
    one beside it would read as though the new one were finished."""
    fake = OutcomeClient(
        pending=[{"id": "act-new", "kind": "form-fill", "to": None,
                  "status": "awaiting_confirmation"}],
        recent=[_done_form()])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "act-new" in html
    assert "data-done-form" not in html


def test_only_a_completed_form_is_shown_done(cfg, svc, monkeypatch):
    fake = OutcomeClient(pending=[], recent=[
        {**_done_form(), "status": "failed"},
        {**_done_form("act-sms"), "kind": "sms"}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "data-done-form" not in html


def test_a_recent_lookup_that_fails_still_renders_the_chat(
        cfg, svc, monkeypatch):
    fake = OutcomeClient(pending=[], recent_error=True)
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    r = c.get(f"/chat?agent={agent}")
    assert r.status_code == 200
    assert "data-done-form" not in r.get_data(as_text=True)


def test_chat_js_draws_the_done_form_card_on_load():
    js = (STATIC / "chat.js").read_text()
    card = js[js.index("function addFormDoneCard"):]
    card = card[:card.index("\n  }\n")]
    # The review page shows a finished form with its PDF; no bearer link
    # is put in the chat page.
    assert '"/review/" + AGENT + "/" + encodeURIComponent(actionId)' in card
    assert "addFormDoneCard(log.dataset.doneForm)" in js


def test_the_newest_finished_form_is_the_one_shown_done(
        cfg, svc, monkeypatch):
    """Two forms finished this week: the card links the newer one. The
    fake lists the older first, so a sort that is dropped or reversed
    links last week's form (QA of #998)."""
    fake = OutcomeClient(pending=[], recent=[
        {**_done_form("act-old"), "updated_at": "2026-10-14T09:00:00Z"},
        {**_done_form("act-new"), "updated_at": "2026-10-18T09:00:00Z"}])
    c, agent, _ = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert 'data-done-form="act-new"' in html
    assert "act-old" not in html


def test_a_disconnected_connection_draws_no_done_form(cfg, svc, monkeypatch):
    """A revoked connection is not a pathway to its requests (#215): the
    chat neither asks for its recent actions nor draws a done card."""
    class Counting(OutcomeClient):
        recent_calls = 0

        def recent_actions(self, tenant):
            Counting.recent_calls += 1
            return super().recent_actions(tenant)

    fake = Counting(pending=[], recent=[_done_form()])
    c, agent, conn = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    with c.session_transaction() as sess:
        account_id = sess["account_id"]
    assert svc.revoke_connection(account_id, conn) is True
    r = c.get(f"/chat?agent={agent}")
    assert r.status_code == 200
    assert "data-done-form" not in r.get_data(as_text=True)
    assert Counting.recent_calls == 0


# --- 4. the visit brief is one tap from the chat -----------------------------

def test_the_brief_card_links_to_the_visit_brief_page():
    js = (STATIC / "chat.js").read_text()
    assert 'ev.kind === "brief"' in js
    # Drawn after the streamed answer, on done, not above it.
    done = js[js.index('ev.type === "done"'):]
    done = done[:done.index("\n        }\n")]
    assert "if (state.brief) addBriefCard();" in done
    card = js[js.index("function addBriefCard"):]
    card = card[:card.index("\n  }\n")]
    assert '"/brief?agent=" + AGENT' in card


def test_the_chat_menu_has_the_visit_brief(cfg, svc, monkeypatch):
    c, agent, _ = _signed_in(_app(cfg, svc, FakeClient()), svc, monkeypatch)
    html = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    menu = html[html.index('class="chat-menu-list"'):]
    menu = menu[:menu.index("</nav>")]
    assert f'href="/brief?agent={agent}">Visit brief</a>' in menu


# --- 6. the funnel's conversion does not chain through "confirmed" -----------

def test_conversion_skips_the_beta_request_step(svc, monkeypatch):
    """`confirmed` is a beta request confirmed by its link, stamped at join
    or before it: never a step after `joined`. Accounts made with an email
    code have none, so joined -> confirmed read 0% for every code sign-up."""
    from tests.test_careagents import _make_account
    a = _make_account(svc, monkeypatch, "a@example.com")
    _make_account(svc, monkeypatch, "b@example.com")
    funnel.mark(svc.session, a.id, "first_answer")
    conv = funnel.daily(svc.session, days=1)["conversion"]
    assert all("confirmed" not in (c["from"], c["to"]) for c in conv)
    first = conv[0]
    assert (first["from"], first["to"]) == ("joined", "first_answer")
    assert (first["from_n"], first["to_n"], first["percent"]) == (2, 1, 50)


def test_the_funnel_page_names_the_beta_request_step():
    html = (ROOT / "careagents" / "templates" / "admin_funnel.html").read_text()
    assert "confirmed a beta request" in html


# --- 7. "Saved" is in view after the tap -------------------------------------

def test_the_saved_line_is_scrolled_into_view():
    js = (STATIC / "assistant_settings.js").read_text()
    say = js[js.index("function say"):]
    say = say[:say.index("\n    }\n")]
    assert "scrollIntoView" in say
