"""Tester check-in emails on day 2 and day 7 (careagents/checkins.py).

Timing, one email per account per kind across racing workers, the stop
link, deleted accounts, the CC, the flag, and no address in any log.
Synthetic data only.
"""

from __future__ import annotations

import logging
import threading

import pytest
from sqlalchemy import Float, String, update

from careagents import checkins, funnel, mail
from careagents.models import Account, Base, now
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _make_account, cfg, svc)

DAY = 86400
OWNER = "owner@example.com"


def _cfg(svc, **env):  # noqa: F811
    """A Config on the `svc` fixture's database, flag on, the owner set."""
    from careagents.config import Config
    base = {"CARE_DATABASE_URL": svc.cfg.database_url,
            "CARE_RP_ID": "localhost", "CARE_ORIGIN": "http://localhost",
            "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "mint-secret",
            "CARE_SESSION_SECRET": "s" * 40, "RESEND_API_KEY": "re_x",
            "CARE_ALLOW_REAL_MAIL": "1", "CARE_CHECKIN_EMAILS": "1",
            "CARE_OWNER_EMAILS": OWNER}
    base.update(env)
    return Config(env={k: v for k, v in base.items() if v is not None})


def _aged(svc, monkeypatch, email, days):  # noqa: F811
    acct = _make_account(svc, monkeypatch, email)
    with svc.session() as s:
        s.execute(update(Account).where(Account.id == acct.id)
                  .values(created_at=now() - days * DAY))
    return acct.id


@pytest.fixture
def sent(monkeypatch):
    out = []

    def fake_send(_cfg, to, subject, html, text, reply_to=None, cc=None):
        out.append({"to": to, "subject": subject, "html": html,
                    "text": text, "reply_to": reply_to, "cc": cc})
        return mail.SENT
    monkeypatch.setattr(mail, "send_message", fake_send)
    return out


def _rows(svc):  # noqa: F811
    with svc.session() as s:
        return {(r.account_id, r.kind)
                for r in s.query(checkins.CheckinMail).all()}


# --- the table ----------------------------------------------------------------

def test_the_table_holds_an_id_a_kind_and_a_time_only():
    table = Base.metadata.tables["ca_checkin_mail"]
    assert {c.name: type(c.type) for c in table.columns} == {
        "account_id": String, "kind": String, "at": Float}
    assert all(c.type.length <= 32 for c in table.columns
               if isinstance(c.type, String))


# --- timing -------------------------------------------------------------------

@pytest.mark.parametrize("days,kinds", [
    (0, []), (1.9, []), (2.0, ["checkin_day2"]), (4.9, ["checkin_day2"]),
    (5.1, []), (6.9, []), (7.0, ["checkin_day7"]), (9.9, ["checkin_day7"]),
    (10.1, []), (40, [])])
def test_each_email_is_due_only_in_its_window(svc, monkeypatch, days,  # noqa: F811
                                              kinds):
    _aged(svc, monkeypatch, "t@example.com", days)
    assert [r["kind"] for r in checkins.due(svc.session)] == kinds


def test_day_two_then_day_seven(svc, monkeypatch, sent):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 2.5)
    cfg_ = _cfg(svc)
    assert checkins.send_due(svc.session, cfg_)["sent"] == 1
    assert "screen=checkin_day2" in sent[0]["text"]
    # Five days on: the day-7 email, and still only one day-2.
    later = now() + 5 * DAY
    assert checkins.send_due(svc.session, cfg_, at=later)["sent"] == 1
    assert "screen=checkin_day7" in sent[1]["text"]
    assert checkins.send_due(svc.session, cfg_, at=later)["sent"] == 0
    assert _rows(svc) == {(aid, "checkin_day2"), (aid, "checkin_day7")}


# --- once per account per kind ------------------------------------------------

def test_a_second_tick_sends_nothing(svc, monkeypatch, sent):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    cfg_ = _cfg(svc)
    checkins.send_due(svc.session, cfg_)
    checkins.send_due(svc.session, cfg_)
    assert len(sent) == 1
    # And what was sent is no longer due, so the preview does not list it.
    assert checkins.due(svc.session) == []


def test_racing_workers_send_each_email_once(tmp_path, monkeypatch):
    import os

    from careagents.accounts import AccountService
    from careagents.config import Config
    from careagents.models import make_engine
    # A file, not :memory:, so the six engines share one database; on the
    # Postgres lane, that database from a clean schema.
    url = os.environ.get("CARE_TEST_DATABASE_URL",
                         f"sqlite:///{tmp_path}/c.db")
    if not url.startswith("sqlite"):
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    env = {"CARE_DATABASE_URL": url,
           "OPENAI_API_KEY": "k", "CARE_SESSION_SECRET": "s" * 40,
           "CARE_CHECKIN_EMAILS": "1", "CARE_OWNER_EMAILS": OWNER}
    cfg_ = Config(env=env)
    # Each worker its own engine and pool, as two processes would have.
    services = [AccountService(Config(env=env)) for _ in range(6)]
    ids = [_aged(services[0], monkeypatch, f"r{i}@example.com", 3)
           for i in range(4)]
    sent, lock, start = [], threading.Lock(), threading.Barrier(6)

    def fake_send(_c, to, *a, **k):
        with lock:
            sent.append(to)
        return mail.SENT
    monkeypatch.setattr(mail, "send_message", fake_send)

    def tick(svc_):
        start.wait()
        checkins.send_due(svc_.session, cfg_)
    try:
        threads = [threading.Thread(target=tick, args=(s,)) for s in services]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(sent) == sorted(f"r{i}@example.com" for i in range(4))
        assert len(_rows(services[0])) == len(ids)
    finally:
        for s in services:
            s.engine.dispose()


def test_a_refused_send_is_counted_and_tried_again(svc, monkeypatch):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    outcomes = [mail.NOT_SENT, mail.SENT]
    monkeypatch.setattr(mail, "send_message", lambda *a, **k: outcomes.pop(0))
    cfg_ = _cfg(svc)
    assert checkins.send_due(svc.session, cfg_)["not_sent"] == 1
    assert _rows(svc) == set()
    assert funnel.daily(svc.session, days=1)["days"][0]["mail_failed"] == 1
    assert checkins.send_due(svc.session, cfg_)["sent"] == 1
    assert _rows(svc) == {(aid, "checkin_day2")}


def test_a_send_that_raised_is_given_back(svc, monkeypatch):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)

    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(mail, "send_message", boom)
    assert checkins.send_due(svc.session, _cfg(svc))["not_sent"] == 1
    assert _rows(svc) == set()


def test_an_unconfirmed_send_is_never_sent_again(svc, monkeypatch):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    calls = []
    monkeypatch.setattr(mail, "send_message",
                        lambda *a, **k: calls.append(1) or mail.UNCONFIRMED)
    cfg_ = _cfg(svc)
    checkins.send_due(svc.session, cfg_)
    checkins.send_due(svc.session, cfg_)
    assert len(calls) == 1


# --- opting out ---------------------------------------------------------------

def _app(svc, cfg_):  # noqa: F811
    from careagents.app import create_app
    from tests.test_careagents import FakeClient
    app = create_app(config=cfg_, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    return app


def _stop_url(text):
    return next(w for w in text.split() if "/emails/stop?t=" in w)


def test_the_stop_link_stops_both_emails(svc, monkeypatch, sent):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    cfg_ = _cfg(svc)
    checkins.send_due(svc.session, cfg_)
    token = _stop_url(sent[0]["text"]).split("t=", 1)[1]
    c = _app(svc, cfg_).test_client()
    # Opening the link (a mail scanner, or the person) changes nothing.
    r = c.get(f"/emails/stop?t={token}")
    assert r.status_code == 200 and "Stop these emails" in r.get_data(
        as_text=True)
    assert (aid, checkins.STOPPED) not in _rows(svc)
    r = c.post("/emails/stop", data={"t": token})
    assert r.status_code == 200 and "Done" in r.get_data(as_text=True)
    # A second tap is the same answer.
    assert c.post("/emails/stop", data={"t": token}).status_code == 200
    assert checkins.due(svc.session, at=now() + 5 * DAY) == []
    assert checkins.send_due(svc.session, cfg_, at=now() + 5 * DAY)[
        "sent"] == 0
    assert len(sent) == 1


def test_an_opt_out_before_day_two_sends_nothing(svc, monkeypatch, sent):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    assert checkins.opt_out(svc.session, aid)
    checkins.send_due(svc.session, _cfg(svc))
    assert sent == []


def test_an_opt_out_after_the_list_is_read_still_wins(svc, monkeypatch,  # noqa: F811
                                                      sent):
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    real_due = checkins.due

    def due_then_stop(*a, **k):
        rows = real_due(*a, **k)
        checkins.opt_out(svc.session, aid)
        return rows
    monkeypatch.setattr(checkins, "due", due_then_stop)
    assert checkins.send_due(svc.session, _cfg(svc))["skipped"] == 1
    assert sent == [] and _rows(svc) == {(aid, checkins.STOPPED)}


@pytest.mark.parametrize("mangle", [
    lambda t: "", lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),
    lambda t: t.split(".")[0], lambda t: "acct_other." + t.split(".")[1],
    lambda t: t + "x"])
def test_a_bad_stop_token_stops_nothing(svc, monkeypatch, mangle):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    cfg_ = _cfg(svc)
    bad = mangle(checkins.stop_token(cfg_, aid))
    c = _app(svc, cfg_).test_client()
    assert c.get(f"/emails/stop?t={bad}").status_code == 410
    assert c.post("/emails/stop", data={"t": bad}).status_code == 410
    assert _rows(svc) == set()


def test_a_token_from_another_secret_stops_nothing(svc, monkeypatch):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    other = checkins.stop_token(_cfg(svc, CARE_SESSION_SECRET="o" * 40), aid)
    assert checkins.read_token(_cfg(svc), other) is None


# --- deleted accounts ---------------------------------------------------------

def test_a_deleted_account_gets_nothing_and_leaves_no_row(svc, monkeypatch,  # noqa: F811
                                                          sent):
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    keep = _aged(svc, monkeypatch, "k@example.com", 3)
    checkins.opt_out(svc.session, aid)
    assert svc.delete_account(aid)
    assert _rows(svc) == set()
    checkins.send_due(svc.session, _cfg(svc))
    assert [m["to"] for m in sent] == ["k@example.com"]
    assert _rows(svc) == {(keep, "checkin_day2")}


def test_an_account_deleted_after_the_list_is_read_is_skipped(
        svc, monkeypatch, sent):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    real_due = checkins.due

    def due_then_delete(*a, **k):
        rows = real_due(*a, **k)
        svc.delete_account(aid)
        return rows
    monkeypatch.setattr(checkins, "due", due_then_delete)
    assert checkins.send_due(svc.session, _cfg(svc))["skipped"] == 1
    assert sent == [] and _rows(svc) == set()
    assert not checkins.opt_out(svc.session, aid)


# --- the CC and reply-to ------------------------------------------------------

def test_the_owner_is_ccd_and_replies_go_to_the_team(svc, monkeypatch, sent):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    checkins.send_due(svc.session, _cfg(svc))
    m = sent[0]
    assert m["to"] == "t@example.com" and m["cc"] == [OWNER]
    assert m["reply_to"] == "contactus@healthclaw.io"


def test_the_checkin_cc_setting_wins(svc, monkeypatch, sent):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    checkins.send_due(svc.session, _cfg(
        svc, CARE_CHECKIN_CC="b@example.com, A@example.com"))
    assert sent[0]["cc"] == ["a@example.com", "b@example.com"]


def test_no_cc_address_means_no_email(svc, monkeypatch, sent):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    checkins.send_due(svc.session, _cfg(svc, CARE_OWNER_EMAILS=None))
    assert sent == [] and _rows(svc) == set()


def test_the_owners_own_account_is_not_ccd_to_itself(svc, monkeypatch,  # noqa: F811
                                                     sent):
    _aged(svc, monkeypatch, OWNER, 3)
    checkins.send_due(svc.session, _cfg(svc))
    assert sent[0]["to"] == OWNER and sent[0]["cc"] == []


def test_resend_gets_the_cc_and_reply_to(svc, monkeypatch):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    posted = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append(json)

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    checkins.send_due(svc.session, _cfg(svc))
    assert posted[0]["to"] == ["t@example.com"]
    assert posted[0]["cc"] == [OWNER]
    assert posted[0]["reply_to"] == "contactus@healthclaw.io"
    assert posted[0]["subject"] == "How's it going? — CareAgents"


def test_the_email_says_the_approved_words_and_nothing_about_the_account(
        svc, monkeypatch, sent):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    checkins.send_due(svc.session, _cfg(svc))
    m = sent[0]
    for part in (m["text"], m["html"]):
        assert "How's it going? What's confusing?" in part.replace(
            "&#39;", "'")
        assert "tap here to tell us" in part
        assert "http://localhost/feedback?screen=checkin_day2" in part
        assert "t@example.com" not in part
    # The id appears only inside the signed stop link.
    assert m["text"].count(aid) == 1 and aid in _stop_url(m["text"])


# --- the flag -----------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "0", "off"])
def test_the_flag_off_sends_nothing(svc, monkeypatch, sent, value):  # noqa: F811
    _aged(svc, monkeypatch, "t@example.com", 3)
    cfg_ = _cfg(svc, CARE_CHECKIN_EMAILS=value)
    assert not cfg_.checkin_emails
    checkins.send_due(svc.session, cfg_)
    assert sent == [] and _rows(svc) == set()


def test_the_worker_starts_the_thread_only_with_the_flag(svc, monkeypatch):  # noqa: F811
    from careagents import worker
    started = []
    monkeypatch.setattr(worker.threading.Thread, "start",
                        lambda self: started.append(self.name))
    monkeypatch.setattr(worker.threading.Thread, "join", lambda self: None)
    for value, expect in ((None, False), ("1", True)):
        started.clear()
        cfg_ = _cfg(svc, CARE_CHECKIN_EMAILS=value)
        worker.run_worker_pool(cfg_, threading.Event())
        assert ("careagents-checkins" in started) is expect


# --- no addresses in logs, the preview ----------------------------------------

def test_no_address_reaches_a_log(svc, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    for i, outcome in enumerate((mail.SENT, mail.NOT_SENT, mail.UNCONFIRMED,
                                 "raise")):
        _aged(svc, monkeypatch, f"addr{i}@example.com", 3)
    outcomes = [mail.SENT, mail.NOT_SENT, mail.UNCONFIRMED, "raise"]

    def fake_post(url, headers=None, json=None, timeout=None):
        o = outcomes.pop(0)
        if o == "raise":
            raise mail.requests.ConnectionError("to addr3@example.com")

        class R:
            status_code = {mail.SENT: 200, mail.NOT_SENT: 422,
                           mail.UNCONFIRMED: 200}[o]
        if o == mail.UNCONFIRMED:
            raise mail.requests.ReadTimeout("addr2@example.com")
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda *_: stop.set())
    checkins.run(svc.session, _cfg(svc), stop)
    assert "checkins:" in caplog.text
    assert "@" not in caplog.text


def test_the_preview_prints_ids_and_emails_only_when_asked(svc, monkeypatch):  # noqa: F811
    aid = _aged(svc, monkeypatch, "t@example.com", 3)
    runner = _app(svc, _cfg(svc)).test_cli_runner()
    out = runner.invoke(args=["checkins", "preview"]).output
    assert f"{aid}  checkin_day2" in out
    assert "@" not in out and "CARE_CHECKIN_EMAILS: on" in out
    out = runner.invoke(args=["checkins", "preview", "--show-emails"]).output
    assert f"{aid}  checkin_day2  t@example.com" in out and OWNER in out
    # A preview sends nothing and claims nothing.
    assert _rows(svc) == set()
