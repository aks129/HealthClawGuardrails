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
    reply = body.get("reply") or ""
    return ("run_id" not in body
            and reply == imessage.reverify_text(
                imessage.link_url("http://localhost", _link_token(reply))))


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


def test_the_reply_names_nothing_about_the_account(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc, verified_days=90)
    reply = _inbound(c, PHONE, "hello").get_json()["reply"]
    assert reply == (
        "It's been a while. Tap this link to confirm this is still your "
        f"phone: http://localhost/link?t={_link_token(reply)} The link "
        "works once, for 30 minutes.")
    for secret in (agent_id, "gene", "Juniper", "example.com"):
        assert secret not in reply


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
    assert "You're connected." in _confirm(c).get_data(as_text=True)

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
    assert line == ("We asked the phone ending in 0123 to confirm it's "
                    "still yours before answering texts.")
    assert PHONE not in subject + line

    # Confirmed, then stale again later: a new episode, a new email.
    c.get(f"/link?t={_link_token(replies[0])}")
    _confirm(c)
    sent.clear()                                  # the connect notice
    _age(svc, verified_days=61)
    _inbound(c, PHONE, "hello")
    _inbound(c, PHONE, "hello")
    assert len(sent) == 1


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
    assert "p***@example.com" in line


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
        assert text == imessage.reverify_text(
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


# --- the migration -----------------------------------------------------------------

def test_boot_adds_the_columns_and_backfills_verified(tmp_path):
    """A database from before #871: no verified_at / last_inbound_at /
    reverify_notified_at. The next boot adds them, and each active iMessage
    row gets verified_at = bound_at, or now when bound_at is empty.

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
    assert rows["s1"] == (1000.0, None, None)
    assert rows["s2"][0] >= before and rows["s2"][1:] == (None, None)
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
