"""iMessage re-verification (#871): a binding does not last forever.

A carrier reassigns numbers. If the old holder never texted STOP, the new
holder would talk to the old owner's assistant. So a bound handle confirms
it is still its owner's: after CARE_IMESSAGE_REVERIFY_DAYS since the last
confirmation, or after 30 days with no text from it. Until then each text
gets a single-use sign-in link instead of a run, and the owner is emailed
once.

Rows are aged by writing their timestamps directly. Synthetic handles only
(555 numbers, example.com addresses).
"""

from __future__ import annotations

import re

import pytest

from careagents import imessage
from careagents.models import ImessageLink, Surface, now
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)
from tests.test_careagents_imessage import (
    HDRS, OTHER_EMAIL, PHONE, _acct_id, _confirm, _inbound, _link_token,
    _pair)

DAY = 86400.0


def _age(svc, verified_days=None, inbound_days=None, handle=PHONE):  # noqa: F811
    """Move a binding's clocks into the past."""
    with svc.session() as s:
        x = s.query(Surface).filter_by(handle=handle, kind="imessage",
                                       status="active").one()
        if verified_days is not None:
            x.verified_at = now() - verified_days * DAY
        if inbound_days is not None:
            x.last_inbound_at = now() - inbound_days * DAY


def _row(svc, handle=PHONE):  # noqa: F811
    with svc.session() as s:
        x = s.query(Surface).filter_by(handle=handle, kind="imessage",
                                       status="active").one()
        return {"verified_at": x.verified_at,
                "last_inbound_at": x.last_inbound_at,
                "reverify_notified_at": x.reverify_notified_at,
                "agent_id": x.agent_id, "account_id": x.account_id}


def _is_reverify(body: dict) -> bool:
    """A sign-in link and no run: START's words or the first-text words,
    as a stranger would get for the same text."""
    reply = body.get("reply") or ""
    url = imessage.link_url("http://localhost", _link_token(reply))
    return ("run_id" not in body
            and reply in (imessage.start_text(url), imessage.link_text(url)))


def _links(svc):  # noqa: F811
    with svc.session() as s:
        return s.query(ImessageLink).count()


# --- binding stamps ----------------------------------------------------------

def test_a_bind_stamps_verified_and_last_inbound(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    before = now()
    _pair(c, agent_id)
    row = _row(svc)
    assert row["verified_at"] >= before
    assert row["reverify_notified_at"] is None


def test_each_admitted_text_records_when_it_arrived(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=5, inbound_days=5)
    assert _inbound(c, PHONE, "hello").status_code == 202
    assert _row(svc)["last_inbound_at"] > now() - 60
    # The verification itself is not refreshed by an ordinary text.
    assert _row(svc)["verified_at"] < now() - 4 * DAY


# --- the window ----------------------------------------------------------------

def test_a_binding_older_than_the_window_gets_a_link_not_a_run(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61, inbound_days=1)
    r = _inbound(c, PHONE, "What medications am I on?")
    assert r.status_code == 200
    assert _is_reverify(r.get_json())
    # Still pending on the next text: the gate does not lift by itself.
    r2 = _inbound(c, PHONE, "hello?")
    assert r2.status_code == 200 and _is_reverify(r2.get_json())


def test_a_binding_inside_the_window_runs(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=59, inbound_days=1)
    r = _inbound(c, PHONE, "hello")
    assert r.status_code == 202 and r.get_json()["run_id"]


def test_the_window_is_configurable(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.config import Config
    assert Config(env={}).imessage_reverify_days == 60
    assert Config(env={"CARE_IMESSAGE_REVERIFY_DAYS": "10"}
                  ).imessage_reverify_days == 10
    monkeypatch.setattr(cfg, "imessage_reverify_days", 10)
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=11, inbound_days=1)
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())


def test_once_asked_only_a_confirm_lifts_it(cfg, svc, monkeypatch):  # noqa: F811
    """The owner has been told we asked. A window widened afterwards does
    not quietly answer the handle again."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61, inbound_days=1)
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())
    monkeypatch.setattr(app.extensions["careagents_imessage"],
                        "reverify_seconds", 90 * DAY)
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())


def test_a_gated_text_does_not_reset_the_silence_clock(cfg, svc, monkeypatch):  # noqa: F811
    """Whoever holds the number now cannot make it look recently used: a
    text that only got a link leaves last_inbound_at where it was. Pinned
    on its own, since the pending flag otherwise hides the clock."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=40, inbound_days=31)
    before = _row(svc)["last_inbound_at"]
    for _ in range(2):
        assert _is_reverify(_inbound(c, PHONE, "hello").get_json())
    assert _row(svc)["last_inbound_at"] == before


@pytest.mark.parametrize("raw", ["sixty", "1.5", ""])
def test_a_non_integer_window_falls_back_to_sixty(raw, caplog):
    import logging

    from careagents.config import Config
    caplog.set_level(logging.WARNING, logger="careagents.config")
    assert Config(env={"CARE_IMESSAGE_REVERIFY_DAYS": raw}
                  ).imessage_reverify_days == 60
    assert "CARE_IMESSAGE_REVERIFY_DAYS" in caplog.text


def test_a_binding_with_no_verified_time_counts_from_bound_then_fails_closed(
        cfg, svc, monkeypatch):  # noqa: F811
    """No verified_at reads as the migration would fill it: bound_at. With
    neither, the binding is due."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    with svc.session() as s:
        s.query(Surface).filter_by(handle=PHONE).update(
            {"verified_at": None, "last_inbound_at": None})
    assert _inbound(c, PHONE, "hello").status_code == 202
    with svc.session() as s:
        s.query(Surface).filter_by(handle=PHONE).update(
            {"verified_at": None, "bound_at": now() - 61 * DAY})
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())

    _pair(c, agent_id)                            # fresh, then emptied
    with svc.session() as s:
        s.query(Surface).filter_by(handle=PHONE).update(
            {"verified_at": None, "bound_at": None,
             "last_inbound_at": None})
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())


# --- long silence --------------------------------------------------------------

def test_thirty_days_of_silence_asks_inside_the_window(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=31, inbound_days=31)
    first = _inbound(c, PHONE, "hello")
    assert _is_reverify(first.get_json())
    # The stranger's own text must not reset the silence clock.
    second = _inbound(c, PHONE, "hello again")
    assert _is_reverify(second.get_json())


def test_under_thirty_days_of_silence_runs(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=29, inbound_days=29)
    assert _inbound(c, PHONE, "hello").status_code == 202


def test_a_migrated_row_without_last_inbound_counts_from_verified(
        cfg, svc, monkeypatch):  # noqa: F811
    """A row from before the column has no last text: it is measured from
    its verification, not read as silent forever."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=5)
    with svc.session() as s:
        s.query(Surface).filter_by(handle=PHONE).update(
            {"last_inbound_at": None})
    assert _inbound(c, PHONE, "hello").status_code == 202


# --- what a stale handle can and cannot do ----------------------------------

def test_stop_and_help_still_work_while_reverify_is_pending(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=90)
    assert _inbound(c, PHONE, "help").get_json() == {
        "reply": imessage.HELP_TEXT}
    assert _inbound(c, PHONE, "stop").get_json() == {
        "reply": imessage.STOP_TEXT}
    assert svc.find_surface_by_handle(PHONE) is None


@pytest.mark.parametrize("word", ["start", "approvals", "connect"])
def test_keywords_on_a_stale_handle_get_the_link(
        cfg, svc, monkeypatch, word):  # noqa: F811
    """START would say "You're connected" and APPROVALS would give the old
    owner's count: neither is for whoever holds the number now."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=90)
    assert _is_reverify(_inbound(c, PHONE, word).get_json())


def test_the_reply_is_the_strangers_sign_in_link(cfg, svc, monkeypatch):  # noqa: F811
    """The reader may be the number's new holder: the reply is word for
    word what a number we have never seen gets for START, so it says
    nothing about a previous binding (#866). The owner's email explains."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=90)
    reply = _inbound(c, PHONE, "hello").get_json()["reply"]
    stranger = _inbound(c, "+15550100199", "hello").get_json()["reply"]

    def shape(text):
        return text.replace(_link_token(text), "<token>")
    assert shape(reply) == shape(stranger)
    assert reply == imessage.link_text(
        f"http://localhost/link?t={_link_token(reply)}")
    for tell in ("while", "still", "again", "connected", agent_id,
                 "gene", "Juniper", "example.com"):
        assert tell not in reply


# --- confirming --------------------------------------------------------------

def test_reconfirming_refreshes_and_keeps_the_assistant(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, tenant, conn = _chat_app(cfg, svc, monkeypatch)
    # A second assistant, not the account's first: a reconfirm must not
    # move the phone to the first one.
    second = c.post("/api/agents", json={
        "name": "Rowan", "persona": "calm", "connection_id": conn}
    ).get_json()["id"]
    _pair(c, second)
    assert _row(svc)["agent_id"] == second
    _age(svc, verified_days=61, inbound_days=31)

    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    assert _row(svc)["reverify_notified_at"] is not None
    c.get(f"/link?t={token}")
    ask = c.get("/link/done").get_data(as_text=True)
    words = " ".join(re.sub(r"<[^>]+>", " ", ask).split())
    assert ("Is this still your phone? Texts from (555) 010-0123 can ask "
            "about your health records.") in words
    assert "Yes, it's still mine" in words and "No, it isn't" in words
    assert "Connect this phone?" not in ask
    page = _confirm(c).get_data(as_text=True)
    assert "Confirmed. This phone is still connected to CareAgents." in page
    assert "You're connected." not in page

    row = _row(svc)
    assert row["verified_at"] > now() - 60
    assert row["last_inbound_at"] > now() - 60
    assert row["reverify_notified_at"] is None
    assert row["agent_id"] == second
    nxt = _inbound(c, PHONE, "hello")
    assert nxt.status_code == 202 and nxt.get_json()["run_id"]
    assert "reply" not in nxt.get_json()          # no second welcome
    # The link was single use.
    assert app.test_client().get(f"/link?t={token}").status_code == 410


def test_no_on_the_reconfirm_page_disconnects_the_phone(cfg, svc, monkeypatch):  # noqa: F811
    """The owner says the phone is not theirs any more: the binding goes,
    as Settings' per-phone disconnect does it, and the phone's next text
    is a stranger's (a fresh link, the allowance reset)."""
    from careagents.models import ImessageHandleState
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61)
    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    c.get(f"/link?t={token}")
    assert "still your phone" in c.get("/link/done").get_data(as_text=True)
    page = c.post("/link/done", data={"connect": "no"}).get_data(
        as_text=True)
    assert ("Disconnected. Texts from that phone won't reach your "
            "assistant.") in " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert svc.find_surface_by_handle(PHONE) is None
    with svc.session() as s:
        st = s.get(ImessageHandleState, imessage.handle_key(PHONE))
        assert (st.link_count or 0) == 0           # allowance reset
    assert app.test_client().get(f"/link?t={token}").status_code == 410
    # A stranger now: the first-text link, no run.
    nxt = _inbound(c, PHONE, "hello").get_json()
    assert "run_id" not in nxt and "/link?t=" in nxt["reply"]


def test_no_on_a_first_connect_still_just_declines(cfg, svc, monkeypatch):  # noqa: F811
    """A phone not on this account: "no" binds nothing and unbinds nothing
    (another account's binding is never touched from here)."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    owner = _row(svc)["account_id"]
    _age(svc, verified_days=61)
    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    holder = app.test_client()
    _login(holder, svc, monkeypatch, email=OTHER_EMAIL)
    holder.get(f"/link?t={token}")
    page = holder.post("/link/done", data={"connect": "no"}).get_data(
        as_text=True)
    assert "we didn't connect that phone" in page
    assert "Disconnected." not in page
    assert _row(svc)["account_id"] == owner


def test_a_different_account_cannot_take_the_handle_by_the_link(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    owner_id = _row(svc)["account_id"]
    _age(svc, verified_days=61)
    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])

    new_holder = app.test_client()
    _login(new_holder, svc, monkeypatch, email=OTHER_EMAIL)
    new_holder.get(f"/link?t={token}")
    page = _confirm(new_holder).get_data(as_text=True)
    assert "You're connected." not in page

    row = _row(svc)
    assert row["account_id"] == owner_id
    assert row["account_id"] != _acct_id(svc, OTHER_EMAIL)
    assert row["verified_at"] < now() - 60 * DAY      # not refreshed
    # Still pending: the next text gets a link again, not a run.
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())


def test_a_new_code_pairing_on_the_same_account_also_reconfirms(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61)
    _inbound(c, PHONE, "hello")
    assert _pair(c, agent_id).status_code == 200
    assert _row(svc)["verified_at"] > now() - 60
    assert _inbound(c, PHONE, "hello").status_code == 202


# --- the link allowance ----------------------------------------------------------

def test_reverify_links_share_the_link_allowance(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61)
    for _ in range(imessage.LINKS_PER_WINDOW):
        assert _is_reverify(_inbound(c, PHONE, "hello").get_json())
    assert _links(svc) == imessage.LINKS_PER_WINDOW
    over = _inbound(c, PHONE, "hello")
    assert over.status_code == 200 and over.get_json() == {}
    assert _links(svc) == imessage.LINKS_PER_WINDOW
    start = _inbound(c, PHONE, "start")
    assert start.get_json() == {"reply": imessage.START_CAPPED_TEXT}
    assert _links(svc) == imessage.LINKS_PER_WINDOW


# --- the owner is told ---------------------------------------------------------

def test_the_owner_is_emailed_once_with_the_handle_masked(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents import mail
    sent = []
    monkeypatch.setattr(mail, "send_notice",
                        lambda cfg, email, subject, line:
                        sent.append((email, subject, line)) or mail.SENT)
    monkeypatch.setattr(cfg, "resend_api_key", "re_test")
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    sent.clear()                                  # the connect notice
    _age(svc, verified_days=61)
    replies = [_inbound(c, PHONE, "hello").get_json()["reply"]
               for _ in range(3)]
    assert len(sent) == 1
    email, subject, line = sent[0]
    assert email == "gene@example.com"
    assert subject == "Please confirm your phone for CareAgents texts"
    assert line == (
        "It's been a while since your phone ending in 0123 was confirmed, "
        "so before CareAgents answers more texts we sent it a sign-in "
        "link. Phone numbers sometimes change hands, and this keeps your "
        "records private. To keep texting, tap the link in that text and "
        "sign in. It takes a few seconds, and then text your question "
        "again. If you haven't texted CareAgents lately, you don't need to "
        "do anything.")
    assert PHONE not in subject + line

    # Confirmed, then stale again later: a new episode, a new email.
    c.get(f"/link?t={_link_token(replies[0])}")
    _confirm(c)
    assert len(sent) == 1        # a re-confirm is not a new connection
    sent.clear()
    _age(svc, verified_days=61)
    _inbound(c, PHONE, "hello")
    _inbound(c, PHONE, "hello")
    assert len(sent) == 1


def test_a_first_link_connect_still_reads_and_emails_as_connected(
        cfg, svc, monkeypatch):  # noqa: F811
    """Only a re-confirm is "Confirmed": a handle bound for the first time
    by the link keeps the connected page and the connect email."""
    from careagents import mail
    sent = []
    monkeypatch.setattr(mail, "send_notice",
                        lambda cfg, email, subject, line:
                        sent.append(line) or mail.SENT)
    monkeypatch.setattr(cfg, "resend_api_key", "re_test")
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(c, PHONE, "hi").get_json()["reply"])
    c.get(f"/link?t={token}")
    ask = c.get("/link/done").get_data(as_text=True)
    assert "Connect this phone?" in ask and "still your phone" not in ask
    page = _confirm(c).get_data(as_text=True)
    assert "You're connected." in page and "Confirmed." not in page
    assert len(sent) == 1 and "was connected" in sent[0]


def test_no_email_when_mail_is_not_set_up(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import mail
    sent = []
    monkeypatch.setattr(mail, "send_notice",
                        lambda *a, **k: sent.append(a) or mail.SENT)
    monkeypatch.setattr(cfg, "resend_api_key", "")
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61)
    assert _is_reverify(_inbound(c, PHONE, "hello").get_json())
    assert sent == []


def test_the_notice_masks_an_apple_id():
    line = imessage.reverify_notice("person@example.com")
    assert "person@example.com" not in line
    assert line.startswith("It's been a while since your Apple ID "
                           "p***@example.com was confirmed")


# --- the Sendblue webhook, end to end --------------------------------------------

def test_sendblue_texts_the_link_then_answers_after_confirm(
        cfg, monkeypatch):  # noqa: F811
    from careagents.accounts import AccountService
    from careagents.config import Config
    from tests import test_careagents_sendblue as sb

    sb_cfg = Config(env=sb._env(cfg))
    sb_svc = AccountService(sb_cfg)
    try:
        app, c, fake, fake_hc, agent_id = sb._app(
            sb_cfg, sb_svc, monkeypatch, reply="Your A1c is in range.")
        assert sb._pair(c, agent_id).status_code == 200
        fake.sent.clear()
        _age(sb_svc, verified_days=61, handle=sb.PHONE)

        assert sb._hook(c, "how is my a1c?", handle="m-2").status_code == 200
        assert len(fake.sent) == 1
        to, text = fake.sent[0]
        assert to == sb.PHONE
        token = _link_token(text)
        assert text == imessage.link_text(
            imessage.link_url("http://localhost", token))
        assert fake.typing == []                 # no run, so no typing
        assert sb_svc.sendblue_pending() == []   # nothing owed

        c.get(f"/link?t={token}")
        _confirm(c)
        fake.sent.clear()
        assert sb._hook(c, "how is my a1c?", handle="m-3").status_code == 200
        assert fake.typing == [sb.PHONE]         # a run was queued
        sb._run(app)
        sb._deliverer(app, fake).once()
        assert fake.sent[-1] == (sb.PHONE, "Your A1c is in range.")
    finally:
        sb_svc.engine.dispose()


def test_sendblue_withholds_an_answer_once_the_binding_is_stale(
        cfg, monkeypatch):  # noqa: F811
    """A run queued while fresh, finished after the binding went due: the
    answer is not sent to whoever holds the number now."""
    from careagents.accounts import AccountService
    from careagents.config import Config
    from careagents.models import SendblueMessage
    from tests import test_careagents_sendblue as sb

    sb_cfg = Config(env=sb._env(cfg))
    sb_svc = AccountService(sb_cfg)
    try:
        app, c, fake, fake_hc, agent_id = sb._app(
            sb_cfg, sb_svc, monkeypatch, reply="Your A1c is in range.")
        assert sb._pair(c, agent_id).status_code == 200
        assert sb._hook(c, "how is my a1c?", handle="m-2").status_code == 200
        assert fake.typing == [sb.PHONE]         # queued while fresh
        fake.sent.clear()
        sb._run(app)
        _age(sb_svc, verified_days=61, handle=sb.PHONE)
        sb._deliverer(app, fake).once()
        assert fake.sent == []
        with sb_svc.session() as s:
            assert [m.outcome for m in s.query(SendblueMessage)
                    .filter(SendblueMessage.run_id.isnot(None))] == [
                        "withheld"]
    finally:
        sb_svc.engine.dispose()


# --- the migration -----------------------------------------------------------------

def test_boot_adds_the_columns_and_backfills_verified(tmp_path):
    """A database from before #871: no verified_at / last_inbound_at /
    reverify_notified_at. The next boot adds them, and each active iMessage
    row gets verified_at = bound_at (or now when bound_at is empty) and
    last_inbound_at = now, so silence counts from the deploy rather than
    gating and emailing every existing tester that day.

    Postgres: the same path runs there. `_add_column` gives each ALTER its
    own transaction and tolerates a peer process adding it first, and the
    backfill is one UPDATE with COALESCE and a bound parameter, valid on
    both. This test covers SQLite; the Postgres lane runs the same boot.
    """
    from sqlalchemy import create_engine, inspect, text

    from careagents.models import make_engine
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as conn:
        # ca_surfaces as it shipped before #871.
        conn.execute(text(
            "CREATE TABLE ca_surfaces (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), agent_id VARCHAR(32), "
            "kind VARCHAR(16) NOT NULL, handle VARCHAR(120), "
            "status VARCHAR(16), bound_at FLOAT, code_exp FLOAT, "
            "welcome_due INTEGER DEFAULT 0)"))
        for row_id, kind, handle, status, bound in (
                ("s1", "imessage", "+15550100123", "active", 1000.0),
                ("s2", "imessage", "+15550100124", "active", None),
                ("s3", "imessage", "code123", "pending", None),
                ("s4", "telegram", "12345", "active", 2000.0)):
            conn.execute(text(
                "INSERT INTO ca_surfaces (id, kind, handle, status, "
                "bound_at) VALUES (:i, :k, :h, :s, :b)"),
                {"i": row_id, "k": kind, "h": handle, "s": status,
                 "b": bound})
    old.dispose()

    before = now()
    engine = make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("ca_surfaces")}
    assert {"verified_at", "last_inbound_at",
            "reverify_notified_at"} <= cols
    with engine.connect() as conn:
        rows = {r[0]: r[1:] for r in conn.execute(text(
            "SELECT id, verified_at, last_inbound_at, reverify_notified_at "
            "FROM ca_surfaces")).all()}
    assert rows["s1"][0] == 1000.0 and rows["s1"][1] >= before
    assert rows["s2"][0] >= before and rows["s2"][1] >= before
    assert rows["s1"][2] is None and rows["s2"][2] is None
    assert rows["s3"] == (None, None, None)      # pending: nothing to verify
    assert rows["s4"] == (None, None, None)      # not iMessage
    engine.dispose()
    make_engine(url).dispose()                   # idempotent on the next boot


def test_handle_inbound_keeps_its_shape():
    """The relay and Sendblue both read {reply?, run_id?}: the gate adds
    no new keys."""
    import inspect as pyinspect
    params = list(pyinspect.signature(imessage.handle_inbound).parameters)
    assert params == ["deps", "raw_handle", "text", "request_id",
                      "conversation_id", "transport_block"]


def test_relay_route_is_gated_too(cfg, svc, monkeypatch):  # noqa: F811
    """The Mac relay's inbound route shares the core."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=61)
    r = c.post("/api/surfaces/imessage/inbound", headers=HDRS,
               json={"handle": PHONE, "text": "hi"})
    assert set(r.get_json()) == {"reply"}
