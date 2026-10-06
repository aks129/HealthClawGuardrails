"""QA gaps found reviewing PR #866 (iMessage, text first).

1. The claim-side single-spend guard on a texted link had no test: the
   "works once" test re-enters through /link/<token>, whose peek also checks
   `used_at`, so dropping the guard from `claim_imessage_link` survived.
2. A row bound before handles were normalized (an Apple ID as it arrived,
   mixed case) is found by `find_surface_by_handle(..., also=raw)` but not by
   STOP or by the one-handle-one-account check, which match the normalized
   handle only. STOP is then confirmed and not honoured.

Tests 2 and 3 fail at 65ad7a1 by design: they are the reproduction for Dev.
Synthetic handles only.
"""

from __future__ import annotations

from careagents import imessage
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)
from tests.test_careagents_imessage import (_acct_id, _inbound, _link_token,
                                            _pair)

PHONE = "+15550100321"
LEGACY_RAW = "QA.Legacy@iCloud.com"     # as a pre-#866 row stored it


def _legacy_row(svc, account_id, agent_id, handle=LEGACY_RAW):  # noqa: F811
    """A surface bound by the pre-#866 /bind route, which stored the
    handle exactly as the relay sent it."""
    from careagents.models import Surface, now
    with svc.session() as s:
        s.add(Surface(account_id=account_id, agent_id=agent_id,
                      kind="imessage", handle=handle, status="active",
                      bound_at=now(), welcome_due=0))


def _first_account(svc):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).first().id


def _second_account(app, svc, monkeypatch, email="qa-other@example.com"):  # noqa: F811
    other = app.test_client()
    _login(other, svc, monkeypatch, email=email)
    conn = other.post("/api/connections/sample").get_json()["id"]
    agent = other.post("/api/agents", json={
        "name": "B", "persona": "calm", "connection_id": conn}
    ).get_json()["id"]
    return other, agent


def test_a_link_parked_by_two_browsers_binds_once(cfg, svc, monkeypatch):  # noqa: F811
    """Two browsers open the same (forwarded) link before either says yes.
    The first yes spends it; the second must read as expired, not run a
    second claim. Kills: drop `used_at IS NULL` from claim_imessage_link."""
    app, owner, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(owner, PHONE, "hi").get_json()["reply"])

    first = app.test_client()
    _login(first, svc, monkeypatch, email="first@example.com")
    second = app.test_client()
    _login(second, svc, monkeypatch, email="second@example.com")
    assert first.get(f"/link?t={token}").headers["Location"].endswith(
        "/link/done")
    assert second.get(f"/link?t={token}").headers["Location"].endswith(
        "/link/done")

    done = first.post("/link/done", data={"connect": "yes"})
    assert "You're connected." in done.get_data(as_text=True)
    late = second.post("/link/done", data={"connect": "yes"})
    assert ("This link has already been used or has expired."
            in late.get_data(as_text=True))
    surface = svc.find_surface_by_handle(PHONE)
    assert surface["account_id"] == _acct_id(svc, "first@example.com")


def test_claim_spends_a_link_exactly_once(cfg, svc, monkeypatch):  # noqa: F811
    """The service contract, without the route's peek in front of it."""
    app, owner, *_ = _chat_app(cfg, svc, monkeypatch)
    token = svc.issue_imessage_link(PHONE)
    link_id = svc.peek_imessage_link(token)
    acct = _first_account(svc)
    assert svc.claim_imessage_link(link_id, acct) == "connected"
    assert svc.claim_imessage_link(link_id, acct) == "expired"


def test_stop_unbinds_a_row_stored_before_normalization(
        cfg, svc, monkeypatch):  # noqa: F811
    """REPRODUCTION (fails at 65ad7a1): STOP answers "won't text you
    anymore" but the legacy row stays active and the next text is queued."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    acct = _first_account(svc)
    _legacy_row(svc, acct, agent_id)
    assert _inbound(c, LEGACY_RAW, "hello").status_code == 202  # routes

    r = _inbound(c, LEGACY_RAW, "STOP")
    assert r.get_json() == {"reply": imessage.STOP_TEXT}
    assert svc.find_surface_by_handle(
        imessage.normalize_handle(LEGACY_RAW), also=LEGACY_RAW) is None
    after = _inbound(c, LEGACY_RAW, "are you still reading my records?")
    assert after.status_code == 200 and after.get_json() == {}


def test_a_row_stored_before_normalization_still_counts_as_taken(
        cfg, svc, monkeypatch):  # noqa: F811
    """REPRODUCTION (fails at 65ad7a1): one handle, one account. A legacy
    mixed-case row on account A does not stop account B pairing the same
    Apple ID, leaving two active rows for one sender."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    acct = _first_account(svc)
    _legacy_row(svc, acct, agent_id)

    other, other_agent = _second_account(app, svc, monkeypatch)
    r = _pair(other, other_agent, handle=LEGACY_RAW)
    assert r.status_code == 409
    assert r.get_json()["reply"] == imessage.TAKEN_TEXT
