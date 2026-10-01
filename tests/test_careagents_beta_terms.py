"""The tester terms and the consent version move together (spec 4.3, R5)."""

from __future__ import annotations

from pathlib import Path

from careagents import tester_terms
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)

TERMS = (Path(__file__).resolve().parents[1] / "careagents" / "templates"
         / tester_terms.TEMPLATE)


def test_the_terms_file_and_the_consent_version_agree():
    """Green while the placeholder stands. Goes red the day someone pastes
    the approved terms without setting TERMS_VERSION, or sets it without
    replacing the placeholder."""
    pending = tester_terms.PENDING_MARKER in TERMS.read_text()
    assert tester_terms.approved() is (not pending)
    if pending:
        assert tester_terms.CONSENT_VERSION == tester_terms.BASE_VERSION
    else:
        assert tester_terms.CONSENT_VERSION == tester_terms.TERMS_VERSION
        assert tester_terms.CONSENT_VERSION != tester_terms.BASE_VERSION
    assert len(tester_terms.CONSENT_VERSION) <= 16  # ca_connections column
    assert 'id="tester-terms"' in TERMS.read_text()


def test_the_card_hides_pending_terms_and_shows_approved_ones(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert 'id="tester-terms"' not in c.get("/home").get_data(as_text=True)
    approve_terms(monkeypatch)
    assert 'id="tester-terms"' in c.get("/home").get_data(as_text=True)


def test_a_new_real_connection_records_the_current_version(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.app import create_app
    from careagents.models import Connection
    approve_terms(monkeypatch, "2026-10-01")
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/fasten", json={})
    assert r.status_code == 428
    assert r.get_json()["consent_version"] == "2026-10-01"
    assert c.post("/api/connections/fasten",
                  json={"consent": True}).status_code == 200
    with svc.session() as s:
        assert s.query(Connection).filter_by(
            kind="fasten").one().consent_version == "2026-10-01"


def test_the_change_line_moves_with_the_terms():
    """The "Our terms changed" card's one line is a placeholder until #565.
    Approving the terms without replacing it fails here."""
    if tester_terms.approved():
        assert tester_terms.CHANGE_SUMMARY != tester_terms.PENDING_CHANGE_SUMMARY
    assert "—" not in tester_terms.CHANGE_SUMMARY
