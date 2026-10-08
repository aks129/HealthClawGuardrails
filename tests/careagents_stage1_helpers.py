"""Helpers shared by the beta stage 1 tests. Not a test module."""

from __future__ import annotations

import os

from careagents.config import Config


def allowlist_cfg(**extra) -> Config:
    """The shared `cfg` fixture opens real records to everyone (`on`).
    Stage 1 runs in `allowlist`, so these tests build their own config."""
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        from careagents.models import Base, make_engine
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    env = {"CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
           "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
           "HEALTHCLAW_MINT_SECRET": "mint-secret",
           "FASTEN_PUBLIC_KEY": "pub123", "CARE_REAL_RECORDS": "allowlist"}
    env.update(extra)
    return Config(env=env)


def approve_terms(monkeypatch, version: str = "2026-10-01") -> None:
    """Act as if #565 were approved: terms version set, consent bumped."""
    from careagents import tester_terms
    monkeypatch.setattr(tester_terms, "TERMS_VERSION", version)
    monkeypatch.setattr(tester_terms, "CONSENT_VERSION", version)


def pend_terms(monkeypatch) -> None:
    """Act as if #565 were still pending: no terms, base consent version.
    For the tests that pin what holds before approval."""
    from careagents import tester_terms
    monkeypatch.setattr(tester_terms, "TERMS_VERSION", None)
    monkeypatch.setattr(tester_terms, "CONSENT_VERSION",
                        tester_terms.BASE_VERSION)
