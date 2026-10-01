"""Operator commands for stage 1 (beta spec 4.2, 4.5, 4.6). Commands, not
routes, for the same reason as `page-views`: no admin surface to get wrong.
The `invites` group shipped with #852; this file pins what stage 1 adds."""

from __future__ import annotations

import logging
import re

from careagents.app import create_app
from tests.careagents_stage1_helpers import allowlist_cfg, approve_terms
from tests.test_careagents import FakeClient, _login


def _runner(**env):
    from careagents.accounts import AccountService
    cfg = allowlist_cfg(**env)
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    return app, app.test_cli_runner(), svc


def test_invite_add_says_invites_wait_for_the_terms(monkeypatch):
    app, run, svc = _runner()
    r = run.invoke(args=["invites", "add", "T@Example.com", "--by", "ops-1"])
    assert r.exit_code == 0, r.output
    assert "invited t@example.com" in r.output
    assert "not honoured until the tester terms are approved" in r.output
    approve_terms(monkeypatch)
    r = run.invoke(args=["invites", "add", "u@example.com", "--by", "ops-1"])
    assert "not honoured" not in r.output


def test_a_full_cohort_is_an_error_exit():
    app, run, svc = _runner()
    for i in range(25):
        svc.invite_real_records(f"t{i}@example.com", "operator")
    r = run.invoke(args=["invites", "add", "late@example.com", "--by", "ops"])
    assert r.exit_code != 0 and "full" in r.output


def test_pause_and_resume_by_email(monkeypatch):
    app, run, svc = _runner()
    _login(app.test_client(), svc, monkeypatch, email="p@example.com")
    from careagents.models import Account

    def paused_at():
        with svc.session() as s:
            return s.query(Account).filter_by(
                email="p@example.com").one().real_paused_at

    r = run.invoke(args=["records", "pause", "P@Example.com"])
    assert r.exit_code == 0 and "paused" in r.output
    assert paused_at() is not None
    assert run.invoke(args=["records", "pause", "no@example.com"]).exit_code == 1
    r = run.invoke(args=["records", "resume", "p@example.com"])
    assert r.exit_code == 0 and "resumed" in r.output
    assert paused_at() is None
    assert run.invoke(args=["records", "resume", "no@example.com"]).exit_code == 1


def test_the_pause_help_says_what_it_does_not_stop():
    app, run, svc = _runner()
    out = run.invoke(args=["records", "pause", "--help"]).output
    assert "ingest" in out and "MCP" in out


def test_weekly_counts_prints_integers_and_no_identity(monkeypatch):
    app, run, svc = _runner()
    _login(app.test_client(), svc, monkeypatch, email="w@example.com")
    out = run.invoke(args=["weekly-counts", "--weeks", "2"]).output
    assert "@" not in out and "acct_" not in out
    lines = out.strip().splitlines()
    assert lines[0].split() == ["week", "signed_up", "real_connected",
                                "asked", "approved"]
    assert len(lines) == 3
    for line in lines[1:]:
        assert re.fullmatch(r"\d{4}-W\d{2}(\s+\d+){4}", line.strip())
    assert lines[1].split()[1] == "1"     # the account just made


def test_no_operator_command_writes_an_email_to_the_log(monkeypatch, caplog):
    app, run, svc = _runner()
    _login(app.test_client(), svc, monkeypatch, email="quiet@example.com")
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        run.invoke(args=["invites", "add", "quiet@example.com", "--by", "o"])
        run.invoke(args=["invites", "list"])
        run.invoke(args=["records", "pause", "quiet@example.com"])
        run.invoke(args=["records", "resume", "quiet@example.com"])
        run.invoke(args=["invites", "revoke", "quiet@example.com"])
        run.invoke(args=["weekly-counts"])
    assert "@example.com" not in caplog.text
