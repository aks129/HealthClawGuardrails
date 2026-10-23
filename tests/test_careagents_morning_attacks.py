# ruff: noqa: F811  (the fixtures are imported, then named as parameters)
"""Security review of PR #1009, the owner Morning block on /admin/funnel.

Finding: the Testers table's `ref` is read from every /beta request, any
status, keyed by the folded mailbox (`beta_signup.account_key`: case,
`+tag`, Gmail dots). A /beta submit needs no proof of the inbox, so anyone
can post a never-confirmed request for `victim+x@...` with a ref of their
choosing, and it lands on the victim's row, the owner's own row included,
and replaces a ref the tester really came with. Only a confirmed request
proves the address, so only a confirmed request may name the ref.

Synthetic data only.
"""

from __future__ import annotations

from careagents import beta_signup, signins
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)
from tests.test_careagents_funnel import _app
from tests.test_careagents_signins import NOW, _make, _ts

OWNER = "owner@example.com"


def _request(svc, email, ref, status="pending", confirmed=False):
    with svc.session() as s:
        s.add(beta_signup.BetaRequest(
            email=email, first_name="Pat", ref=ref, status=status,
            confirmed_at=_ts(2) if confirmed else None,
            created_at=_ts(2), updated_at=_ts(2)))


def _ref(svc, email):
    rows = signins.testers(svc.session, moment=NOW)
    return {r["email"]: r["ref"] for r in rows}[email]


def test_an_unconfirmed_tag_variant_request_does_not_set_a_testers_ref(svc):
    _make(svc, "victim@example.com", 3)
    # Anyone can post this: /beta proves nothing about the inbox.
    beta_signup.submit(svc.session, "Mal", "victim+x@example.com", None,
                       "attacker-ref")
    assert _ref(svc, "victim@example.com") != "attacker-ref"


def test_an_unconfirmed_request_does_not_replace_a_confirmed_ref(svc):
    _make(svc, "victim@example.com", 3)
    _request(svc, "victim@example.com", "real-campaign", status="active",
             confirmed=True)
    _request(svc, "victim+x@example.com", "attacker-ref")
    assert _ref(svc, "victim@example.com") == "real-campaign"


def test_a_stranger_cannot_write_a_ref_onto_the_owners_own_row(
        svc, monkeypatch):
    app = _app(svc, CARE_OWNER_EMAILS=OWNER)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=OWNER)
    stranger = app.test_client()
    stranger.post("/beta", json={"first_name": "Mal",
                                 "email": "owner+x@example.com",
                                 "ref": "attacker-ref", "consent": True})
    with svc.session() as s:
        assert s.get(beta_signup.BetaRequest, "owner+x@example.com")
    page = c.get("/admin/funnel")
    assert page.status_code == 200
    assert "attacker-ref" not in page.get_data(as_text=True)
