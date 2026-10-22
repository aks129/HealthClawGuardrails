"""Security re-review of PR #1021 at e792289d: what the guards left open.

The malformed-component and canary sets in
tests/test_sec1021_bp_component_exploits.py pass at e792289d. Two things
remained, pinned as strict xfail at first and now fixed on the PR:

1. `careagents/agent.py::_obs_flag` iterates `interpretation` unguarded.
   It runs on EVERY Observation in the `$interpret` bundle
   (`_latest_per_analyte`, `_not_evaluated`). The engine rewrites
   `interpretation` only on a reading it scored, so an unscored one (a BP
   panel, any analyte the engine has no range for) reaches it as the feed
   sent it. Through the real upload ingest, `get_labs` raises. Pre-existing
   on main; not a #1021 regression.
2. `r6/seed.py::_stored_bp_date` (new in #1021) anchors the BP history to
   the stored `demo-obs-bp` date. A stored year-1 date parses, and
   `anchor - timedelta(days=540)` raises OverflowError, so the seed aborts.
   Reaching it needs a write into one's own sample tenant.
"""
from __future__ import annotations

import json

import pytest

from tests.test_beta_acceptance_rows import Chain, TENANT
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

LOINC = "http://loinc.org"

_SHAPES = {
    "str": "high",
    "int": 7,
    "true": True,
    "list-of-str": ["H"],
    "dict": {"coding": [{"code": "H"}]},
    "coding-int": [{"coding": 5}],
}


def _obs(rid, code, interpretation):
    res = {"resourceType": "Observation", "id": rid, "status": "final",
           "code": {"coding": [{"system": LOINC, "code": code}]},
           "effectiveDateTime": "2026-03-01T00:00:00Z",
           "interpretation": interpretation}
    if code == "85354-9":
        res["component"] = [
            {"code": {"coding": [{"system": LOINC, "code": part}]},
             "valueQuantity": {"value": value, "unit": "mmHg"}}
            for part, value in (("8480-6", 140), ("8462-4", 90))]
    else:
        res["valueQuantity"] = {"value": 99, "unit": "mg/dL"}
    return res


@pytest.mark.parametrize("code", ["85354-9", "99999-9"])
@pytest.mark.parametrize("shape", list(_SHAPES))
def test_get_labs_survives_a_malformed_unscored_interpretation(
        cfg, svc, monkeypatch, shape, code):  # noqa: F811
    from careagents import agent
    chain = Chain(cfg, svc, monkeypatch)
    rid = f"interp-{shape}-{code}"
    out = chain.hc.ingest_bundle(TENANT, {
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": _obs(rid, code, _SHAPES[shape])}]})
    assert out.get("ingested") == 1, out
    # The row reached the engine's answer unscored, as the feed sent it.
    assert rid in json.dumps(chain.hc.interpret_labs(TENANT))
    result = json.loads(agent._execute_tool(chain.hc, TENANT, "get_labs",
                                            {}, []))
    assert isinstance(result, dict)


def test_a_scored_reading_with_a_malformed_interpretation_is_safe(
        cfg, svc, monkeypatch):  # noqa: F811
    """Control: the engine rewrites `interpretation` on a reading it
    scores (r6/labs/report.py annotate_observation), so glucose is safe."""
    from careagents import agent
    chain = Chain(cfg, svc, monkeypatch)
    chain.hc.ingest_bundle(TENANT, {
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": _obs("interp-glucose", "2345-7", "high")}]})
    json.loads(agent._execute_tool(chain.hc, TENANT, "get_labs", {}, []))


def test_a_year_one_stored_bp_date_does_not_abort_the_seed(app):
    from models import db
    from r6.models import R6Resource
    from r6.seed import seed_demo_data
    with app.app_context():
        res = {"resourceType": "Observation", "id": "demo-obs-bp",
               "status": "final", "effectiveDateTime": "0001-01-05T00:00:00Z"}
        db.session.add(R6Resource("Observation", json.dumps(res),
                                  resource_id="demo-obs-bp",
                                  tenant_id="anchor-1021"))
        db.session.commit()
        assert seed_demo_data("anchor-1021") > 0


@pytest.mark.parametrize("when", ["9999-12-31T00:00:00Z", "2026-01-01",
                                  "nonsense", None, 5])
def test_other_odd_stored_bp_dates_seed_cleanly(app, when):
    from models import db
    from r6.models import R6Resource
    from r6.seed import seed_demo_data
    with app.app_context():
        res = {"resourceType": "Observation", "id": "demo-obs-bp",
               "status": "final", "effectiveDateTime": when}
        db.session.add(R6Resource("Observation", json.dumps(res),
                                  resource_id="demo-obs-bp",
                                  tenant_id="anchor-1021"))
        db.session.commit()
        seed_demo_data("anchor-1021")
        rows = R6Resource.query.filter_by(tenant_id="anchor-1021").filter(
            R6Resource.id.like("demo-obs-bp-%")).all()
        assert len(rows) == 6
