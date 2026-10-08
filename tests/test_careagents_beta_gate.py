"""The one real-records gate: off beats everything, pause beats everything
else, and a table invite counts only in allowlist mode with approved terms
(R1, R6).

R2, for the reviewer: the invite is matched against `acct.email`, and an
account row exists only after the email code for that address was verified
(AccountService.verify_email_code), so holding the account proves control
of the invited inbox.
"""

from __future__ import annotations

import pytest

from careagents.app import create_app
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import (
    allowlist_cfg, approve_terms, pend_terms)
from tests.test_careagents import FakeClient, _login

EMAIL = "tester@example.com"


def _client(cfg, monkeypatch):
    from careagents.accounts import AccountService
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    return c, svc


def _can_start(c) -> bool:
    return c.post("/api/connections/fasten",
                  json=consented()).status_code == 200


def _tier(c) -> str:
    return {m["id"]: m["tier"] for m in c.get(
        "/api/connections/catalog").get_json()["connectors"]}["fasten"]


@pytest.mark.parametrize("mode", ["off", "allowlist", "on"])
def test_off_is_closed_whatever_the_table_and_env_say(monkeypatch, mode):
    cfg = allowlist_cfg(CARE_REAL_RECORDS=mode,
                        CARE_REAL_RECORDS_ALLOWLIST=EMAIL)
    c, svc = _client(cfg, monkeypatch)
    approve_terms(monkeypatch)
    svc.invite_real_records(EMAIL, "operator")
    assert _can_start(c) is (mode != "off")


def test_off_answers_before_any_account_state_is_read(monkeypatch):
    """R1: `off` is decided from the config alone. Neither the pause flag
    nor the invite table is consulted, so no row can reopen it."""
    c, svc = _client(allowlist_cfg(CARE_REAL_RECORDS="off"), monkeypatch)
    approve_terms(monkeypatch)

    def _read(*_a, **_k):
        pytest.fail("off mode read account state")

    monkeypatch.setattr(svc, "is_paused", _read)
    monkeypatch.setattr(svc, "real_records_invited", _read)
    assert _can_start(c) is False
    assert _tier(c) == "soon"


def test_an_invite_opens_allowlist_mode_only_once_terms_are_approved(
        monkeypatch):
    pend_terms(monkeypatch)
    c, svc = _client(allowlist_cfg(), monkeypatch)
    svc.invite_real_records(EMAIL, "operator")
    assert _can_start(c) is False          # terms pending (R6)
    assert _tier(c) == "soon"
    approve_terms(monkeypatch)
    assert _tier(c) == "live"
    assert _can_start(c) is True


def test_an_invite_starts_the_env_allowlist_connect_only_after_acceptance(
        monkeypatch):
    """#565 approval, end to end. An invited email (not on the environment
    list) cannot start a real connection while the terms are pending, nor
    once they are approved until the terms are accepted. Accepted, it
    reaches the same connect path an environment-allowlisted account does:
    the same connector plan, the same answer, the same stored consent."""
    from careagents import connectors, tester_terms
    from careagents.models import Connection
    shipped = tester_terms.TERMS_VERSION
    assert shipped and tester_terms.approved()   # the terms in this tree

    starts = []
    real_start = connectors.start

    def _spy(connector_id, provider, cfg, client, *, real_records):
        starts.append((connector_id, real_records))
        return real_start(connector_id, provider, cfg, client,
                          real_records=real_records)

    monkeypatch.setattr(connectors, "start", _spy)
    cfg = allowlist_cfg(CARE_REAL_RECORDS_ALLOWLIST="listed@example.com")
    c, svc = _client(cfg, monkeypatch)          # EMAIL: invited, not listed
    assert EMAIL not in cfg.real_records_allowlist
    svc.invite_real_records(EMAIL, "operator")

    pend_terms(monkeypatch)
    r = c.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 503 and starts == [("fasten", False)]

    approve_terms(monkeypatch, shipped)
    r = c.post("/api/connections/fasten", json={})
    assert r.status_code == 428
    assert r.get_json()["consent_version"] == shipped
    with svc.session() as s:
        assert s.query(Connection).filter_by(kind="fasten").count() == 0

    invited = c.post("/api/connections/fasten", json={"consent": True})
    listed_client = c.application.test_client()
    _login(listed_client, svc, monkeypatch, email="listed@example.com")
    listed = listed_client.post("/api/connections/fasten",
                                json={"consent": True})
    assert invited.status_code == listed.status_code == 200
    a, b = invited.get_json(), listed.get_json()
    assert a.keys() == b.keys() == {"id", "status", "connect_url"}
    assert a["status"] == b["status"] == "pending"
    assert starts[-3:] == [("fasten", True)] * 3
    with svc.session() as s:
        rows = {r.id: r for r in s.query(Connection).filter_by(kind="fasten")}
        assert {rows[a["id"]].consent_version,
                rows[b["id"]].consent_version} == {shipped}
        assert rows[a["id"]].status == rows[b["id"]].status == "pending"


def test_the_env_allowlist_still_works_without_approved_terms(monkeypatch):
    pend_terms(monkeypatch)
    c, _ = _client(allowlist_cfg(CARE_REAL_RECORDS_ALLOWLIST=EMAIL),
                   monkeypatch)
    assert _can_start(c) is True


def test_an_uninvited_account_is_refused(monkeypatch):
    c, svc = _client(allowlist_cfg(), monkeypatch)
    approve_terms(monkeypatch)
    svc.invite_real_records("someone.else@example.com", "operator")
    assert _can_start(c) is False


def test_a_revoked_invite_closes_new_connections(monkeypatch):
    c, svc = _client(allowlist_cfg(), monkeypatch)
    approve_terms(monkeypatch)
    svc.invite_real_records(EMAIL, "operator")
    svc.revoke_real_records_invite(EMAIL)
    assert _can_start(c) is False


@pytest.mark.parametrize("mode", ["allowlist", "on"])
def test_a_paused_account_is_closed_even_when_on(monkeypatch, mode):
    c, svc = _client(allowlist_cfg(CARE_REAL_RECORDS=mode,
                                   CARE_REAL_RECORDS_ALLOWLIST=EMAIL),
                     monkeypatch)
    assert svc.set_paused(EMAIL, True) is True
    assert _can_start(c) is False
    assert _tier(c) == "soon"
    svc.set_paused(" Tester@Example.COM ", False)
    assert _can_start(c) is True


def test_pausing_an_unknown_email_reports_false(monkeypatch):
    _, svc = _client(allowlist_cfg(), monkeypatch)
    assert svc.set_paused("nobody@example.com", True) is False


def test_pause_is_logged_by_account_id_never_by_email(monkeypatch, caplog):
    _, svc = _client(allowlist_cfg(), monkeypatch)
    with caplog.at_level("INFO"):
        svc.set_paused(EMAIL, True)
        svc.set_paused(EMAIL, False)
    assert "acct_" in caplog.text
    assert "@" not in caplog.text
