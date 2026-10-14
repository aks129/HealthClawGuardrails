"""Real-record invites in the database (beta pathway spec section 4.2).

The spec's tests, section 6:
  - An invited email can start a real connection; an uninvited one cannot.
  - The environment allowlist still works alongside the table.
  - A revoked invite blocks new connections and keeps existing ones.
Plus the rule the environment list already follows: invites are read in
`allowlist` mode only, so the table is never a way around `off`.

Stage 1 (beta spec 4.3) honours table invites only once the tester terms
are approved (#565), so the tests that expect an invite to open records
approve them first. The gate's own tests are test_careagents_beta_gate.py.
"""

from __future__ import annotations

import json

from careagents.config import Config
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _beta_app, _login, cfg, svc)

_EMPTY_BUNDLE = json.dumps({"resourceType": "Bundle", "type": "collection",
                            "entry": []})


def _tiers(client):
    return {m["id"]: m["tier"] for m in
            client.get("/api/connections/catalog").get_json()["connectors"]}


def test_an_invited_email_can_start_a_real_connection(svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    assert svc.invite_real_records("Tester@Example.org", "owner") is True
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tester@example.org")
    assert _tiers(c)["fasten"] == "live"
    r = c.post("/api/connections/direct", json=consented())
    assert r.status_code == 200, r.get_json()


def test_an_uninvited_email_cannot(svc, monkeypatch):  # noqa: F811
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records("someone.else@example.org", "owner")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="stranger@example.org")
    assert _tiers(c)["fasten"] == "soon"
    assert c.post("/api/connections/direct",
                  json=consented()).status_code == 503


def test_the_environment_allowlist_still_works_alongside_the_table(
        svc, monkeypatch):  # noqa: F811
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="env@example.org")
    approve_terms(monkeypatch)
    svc.invite_real_records("table@example.org", "owner")
    for email in ("env@example.org", "table@example.org"):
        c = app.test_client()
        _login(c, svc, monkeypatch, email=email)
        assert c.post("/api/connections/direct",
                      json=consented()).status_code == 200, email


def test_a_revoked_invite_blocks_new_connections_and_keeps_existing_ones(
        svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records("tester@example.org", "owner")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tester@example.org")
    existing = c.post("/api/connections/direct",
                      json=consented()).get_json()["id"]

    assert svc.revoke_real_records_invite("tester@example.org") is True

    assert c.post("/api/connections/direct",
                  json=consented()).status_code == 503
    assert _tiers(c)["fasten"] == "soon"
    r = c.post(f"/api/connections/{existing}/upload", data=_EMPTY_BUNDLE,
               content_type="application/fhir+json")
    assert r.status_code == 200, r.get_json()


def test_inviting_again_reopens_a_revoked_invite(svc):  # noqa: F811
    svc.invite_real_records("tester@example.org", "owner")
    assert svc.invite_real_records("tester@example.org", "owner") is False
    svc.revoke_real_records_invite("tester@example.org")
    assert svc.revoke_real_records_invite("tester@example.org") is False
    assert svc.real_records_invited("tester@example.org") is False
    assert svc.invite_real_records("tester@example.org", "ops") is True
    assert svc.real_records_invited("TESTER@example.org") is True
    [row] = svc.real_record_invites()
    assert row["invited_by"] == "ops" and row["revoked_at"] is None


def test_invites_are_never_a_way_around_off(svc, monkeypatch):  # noqa: F811
    app = _beta_app(svc)  # CARE_REAL_RECORDS unset is off
    svc.invite_real_records("tester@example.org", "owner")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="tester@example.org")
    assert _tiers(c)["fasten"] == "soon"
    assert c.post("/api/connections/direct",
                  json=consented()).status_code == 503


def test_the_config_rule_asks_the_table_only_in_allowlist_mode():
    base = {"CARE_DATABASE_URL": "sqlite:///:memory:"}
    asked = []

    def invited(email):
        asked.append(email)
        return True

    off = Config(env={**base, "CARE_REAL_RECORDS": "off"})
    on = Config(env={**base, "CARE_REAL_RECORDS": "on"})
    listed = Config(env={**base, "CARE_REAL_RECORDS": "allowlist",
                         "CARE_REAL_RECORDS_ALLOWLIST": "env@example.org"})
    assert off.real_records_open_for("a@example.org", invited=invited) is False
    assert on.real_records_open_for("a@example.org", invited=invited) is True
    assert listed.real_records_open_for("env@example.org",
                                        invited=invited) is True
    assert asked == []
    assert listed.real_records_open_for(" A@Example.org ",
                                        invited=invited) is True
    assert asked == ["a@example.org"]
    assert listed.real_records_open_for("", invited=invited) is False
    assert listed.real_records_open_for(None, invited=invited) is False
    assert listed.real_records_open_for("b@example.org") is False


def test_a_bad_email_is_refused(svc):  # noqa: F811
    from careagents.accounts import AuthError
    import pytest
    for bad in ("", "not-an-email", "a" * 250 + "@example.org"):
        with pytest.raises(AuthError):
            svc.invite_real_records(bad, "owner")
    with pytest.raises(ValueError):
        svc.invite_real_records("ok@example.org", "  ")
    assert svc.real_record_invites() == []


def test_the_operator_commands_add_list_and_revoke(svc):  # noqa: F811
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    runner = app.test_cli_runner()
    r = runner.invoke(args=["invites", "add", "Tester@Example.org",
                            "--by", "owner"])
    assert r.exit_code == 0 and "invited tester@example.org" in r.output
    r = runner.invoke(args=["invites", "list"])
    assert "tester@example.org" in r.output and "live" in r.output
    r = runner.invoke(args=["invites", "revoke", "tester@example.org"])
    assert r.exit_code == 0 and "revoked" in r.output
    r = runner.invoke(args=["invites", "list"])
    assert "revoked" in r.output
    r = runner.invoke(args=["invites", "add", "not-an-email", "--by", "owner"])
    assert r.exit_code != 0


def test_the_add_command_warns_when_invites_are_not_read(svc):  # noqa: F811
    runner = _beta_app(svc).test_cli_runner()
    r = runner.invoke(args=["invites", "add", "tester@example.org",
                            "--by", "owner"])
    assert r.exit_code == 0 and "'off'" in r.output
