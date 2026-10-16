"""A lost beta confirmation email can be asked for again (patient tester
walk, 2026-10-16): only a send that did not fail spends the mailbox's
email, a re-send waits CONFIRM_RESEND seconds, a mailbox gets at most
CONFIRM_DAILY a day, and the form says truthfully what happened. Plus the
invite email that names its inviter.

Synthetic data only.
"""
# ruff: noqa: F811  (pytest fixtures imported by name)

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import beta_signup, mail, models
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _confirm_token, _join, _post, _row, _rows, made, sent)


def _age(svc, seconds):
    """As if every confirmation email went out `seconds` earlier."""
    with svc.session() as s:
        for cap in s.query(beta_signup.BetaMailCap):
            cap.sent_at -= seconds
            if cap.sends is not None:
                cap.sends = " ".join(repr(float(x) - seconds)
                                     for x in cap.sends.split())
            if cap.already_at is not None:
                cap.already_at -= seconds


@pytest.fixture
def failing(monkeypatch, sent):
    """Make the provider fail the next sends: `failing.mode` is None (send),
    "refused" (never reached: NOT_SENT) or "timeout" (UNCONFIRMED)."""
    real = mail.requests.post

    class Switch:
        mode = None

    def post(*a, **k):
        if Switch.mode == "refused":
            raise mail.requests.ConnectionError("down")
        if Switch.mode == "timeout":
            raise mail.requests.Timeout("slow")
        return real(*a, **k)
    monkeypatch.setattr(mail.requests, "post", post)
    return Switch


# --- the quota is spent only on a send that did not fail -------------------

def test_a_refused_send_says_so_and_can_be_asked_again_at_once(
        made, sent, failing):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    failing.mode = "refused"
    r = _post(c)
    assert r.status_code == 502
    assert r.get_json() == {"error": "not_sent", "sent": False}
    assert sent == []
    failing.mode = None
    r = _post(c, ip="198.51.100.2")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "sent": True}
    (to, subject, _, text_body), = sent
    assert to == "avery@example.com"
    c.post("/beta/confirm", data={"t": _confirm_token(text_body)})
    assert _row(svc)["status"] == "new"


def test_a_refused_send_leaves_the_daily_count_alone(made, sent, failing):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    failing.mode = "refused"
    for i in range(beta_signup.CONFIRM_DAILY + 2):
        assert _post(c, ip=f"198.51.100.{i}").status_code == 502
    with svc.session() as s:
        (cap,) = s.query(beta_signup.BetaMailCap).all()
        assert cap.day_count == 0


def test_an_unconfirmed_send_keeps_its_claim(made, sent, failing):
    """It may be in the inbox already (#220): the wait applies."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    failing.mode = "timeout"
    r = _post(c)
    assert r.status_code == 202
    assert r.get_json() == {"sent": None,
                            "retry_after": beta_signup.CONFIRM_RESEND}
    failing.mode = None
    r = _post(c, ip="198.51.100.2")
    assert r.status_code == 200 and r.get_json()["sent"] is False
    assert sent == []


# --- a re-send after a short wait, and a daily cap -------------------------

def test_asking_again_at_once_says_when_a_new_one_can_come(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    assert _post(c).get_json() == {"ok": True, "sent": True}
    r = _post(c, ip="198.51.100.2")
    assert r.status_code == 200
    body = r.get_json()
    assert body["sent"] is False and "ok" not in body
    assert 0 < body["retry_after"] <= beta_signup.CONFIRM_RESEND
    assert len(sent) == 1
    _age(svc, beta_signup.CONFIRM_RESEND)
    assert _post(c, ip="198.51.100.3").get_json() == {"ok": True,
                                                       "sent": True}
    assert len(sent) == 2
    # The newer link works; the one it replaced does not.
    assert c.post("/beta/confirm", data={
        "t": _confirm_token(sent[0][3])}).status_code == 410
    assert c.post("/beta/confirm", data={
        "t": _confirm_token(sent[1][3])}).status_code == 200


def test_a_mailbox_gets_at_most_the_daily_cap(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    for i in range(beta_signup.CONFIRM_DAILY):
        # Aliases of one inbox share its cap.
        r = _post(c, ip=f"198.51.100.{i}", email=f"avery+{i}@example.com")
        assert r.get_json() == {"ok": True, "sent": True}, i
        _age(svc, beta_signup.CONFIRM_RESEND)
    r = _post(c, ip="198.51.100.99")
    body = r.get_json()
    assert body["sent"] is False
    assert body["retry_after"] > beta_signup.CONFIRM_CAP / 2
    assert len(sent) == beta_signup.CONFIRM_DAILY


def test_rejoining_the_same_day_after_deleting_the_account_sends(
        made, sent, monkeypatch):
    """The cap outlives the request, so it used to swallow the rejoin."""
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    c = app.test_client()
    _login(c, svc, monkeypatch, email="avery@example.com")
    assert c.post("/api/account/delete",
                  json={"confirm": "DELETE"}).status_code == 200
    assert _rows(svc) == []
    _age(svc, beta_signup.CONFIRM_RESEND)
    sent.clear()
    r = _post(app.test_client(), ip="198.51.100.5")
    assert r.get_json() == {"ok": True, "sent": True}
    (to, subject, _, _), = sent
    assert subject.startswith("Confirm you asked to test CareAgents")
    assert _row(svc)["status"] == "pending"


# --- the invite email names its inviter -------------------------------------

def _code(client) -> str:
    html = client.get("/settings").get_data(as_text=True)
    return re.search(r"/i\?c=([0-9a-f]{32})", html).group(1)


def test_the_invite_email_names_the_inviter_by_first_name_only(
        made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, email="robin.inviter@example.com", first_name="Robin")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="robin.inviter@example.com")
    code = _code(c)
    sent.clear()
    r = _post(app.test_client(), ip="198.51.100.6",
              email="sam@example.com", first_name="Sam", invite=code)
    assert r.get_json() == {"ok": True, "sent": True}
    (to, subject, html, text_body), = sent
    assert subject.startswith("Robin invited you to try CareAgents")
    assert "robin.inviter" not in subject + html + text_body
    assert text_body.startswith("Robin invited you to try CareAgents.")


# --- the new columns reach an existing database -----------------------------

@pytest.fixture
def url(tmp_path):
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if env and not env.startswith("sqlite"):
        engine = create_engine(env)
        models.Base.metadata.drop_all(engine)
        engine.dispose()
        return env
    return f"sqlite:///{tmp_path / 'legacy.db'}"


def test_a_legacy_cap_row_gains_the_count_and_reads_as_one_email(url):
    legacy = create_engine(url)
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_beta_mail_caps (email_hash VARCHAR(64) "
            "PRIMARY KEY, sent_at FLOAT NOT NULL)"))
        conn.execute(text(
            "INSERT INTO ca_beta_mail_caps VALUES (:k, 1000000.0)"),
            {"k": beta_signup.hash_token("a@example.com")})
    engine = models.make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns(
        "ca_beta_mail_caps")}
    assert {"sends", "already_at"} <= cols
    session_factory = models.make_session_factory(engine)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = session_factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    t0 = 1_000_000.0
    assert beta_signup.claim_mail(scope, "a@example.com", t0 + 1) > 0
    assert beta_signup.claim_mail(
        scope, "a@example.com", t0 + beta_signup.CONFIRM_RESEND) == 0
    with scope() as s:
        cap = s.get(beta_signup.BetaMailCap,
                    beta_signup.hash_token("a@example.com"))
        assert beta_signup.send_times(cap) == [
            t0, t0 + beta_signup.CONFIRM_RESEND]
    engine.dispose()
    legacy.dispose()
