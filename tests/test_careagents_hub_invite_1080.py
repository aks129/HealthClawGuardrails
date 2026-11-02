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
LINE = ("Invite someone to try CareAgents, free during the beta. They start "
        "with made-up records.")


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


def _left(card: str) -> str:
    return re.search(r'class="invite-used">([^<]*)<', card).group(1)


def test_a_new_account_sees_the_card_with_its_settings_link(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    card = _section(html)
    assert card is not None
    assert LINE in card
    assert _left(card) == "5 left"
    # Five slots, none filled, said in words to a screen reader.
    assert card.count('class="invite-slot"') == 5
    assert "is-used" not in card
    assert 'role="img" aria-label="0 of 5 places used"' in card
    # One Share button that copies where there is no share sheet.
    assert card.count("<button") == 1
    assert 'id="invite-share"' in card and 'data-fallback="copy"' in card
    assert "invite.js" in html
    # The long link is behind "Show link", not the card's main line.
    box = card[card.index('<details class="invite-link-box">'):]
    box = box[:box.index("</details>")]
    assert "<summary>Show link</summary>" in box
    link = re.search(r"/i\?c=[0-9a-f]{32}", box).group(0)
    assert len(re.findall(r"/i\?c=", card)) == 1
    assert link in c.get("/settings").get_data(as_text=True)


def test_the_line_promises_what_the_link_opens(made, monkeypatch):  # noqa: F811
    # The friend's page (/i?c=, beta.html) is the free beta sign-up and
    # starts on made-up records. The hub's line and the share-sheet text
    # say the same, and claim nothing about their own records.
    c, _ = _signed_in(made, monkeypatch)
    card = _section(c.get("/home").get_data(as_text=True))
    code = re.search(r"/i\?c=([0-9a-f]{32})", card).group(1)
    landing = _visible(
        c.application.test_client().get(f"/i?c={code}").get_data(as_text=True))
    assert "free during the beta" in landing and "made-up sample records" in landing
    assert "free during the beta" in LINE and "made-up records" in LINE
    share = (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
             "static" / "invite.js").read_text()
    text = re.search(r'text: "([^"]*)"', share).group(1)
    assert "free during the beta" in text and "made-up records" in text
    for claim in ("their own health assistant", "records stay theirs",
                  "own records"):
        assert claim not in card and claim not in text


@pytest.mark.parametrize("used,left,filled", [
    (0, "5 left", 0), (2, "3 left", 2), (4, "1 left", 4),
    (5, "All used", 5), (7, "All used", 5)])
def test_places_left_lead_the_card(made, monkeypatch, used, left, filled):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, used=used)
    card = _section(c.get("/home").get_data(as_text=True))
    assert _left(card) == left
    assert card.count("invite-slot is-used") == filled
    assert f'aria-label="{filled} of 5 places used"' in card
    assert ("invite-share" in card) is (used < 5)


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
    assert _left(card) == "All used"
    assert "All 5 places on your link are used." in card
    assert "invite-share" not in card


def test_a_turned_off_link_says_so_and_shows_no_link(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    with svc.session() as s:
        code = s.query(invites.InviteLink).one().code
    assert invites.revoke(svc.session, code)
    card = _section(c.get("/home").get_data(as_text=True))
    assert _left(card) == "Turned off"
    assert "This link no longer lets anyone join." in card
    assert "/i?c=" not in card and "invite-share" not in card
    assert "invite-slot" not in card


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
    r = app.test_client().get("/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    assert _section(c.get("/home").get_data(as_text=True)) is not None
    from careagents.models import Account
    with svc.session() as s:
        aid = s.query(Account).filter_by(email=EMAIL).one().id
    assert svc.delete_account(aid) is True
    r = c.get("/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")


# --- plain words ----------------------------------------------------------

def test_every_hub_section_header_has_one_plain_sentence(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    main = html[html.index('<main class="hub">'):html.index("</main>")]
    heads = list(re.finditer(r"<h2\b[^>]*>.*?</h2>", main, flags=re.S))
    assert len(heads) >= 5, heads
    for m in heads:
        after = re.sub(r"^\s*(</div>)?\s*", "", main[m.end():])
        assert after.startswith('<p class="section-line">'), m.group(0)


def test_every_section_header_in_the_template_has_its_sentence():
    # Sections a new account does not see (what's new, the sample form)
    # too: every <h2> in the hub's own markup, before its modals.
    tpl = (pathlib.Path(__file__).resolve().parents[1] / "careagents" /
           "templates" / "home.html").read_text()
    main = tpl[:tpl.index("</main>")]
    heads = re.findall(r"<h2\b[^>]*>.*?</h2>", main, flags=re.S)
    assert len(heads) >= 8, heads
    for m in re.finditer(r"<h2\b[^>]*>.*?</h2>", main, flags=re.S):
        after = re.sub(r"^\s*(</div>)?\s*", "", main[m.end():])
        assert after.startswith('<p class="section-line">'), m.group(0)


def test_the_hub_has_no_word_a_first_timer_did_not_know(made, monkeypatch):  # noqa: F811
    c, _ = _signed_in(made, monkeypatch)
    html = c.get("/home").get_data(as_text=True)
    main = html[html.index('<main class="hub">'):html.index("</main>")]
    words = _visible(main).lower()
    # The text examined is the hub's own, not an empty string.
    assert "ask your assistant" in words and "invite someone" in words
    for w in ("hub", "sources?", "wallet", "test version"):
        assert not re.search(rf"\b{w}\b", words), w
    title = re.search(r"<title>([^<]*)</title>", html).group(1)
    assert "hub" not in title.lower()


def test_every_visit_brief_button_is_explained():
    """A template check: within the card loop, the explanation sits under
    no {% if %} a card can skip. It does not see an {% if %} outside the
    loop, or the whitespace-trimming {%- if %} form."""
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


def test_at_most_twelve_slots_are_drawn(made, monkeypatch):  # noqa: F811
    # An operator can set any cap; the card draws at most 12 slots and
    # still says the count in words.
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, cap=50, used=3)
    card = _section(c.get("/home").get_data(as_text=True))
    assert card.count("invite-slot") - card.count("invite-slots") == 12
    # 3 of 50 scaled to 12 slots, rounded up: one filled, not none.
    assert card.count("invite-slot is-used") == 1
    assert _left(card) == "47 left"
    assert 'aria-label="3 of 50 places used"' in card


@pytest.mark.parametrize("cap,used,filled", [
    (20, 15, 9), (20, 19, 11), (20, 20, 12), (20, 0, 0), (12, 7, 7)])
def test_the_slots_agree_with_the_label_over_twelve(
        made, monkeypatch, cap, used, filled):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, cap=cap, used=used)
    card = _section(c.get("/home").get_data(as_text=True))
    drawn = card.count("invite-slot") - card.count("invite-slots")
    label = re.search(r'aria-label="(\d+) of (\d+) places used"', card)
    assert (int(label.group(1)), int(label.group(2))) == (used, cap)
    assert drawn == min(cap, 12)
    assert card.count("invite-slot is-used") == filled
    # The share drawn filled is the label's share to within one slot, and
    # full only when the link is.
    assert abs(filled / drawn - used / cap) <= 1 / drawn
    assert (filled == drawn) is (used >= cap)


def test_used_up_and_turned_off_say_what_settings_says(made, monkeypatch):  # noqa: F811
    c, svc = _signed_in(made, monkeypatch)
    c.get("/home")
    _set(svc, used=5)
    full = "All 5 places on your link are used."
    assert full in _section(c.get("/home").get_data(as_text=True))
    assert full in c.get("/settings").get_data(as_text=True)
    with svc.session() as s:
        code = s.query(invites.InviteLink).one().code
    assert invites.revoke(svc.session, code)
    off = "This link no longer lets anyone join."
    assert off in _section(c.get("/home").get_data(as_text=True))
    assert off in c.get("/settings").get_data(as_text=True)
