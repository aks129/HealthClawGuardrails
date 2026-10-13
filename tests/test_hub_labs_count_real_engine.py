"""The chat's "Explain my latest labs" tap asks the real engine one count
(QA #944 F2), and these drive the real HealthClawClient onto the real
engine WSGI so that count is the engine's, not a fake's.

The engine's `code` filter matches `"code":"<value>"` anywhere in the
resource, a category's coding included, so `code=laboratory` counts the
Observations filed as labs. If the engine's filter ever narrows to
`Observation.code` alone, this file fails rather than the tap silently
vanishing. Synthetic data only.
"""

from __future__ import annotations

from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

CAT = "http://terminology.hl7.org/CodeSystem/observation-category"


def _obs(oid, loinc, category):
    return {"resource": {
        "resourceType": "Observation", "id": oid, "status": "final",
        "category": [{"coding": [{"system": CAT, "code": category}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
        "valueQuantity": {"value": 1, "unit": "1"}}}


def _labs(chain):
    return chain.hc.search(TENANT, "Observation",
                           {"code": "laboratory", "_summary": "count"})


def test_the_laboratory_count_counts_labs_and_not_vitals(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    # The sample seed files nothing by category: no lab counted, and no
    # resource came back, only a total.
    before = _labs(chain)
    assert before["total"] == 0 and not before.get("entry")
    chain.hc.ingest_bundle(TENANT, {
        "resourceType": "Bundle", "type": "collection", "entry": [
            _obs("qa944-hr-1", "8867-4", "vital-signs"),
            _obs("qa944-hr-2", "8867-4", "vital-signs"),
            _obs("qa944-cr-1", "2160-0", "laboratory"),
            _obs("qa944-k-1", "2823-3", "laboratory")]})
    after = _labs(chain)
    assert after["total"] == 2 and not after.get("entry")
