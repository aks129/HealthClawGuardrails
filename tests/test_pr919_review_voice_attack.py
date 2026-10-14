"""Security review of #919: the review relay decides the tenant and the voice
from two separate reads of the assistant's connection.

`review()` in careagents/app.py resolves the tenant through
`_agent_owns_action` (one `get_agent_context`), then calls
`_live_agent_context` a second time to decide `sample`. Between the two, the
account can move the assistant to its sample records with
POST /api/agents/<id>/connection. The page is then fetched for the REAL
tenant's action while asking the engine, with the internal secret, for the
sample voice: a real patient's intake review says "made-up records, not
yours" and offers "Tick to say the sample person has no known allergies".

The brief (same file, `appointment_brief`) takes tenant and kind from one
context, which is the shape this route should have. The move is injected
deterministically inside the ownership probe (`action_status`), the exact
window between the two reads. Synthetic data only.
"""

from __future__ import annotations

from tests.test_careagents import FakeClient, cfg, svc  # noqa: F401
from tests.test_careagents_beta_weekly import _real_agent


def test_voice_and_tenant_come_from_the_same_connection(
        cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    real_tenant = svc.get_agent_context(
        _account_id(svc, agent_id), agent_id)["tenant"]
    # The same account also holds the sample records.
    assert c.post("/api/connections/sample", json={}).status_code == 200
    acct_id = _account_id(svc, agent_id)
    sample_conn = svc.active_sample(acct_id)
    assert sample_conn and sample_conn["tenant_id"] != real_tenant

    seen = []
    original_status = FakeClient.action_status

    def status_then_move(self, tenant, action_id):
        out = original_status(self, tenant, action_id)
        # The patient's own "Change records" lands between the two reads.
        svc.move_agent(acct_id, agent_id, sample_conn["id"])
        return out

    def fetch(self, tenant, action_id, **kw):
        seen.append((tenant, kw))
        return 200, f"<html>/r6/actions/{action_id}/review</html>"

    monkeypatch.setattr(FakeClient, "action_status", status_then_move)
    monkeypatch.setattr(FakeClient, "fetch_review_page", fetch)

    assert c.get(f"/review/{agent_id}/act-1").status_code == 200
    assert len(seen) == 1
    tenant, kw = seen[0]
    # Either the review follows the move (sample tenant, sample voice) or it
    # stays on the real tenant in the patient's own voice. Never the mix.
    assert not (tenant == real_tenant and kw.get("voice") == "sample"), (
        "real tenant's review fetched with the sample voice")


def _account_id(svc, agent_id):  # noqa: F811
    from careagents.accounts import Agent
    with svc.session() as s:
        return s.query(Agent).filter_by(id=agent_id).one().account_id
