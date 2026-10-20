"""Security review of #928: exploit tests for personal invite links.

Each test asserts the safe behaviour. A test marked xfail(strict=True) pins
a finding that is still open: it goes red the day the finding is fixed, and
the marker comes off then. Synthetic data only.
"""

from __future__ import annotations


import pytest

from careagents import invites, tester_terms
from careagents.models import RealRecordInvite
from tests.test_careagents import _login
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _age_caps, _post, made, sent)
from tests.test_careagents_invite_links import (_account_id, _code,
                                                _invite_row, _join_via)

CONSENT = {"consent": True}


def _active_invites(svc) -> int:
    with svc.session() as s:
        return (s.query(RealRecordInvite)
                .filter(RealRecordInvite.revoked_at.is_(None)).count())


# The stage 1 cap under racing joins is pinned for real in
# tests/test_careagents_invite_cap_race_r2.py: an earlier pin here granted
# nothing (its inviter had no real-record access) and passed vacuously.


def test_a_peer_link_does_not_reopen_an_operator_revoked_invite(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    svc.invite_real_records("revoked@example.com", "operator")
    assert svc.revoke_real_records_invite("revoked@example.com")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="friend@example.com")
    code = _code(c)
    _join_via(app, sent, code, "revoked@example.com", first_run=False)
    by, revoked_at = _invite_row(svc, "revoked@example.com")
    assert revoked_at is not None, "operator revoke was undone by a link"
    assert by == "operator"


def test_an_uninvited_account_cannot_grant_real_records_to_a_second_mailbox(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",
                    FASTEN_PUBLIC_KEY="pub123")
    a = app.test_client()
    _login(a, svc, monkeypatch, email="stranger@example.com")
    # The inviter itself is refused real records...
    assert a.post("/api/connections/fasten", json={
        **CONSENT, "consent_version": tester_terms.CONSENT_VERSION},
    ).status_code == 503
    code = _code(a)
    # ...but its second mailbox, joined through its own link, is not.
    b, _ = _join_via(app, sent, code, "stranger@other-domain.example",
                     first_run=False)
    _login(b, svc, monkeypatch, email="stranger@other-domain.example")
    r = b.post("/api/connections/fasten", json={
        **CONSENT, "consent_version": tester_terms.CONSENT_VERSION})
    assert r.status_code == 503, "second mailbox can start a real connection"


def test_plus_aliases_of_one_inbox_take_one_real_record_invite(
        made, sent, monkeypatch):  # noqa: F811
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="host@example.com")
    code = _code(c)
    for i in range(3):
        _join_via(app, sent, code, f"robin+{i}@example.org",
                  ip=f"203.0.113.{40 + i}", first_run=False)
        _age_caps(svc)
    # Invites are keyed by inbox, so count rows, not aliases that match.
    with svc.session() as s:
        granted = [r.display_email for r in s.query(RealRecordInvite)]
    assert len(granted) <= 1, granted


def test_an_inviter_cannot_spend_their_own_link_through_a_plus_alias(
        made, sent, monkeypatch):  # noqa: F811
    """Passes: the self-invite check folds the mailbox."""
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="self.ish@gmail.com")
    code = _code(c)
    _join_via(app, sent, code, "selfish+x@googlemail.com", first_run=False)
    assert _invite_row(svc, "selfish+x@googlemail.com") is None
    assert invites.join_outcome(svc.session, "selfish+x@googlemail.com") is None


def test_admin_funnel_is_404_for_a_case_or_alias_variant_of_the_owner(
        made, monkeypatch):  # noqa: F811
    """Passes: exact lower-cased match, no mailbox folding."""
    app, svc = made(CARE_OWNER_EMAILS="Owner@Example.com")
    for who in ("owner+x@example.com", "0wner@example.com",
                "owner@example.co"):
        c = app.test_client()
        _login(c, svc, monkeypatch, email=who)
        assert c.get("/admin/funnel").status_code == 404, who
    c = app.test_client()
    _login(c, svc, monkeypatch, email="OWNER@example.com")
    r = c.get("/admin/funnel")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # Emails only in the Testers table (owner decision 2026-10-16), nowhere
    # else on the page.
    assert "@" not in html.split("<main", 1)[1].split(
        'id="funnel-days"', 1)[1]
    assert _account_id(svc, "OWNER@example.com".lower())


def _deployed_access_log_formats():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "deploy" / "careagents"
    docker = re.search(r"--access-logformat '([^']*)'",
                       (root / "Dockerfile").read_text()).group(1)
    service = re.search(r"--access-logformat '([^']*)'",
                        (root / "careagents.service").read_text()).group(1)
    return [docker.replace('\\"', '"'), service.replace("%%", "%")]


@pytest.mark.parametrize("fmt", _deployed_access_log_formats())
def test_the_access_log_never_holds_an_invite_code(fmt):
    import datetime
    from types import SimpleNamespace

    from gunicorn.config import Config as GConfig
    from gunicorn.glogging import Logger

    code = "0123456789abcdef0123456789abcdef"
    log = Logger(GConfig())
    # The link as Settings now writes it (#928 F5): the code in the query.
    environ = {"REQUEST_METHOD": "GET", "RAW_URI": f"/i?c={code}",
               "PATH_INFO": "/i", "QUERY_STRING": f"c={code}",
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "10.0.0.1"}
    resp = SimpleNamespace(status="200 OK", sent=10, headers=[])
    req = SimpleNamespace(headers=[])
    line = fmt % log.atoms(resp, req, environ,
                           datetime.timedelta(milliseconds=5))
    assert code not in line, line
