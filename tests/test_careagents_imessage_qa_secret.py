"""QA #866 round 2: an unset relay secret opens nothing.

`secret_matches` refuses when the expected secret is empty. Without that
guard both sides hash "" and match, so a deployment missing
HEALTHCLAW_MINT_SECRET would accept relay calls carrying no header at all.
Production refuses to boot without the secret; this pins the guard for
every other environment. Kills: delete `if not expected: return False`.
"""

from __future__ import annotations

from careagents.accounts import secret_matches
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)


def test_an_empty_expected_secret_matches_nothing():
    assert secret_matches("", "") is False
    assert secret_matches("anything", "") is False
    assert secret_matches("m", "m") is True


def test_relay_routes_refuse_when_no_secret_is_configured(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    cfg.mint_secret = ""
    for headers in ({}, {"X-Internal-Secret": ""}):
        r = c.post("/api/surfaces/imessage/inbound", headers=headers,
                   json={"handle": "+15550100321", "text": "hi"})
        assert r.status_code == 403
        r = c.get("/api/surfaces/imessage/runs/x", headers=headers)
        assert r.status_code == 403
