"""Security sign-off of PR #853 (#847): a disconnected connection's requests.

`_live_agent_context` says "A revoked connection is not a pathway to the
tenant's requests", and the disconnect dialog this PR adds tells the person
that an assistant on those records keeps its chat but "anything it prepares
can't be approved". The chat page now reads the engine's pending list on
every load (#847) through `svc.get_agent_context`, which does not check the
connection's status. So after a disconnect the chat still asks the revoked
tenant for its pending requests and draws a "Review & approve" card for each,
whose button then 404s on the review page.

Approval is still impossible (the review relay refuses), so this is not an
approval bypass. It is a read of a revoked tenant's request list, and a
card that promises an approval the product will refuse. Expected to FAIL
at e90d30b.
"""

from __future__ import annotations

from tests.test_careagents_what_happened import (  # noqa: F401
    OutcomeClient, _app, _signed_in, cfg, svc)


class _Spy(OutcomeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.pending_tenants = []

    def pending_actions(self, tenant):
        self.pending_tenants.append(tenant)
        return super().pending_actions(tenant)


def test_a_disconnected_connection_draws_no_review_cards_in_chat(
        cfg, svc, monkeypatch):  # noqa: F811
    fake = _Spy(pending=[{"id": "act-revoked-1", "kind": "sms",
                          "to": "CVS Pharmacy",
                          "status": "awaiting_confirmation"}])
    c, agent, conn = _signed_in(_app(cfg, svc, fake), svc, monkeypatch)
    assert c.post(f"/api/connections/{conn}/disconnect").status_code == 200
    # The review page and the approvals list both refuse the revoked tenant.
    assert c.get(f"/review/{agent}/act-revoked-1").status_code == 404
    fake.pending_tenants.clear()
    r = c.get(f"/chat?agent={agent}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "act-revoked-1" not in html, (
        "the chat draws a Review & approve card for a revoked connection")
    assert fake.pending_tenants == [], (
        "the chat read the revoked tenant's pending list")
