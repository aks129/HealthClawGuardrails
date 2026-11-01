"""The hub's invite card and plain words (#1080).

The card shows the account's personal link (careagents/invites.py), the
same one Settings shows, with its count as filled slots and one Share
button. It is left out for a paused account and a confirm-link session
(#928 F6), and a signed-out or deleted one never reaches the page. A used-up
or turned-off link is said plainly, with nothing to share.

Every section header on the hub has one plain sentence under it, and the
words a first-timer didn't know are gone. The 375px view:
e2e/tests/careagents-hub-invite.spec.ts.

Synthetic data only.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from careagents import invites
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _visible, made, sent)
from tests.test_careagents_connect_journey_937 import _join_plain

pytestmark = pytest.mark.extension

EMAIL = "robin.card@example.com"
LINE = "Give someone their own health assistant. Their records stay theirs."


def _section(html: str) -> str | None:
    if 'id="invite-section"' not in html:
        return None
    part = html[html.index('id="invite-section"'):]
    return part[:part.index("</section>")]


def _signed_in(made, monkeypatch, **env):  # noqa: F811
    app, svc = made(**env)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    return c, svc


def _set(svc, **fields):
    with svc.session() as s:
        row = s.query(invites.InviteLink).one()
        for k, v in fields.items():
            setattr(row, k, v)


def test_a_new_account_sees_the_card_with_its_settings_link(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    card = _section(html)
    assert card is not None
    assert LINE in card
    assert "0 of 5 used" in card
    # Five slots, none filled.
    assert card.count('class="invite-slot"') == 5
    assert "is-used" not in card
    # One Share button that copies where there is no share sheet.
    assert card.count("<button") == 1
    assert 'id="invite-share"' in card and 'data-fallback="copy"' in card
    assert "invite.js" in html
    link = re.search(r"/i\?c=[0-9a-f]{32}", card).group(0)
    assert link in c.get("/settings").get_data(as_text=True)


def test_used_seats_are_filled_slots(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, used=2)
    card = _section(c.get("/home").get_data(as_text=True))
    assert "2 of 5 used" in card
    assert card.count("invite-slot is-used") == 2
    assert card.count('class="invite-slot"') == 3


def test_a_paused_account_has_no_card(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    assert _section(c.get("/home").get_data(as_text=True)) is not None
    svc.set_paused(EMAIL, True)
    html = c.get("/home").get_data(as_text=True)
    assert 'id="hub-paused"' in html
    assert _section(html) is None
    assert "/i?c=" not in html and "invite.js" not in html


def test_a_used_up_link_says_so_and_offers_nothing_to_share(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, used=5)
    card = _section(c.get("/home").get_data(as_text=True))
    assert "All 5 places on your link are used." in card
    assert card.count("invite-slot is-used") == 5
    assert "invite-share" not in card


def test_a_turned_off_link_says_so_and_shows_no_link(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    with svc.session() as s:
        code = s.query(invites.InviteLink).one().code
    assert invites.revoke(svc.session, code)
    card = _section(c.get("/home").get_data(as_text=True))
    assert "turned off" in card
    assert "This link no longer lets anyone join." in card
    assert "/i?c=" not in card and "invite-share" not in card


def test_a_confirm_link_session_gets_no_card_and_no_link_made(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c, _ = _join_plain(app, sent, "linkcard@example.com")
    html = c.get("/home").get_data(as_text=True)
    assert 'class="hub' in html
    assert _section(html) is None
    with svc.session() as s:
        assert s.query(invites.InviteLink).count() == 0


def test_signed_out_and_deleted_accounts_never_reach_the_card(made, monkeypatch):  # noqa: F811
    app, svc = made()
    assert app.test_client().get("/home").status_code == 302
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    assert _section(c.get("/home").get_data(as_text=True)) is not None
    from careagents.models import Account
    with svc.session() as s:
        aid = s.query(Account).filter_by(email=EMAIL).one().id
    assert svc.delete_account(aid) is True
    r = c.get("/home")
    assert r.status_code == 302
    assert "invite-section" not in r.get_data(as_text=True)


# --- plain words ----------------------------------------------------------

def test_every_hub_section_header_has_one_plain_sentence(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    main = html[html.index('<main class="hub">'):html.index("</main>")]
    heads = re.findall(r"<h2>([^<]+)</h2>", main)
    assert len(heads) >= 5, heads
    for h in heads:
        after = main[main.index(f"<h2>{h}</h2>") + len(f"<h2>{h}</h2>"):]
        after = re.sub(r"^\s*(</div>)?\s*", "", after)
        assert after.startswith('<p class="section-line">'), h


def test_every_section_header_in_the_template_has_its_sentence():
    # Sections a new account does not see (what's new, the sample form)
    # too: every <h2> in the hub's own markup, before its modals.
    tpl = (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
           "templates" / "home.html").read_text()
    main = tpl[:tpl.index("</main>")]
    heads = re.findall(r"<h2>.*?</h2>", main)
    assert len(heads) >= 8, heads
    for m in re.finditer(r"<h2>.*?</h2>", main):
        after = re.sub(r"^\s*(</div>)?\s*", "", main[m.end():])
        assert after.startswith('<p class="section-line">'), m.group(0)


def test_the_hub_has_no_word_a_first_timer_did_not_know(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    main = html[html.index('<main class="hub">'):html.index("</main>")]
    words = _visible(main).lower()
    for w in ("hub", "sources?", "wallet", "test version"):
        assert not re.search(rf"\b{w}\b", words), w
    title = re.search(r"<title>([^<]*)</title>", html).group(1)
    assert "hub" not in title.lower()


def test_every_visit_brief_button_is_explained():
    tpl = (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
           "templates" / "home.html").read_text()
    card = tpl[tpl.index("{% for a in people_view.assistants %}"):
               tpl.index("{% endfor %}", tpl.index("people_view.assistants"))]
    gloss = card.index("Visit brief: a one-page summary")
    # Not inside an {% if %} that a card can skip.
    before = card[:gloss]
    assert before.count("{% if") == before.count("{% endif %}")


def test_hub_redirects_to_home(made):  # noqa: F811
    app, _ = made()
    r = app.test_client().get("/hub")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/home")


def test_landing_and_404_name_where_the_button_goes(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    landing = c.get("/").get_data(as_text=True)
    assert "Go to your home page" in landing and "hub" not in _visible(landing).lower()
    missing = c.get("/no-such-page").get_data(as_text=True)
    assert "Back to your home page" in missing
    assert "Back to your assistant" not in missing
