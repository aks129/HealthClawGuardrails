"""Re-verify of #920 fixes (head bbbd0d4): a confirm-link session against
every connection state, the routes that outlast a session, and the race
with the first real connection. Synthetic data only."""
# ruff: noqa: F811
from __future__ import annotations

import pytest

from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    made, sent)
from tests.test_careagents_first_run import _account, _ask, _signed_in_as

EMAIL = "avery@example.com"
CANARY = "Canary Clinic Zephyrine Q7"


def _link_session(app, sent):
    c = app.test_client()
    token, nonce, _ = _ask(c, sent)
    assert c.post("/beta/confirm", data={"t": token, "n": nonce}
                  ).status_code == 303
    return c


@pytest.mark.parametrize("status", ["active", "pending", "failed", "error",
                                    "disconnected", "paused"])
def test_any_real_connection_state_ends_the_link_session(made, sent, status):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, sent)
    acct = _account(svc).id
    svc.add_connection(acct, "fasten", "t-canary", CANARY, status=status,
                       consent_version="v-test")
    r = c.get("/home")
    assert r.status_code == 302 and _signed_in_as(c) is None
    assert CANARY not in r.get_data(as_text=True)


@pytest.mark.parametrize("method,path,body", [
    ("post", "/webauthn/register/options", None),
    ("post", "/webauthn/register/verify", {}),
    ("post", "/webauthn/consent/options", None),
    ("post", "/authorize/decide", {}),
    ("post", "/api/surfaces/telegram", {}),
    ("post", "/api/surfaces/imessage", {"handle": "+15550100000"}),
    ("post", "/api/connections/fasten", {}),
    ("post", "/api/connections/direct", {}),
    ("post", "/api/connections/smart", {}),
    ("get", "/link/done", None),
])
def test_a_link_session_cannot_widen_itself(made, sent, method, path, body):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _link_session(app, sent)
    r = getattr(c, method)(path, json=body)
    assert r.status_code in (302, 403), (path, r.status_code)
    with svc.session() as s:
        from careagents.models import Connection, Passkey, Surface
        acct = _account(svc).id
        assert s.query(Passkey).filter_by(account_id=acct).count() == 0
        assert s.query(Surface).filter_by(account_id=acct).count() == 0
        assert [x.kind for x in s.query(Connection)
                .filter_by(account_id=acct)] == ["sample"]


def test_the_link_session_is_not_permanent(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = _link_session(app, sent)
    with c.session_transaction() as s:
        assert s.permanent is False and s.get("via_link") is True


def test_a_code_sign_in_elsewhere_then_real_records_ends_the_link(
        made, sent, monkeypatch):
    """The race as it plays out: the owner signs in with a code in another
    browser and starts a real connection; the link browser's next request,
    of any kind, is signed out before it reads anything."""
    app, svc = made(RESEND_API_KEY="re_test")
    stranger = _link_session(app, sent)
    owner = app.test_client()
    _login(owner, svc, monkeypatch, email=EMAIL)
    svc.add_connection(_account(svc).id, "fasten", "t-canary", CANARY,
                       status="pending", consent_version="v-test")
    for path in ("/api/approvals/count", "/api/labs/timeline", "/brief",
                 "/settings", "/api/connections/catalog"):
        r = stranger.get(path)
        assert r.status_code in (302, 401), (path, r.status_code)
        assert CANARY not in r.get_data(as_text=True)
    # The owner's own session is unaffected.
    assert owner.get("/home").status_code == 200
