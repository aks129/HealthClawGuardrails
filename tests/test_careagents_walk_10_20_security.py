"""Security pins for PR #1010: the code mail's wording now turns on the account.

start_email_code reads ca_accounts to pick the wording, and stores "login"
for an address that has one. That is safe only while:

1. every answer of /api/auth/email, failure answers included, is the same
   bytes and headers with or without an account;
2. /api/auth/verify says `new_account` only after a correct code, and its
   refusal is the same either way;
3. EmailToken.purpose is wording, never an authorization input: a code
   stored before the deploy ("verify" for an existing account), or one whose
   account state changed after it was minted, signs in as any code does.

Synthetic data only.
"""
# ruff: noqa: F811  (pytest fixtures imported by name)

from __future__ import annotations

import pytest

from careagents import accounts, mail
from careagents.beta_signup import mailbox
from careagents.models import Account, EmailToken
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _past_the_resend_wait, app, cfg, svc)

MEMBER = "member.1010@synthetic.example"
STRANGER = "stranger.1010@synthetic.example"


def _make_member(svc):
    with svc.session() as s:
        s.add(Account(email=MEMBER))


def _answer(r):
    hdrs = {k: v for k, v in r.headers.items() if k.lower() != "date"}
    return r.status_code, r.get_data(), hdrs


@pytest.mark.parametrize("outcome", [mail.SENT, mail.UNCONFIRMED,
                                     mail.NOT_SENT])
def test_every_code_request_answer_is_the_same_bytes_and_headers(
        app, svc, monkeypatch, outcome):
    _make_member(svc)
    purposes = []
    monkeypatch.setattr(
        mail, "send_code",
        lambda cfg, e, code, purpose: purposes.append(purpose) or outcome)
    answers = []
    for email in (MEMBER, STRANGER):
        _past_the_resend_wait(svc, email)
        first = app.test_client().post("/api/auth/email",
                                       json={"email": email})
        again = app.test_client().post("/api/auth/email",
                                       json={"email": email})
        answers.append((_answer(first), _answer(again)))
    # The wording did differ, so the account was looked at...
    assert "login" in purposes and "verify" in purposes
    # ...and nothing of it reached the answer.
    assert answers[0] == answers[1]


def test_a_wrong_code_says_nothing_about_the_account(app, svc, monkeypatch):
    _make_member(svc)
    monkeypatch.setattr(mail, "send_code", lambda *a: mail.SENT)
    refusals = []
    for email in (MEMBER, STRANGER):
        c = app.test_client()
        assert c.post("/api/auth/email", json={"email": email}).status_code \
            == 200
        r = c.post("/api/auth/verify",
                   json={"email": email, "code": "not-a-code"})
        assert r.status_code == 400
        assert "new_account" not in r.get_json()
        refusals.append(_answer(r))
    assert refusals[0] == refusals[1]


def _plant(svc, email, purpose, code="24681357"):
    """A live code as start_email_code leaves one, with a chosen purpose."""
    with svc.session() as s:
        s.add(EmailToken(email=email, code_hash=svc._hash(code),
                         purpose=purpose,
                         exp=accounts.now() + accounts.CODE_TTL,
                         mailbox=mailbox(email)))
    return code


def test_a_verify_purpose_code_from_before_the_deploy_signs_a_member_in(
        app, svc):
    _make_member(svc)
    code = _plant(svc, MEMBER, "verify")
    r = app.test_client().post("/api/auth/verify",
                               json={"email": MEMBER, "code": code})
    assert r.status_code == 200
    assert r.get_json()["new_account"] is False
    with svc.session() as s:
        assert s.query(Account).filter_by(email=MEMBER).count() == 1


def test_a_login_purpose_code_whose_account_is_gone_makes_one_and_says_so(
        app, svc):
    code = _plant(svc, STRANGER, "login")
    r = app.test_client().post("/api/auth/verify",
                               json={"email": STRANGER, "code": code})
    assert r.status_code == 200
    assert r.get_json()["new_account"] is True


def test_purpose_is_read_only_for_the_mail_wording():
    """No code path may gate on EmailToken.purpose: it now differs by account
    state, so a gate on it would be an account oracle or a lockout."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "careagents"
    readers = []
    for path in root.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ".purpose" in line and "EmailToken" not in line \
                    and "checkin" not in line:
                readers.append(f"{path.name}:{n}: {line.strip()}")
    assert readers == [], readers
