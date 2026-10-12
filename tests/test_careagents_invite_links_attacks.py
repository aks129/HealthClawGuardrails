"""Security review of #928: exploit tests for personal invite links.

Each test asserts the safe behaviour. A test marked xfail(strict=True) pins
a finding that is still open: it goes red the day the finding is fixed, and
the marker comes off then. Synthetic data only.
"""

from __future__ import annotations

import threading

import pytest

from careagents import beta, invites, tester_terms
from careagents.config import Config
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


@pytest.mark.xfail(strict=True, reason=(
    "#928 F1: invite_real_records is count-then-insert ('a hand-run "
    "command, not worth a lock'), and this PR puts it on the public "
    "/beta/confirm path. Concurrent confirms at 24 live invites pass the "
    "stage 1 cap of 25."))
def test_concurrent_confirms_never_pass_the_stage1_cap(tmp_path, monkeypatch):
    from careagents.accounts import AccountService
    from tests.test_careagents import _make_account
    cfg = Config(env={"CARE_DATABASE_URL": f"sqlite:///{tmp_path}/s1.db",
                      "OPENAI_API_KEY": "k", "CARE_INVITE_LINK_CAP": "20"})
    svc = AccountService(cfg)
    try:
        for i in range(beta.STAGE1_INVITE_CAP - 1):
            svc.invite_real_records(f"t{i}@example.com", "operator")
        host = _make_account(svc, monkeypatch, "host@example.com")
        link = invites.link_for(svc.session, host.id, 20)
        joiners = [f"racer{i}@example.org" for i in range(8)]
        for e in joiners:
            assert invites.note_pending(svc.session, e, link["code"], 20)

        # Hold every caller between the count and the insert, so the
        # check-then-act window is hit deterministically.
        from sqlalchemy.orm import Query
        real_count = Query.count
        gate = threading.Barrier(len(joiners), timeout=10)

        def count(self):
            n = real_count(self)
            if threading.current_thread().name.startswith("racer"):
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
            return n
        monkeypatch.setattr(Query, "count", count)

        out = []
        threads = [threading.Thread(
            target=lambda e=e: out.append(invites.on_join(svc, cfg, e)),
            name=f"racer{i}") for i, e in enumerate(joiners)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        monkeypatch.setattr(Query, "count", real_count)
        assert _active_invites(svc) <= beta.STAGE1_INVITE_CAP
    finally:
        svc.engine.dispose()


@pytest.mark.xfail(strict=True, reason=(
    "#928 F2: on_join calls invite_real_records, which reopens a revoked "
    "invite. A peer's link undoes the operator's revoke with no operator "
    "step, and invited_by is overwritten."))
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


@pytest.mark.xfail(strict=True, reason=(
    "#928 F3: every account gets a link, including one with no real-record "
    "access made through the public sign-in. A second mailbox joined "
    "through it gets a RealRecordInvite, so one stranger self-grants real "
    "records with no operator step. Needs an owner ruling: should a link "
    "grant real records only when its owner holds them (or only operator-"
    "approved inviters)?"))
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


@pytest.mark.xfail(strict=True, reason=(
    "#928 F4 (Low): pending notes, joins and RealRecordInvite key on the raw "
    "address, not the mailbox. One inbox via +tags takes several seats and "
    "several stage 1 invites."))
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
    granted = [e for e in (f"robin+{i}@example.org" for i in range(3))
               if _invite_row(svc, e) is not None]
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
    assert "@" not in html.split("<main", 1)[1]   # no email on the page
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


@pytest.mark.xfail(strict=True, reason=(
    "#928 F5 (Low): the invite code is in the PATH (/i/<code>), and the "
    "deployed access log keeps the path (%(U)s); it drops only the query "
    "string, which is where the repo keeps link secrets (/link?t=...). "
    "Every opened code, a capability that hands out real-record invites, "
    "lands in the platform log."))
@pytest.mark.parametrize("fmt", _deployed_access_log_formats())
def test_the_access_log_never_holds_an_invite_code(fmt):
    import datetime
    from types import SimpleNamespace

    from gunicorn.config import Config as GConfig
    from gunicorn.glogging import Logger

    code = "0123456789abcdef0123456789abcdef"
    log = Logger(GConfig())
    environ = {"REQUEST_METHOD": "GET", "RAW_URI": f"/i/{code}",
               "PATH_INFO": f"/i/{code}", "QUERY_STRING": "",
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "10.0.0.1"}
    resp = SimpleNamespace(status="200 OK", sent=10, headers=[])
    req = SimpleNamespace(headers=[])
    line = fmt % log.atoms(resp, req, environ,
                           datetime.timedelta(milliseconds=5))
    assert code not in line, line
