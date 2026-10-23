"""QA on #1021 (the blood pressure chart on the sample records).

Mutation testing of the PR left four behaviours it relies on unpinned:

- a part takes its own flag, never the panel's (the code says so; no test
  set a flag on a panel, so reading the panel's flag passed every test);
- a malformed component or a non-object valueQuantity is skipped, not
  raised on (both guards could be deleted with the suite green);
- the seeded history stays OLDER than demo-obs-bp, which is what keeps care
  gaps, the BP-control measure and get_labs on 138/88. Dating one history
  panel after it passed every test, on the engine too.
"""
from __future__ import annotations

import json
from datetime import datetime

from careagents.agent import _execute_tool
from careagents.labs_timeline import build_series, keys_for_topic
from r6.seed import _built_in_resources
from tests.test_beta_acceptance_rows import BASE, TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

LOINC = "http://loinc.org"
_PANELS = {"85354-9", "55284-4"}


def _part(code, value, flag=None, **kw):
    part = {"code": {"coding": [{"system": LOINC, "code": code}]},
            "valueQuantity": {"value": value, "unit": "mmHg"}}
    if flag:
        part["interpretation"] = [{"coding": [{"code": flag}]}]
    part.update(kw)
    return part


def _panel(components, flag=None, date="2026-03-01"):
    res = {"resourceType": "Observation",
           "code": {"coding": [{"system": LOINC, "code": "85354-9"}]},
           "effectiveDateTime": date, "component": components}
    if flag:
        res["interpretation"] = [{"coding": [{"code": flag}]}]
    return {"resource": res}


def _bp_series(bundle):
    return {s["key"]: s for s in build_series(
        bundle, keys_for_topic("blood pressure"))}


def test_a_panel_flag_is_not_copied_onto_its_parts():
    """MUTATION: `_flag_of(resource)` for `_flag_of(source)` -> red. A panel
    flagged high says nothing about which part was."""
    got = _bp_series({"entry": [_panel(
        [_part("8480-6", 150, flag="H"), _part("8462-4", 78)], flag="H")]})
    assert got["systolic"]["readings"][0]["flag"] == "H"
    assert got["diastolic"]["readings"][0]["flag"] == "IND"


def test_a_malformed_component_is_skipped_not_raised_on():
    """MUTATION: drop the isinstance(c, dict) guard -> AttributeError."""
    got = _bp_series({"entry": [_panel(
        ["138/88", None, _part("8480-6", 138), _part("8462-4", 88)])]})
    assert got["systolic"]["readings"][0]["value"] == 138
    assert got["diastolic"]["readings"][0]["value"] == 88


def test_a_non_object_value_quantity_is_skipped_not_raised_on():
    """MUTATION: drop the isinstance(quantity, dict) guard ->
    AttributeError. Upstream can put any JSON in the field."""
    got = _bp_series({"entry": [
        _panel([_part("8480-6", 0, valueQuantity="138 mmHg"),
                _part("8462-4", 88)], date="2026-03-01"),
        _panel([_part("8480-6", 131), _part("8462-4", 83)],
               date="2026-04-01")]})
    assert [r["value"] for r in got["systolic"]["readings"]] == [131]
    assert [r["value"] for r in got["diastolic"]["readings"]] == [88, 83]


def _when(res):
    return datetime.strptime(res["effectiveDateTime"], "%Y-%m-%dT%H:%M:%SZ")


def test_the_seeded_history_stays_older_than_the_latest_reading():
    """MUTATION: date a history panel after demo-obs-bp -> red. Care gaps,
    the BP-control measure and get_labs read the latest panel; the PR's
    promise is that they still see 138/88."""
    panels = [r for r in _built_in_resources()
              if r.get("resourceType") == "Observation"
              and {c.get("code") for c in r["code"]["coding"]} & _PANELS]
    latest = next(p for p in panels if p["id"] == "demo-obs-bp")
    history = [p for p in panels if p["id"] != "demo-obs-bp"]
    assert history, "the sample records hold a BP history"
    assert all(_when(p) < _when(latest) for p in history)
    values = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"]
              for c in latest["component"]}
    assert values == {"8480-6": 138, "8462-4": 88}


def test_on_the_real_engine_the_latest_bp_is_still_138_over_88(
        cfg, svc, monkeypatch):  # noqa: F811
    """End to end, order-independent: the chart's last point and the
    get_labs reading with the latest date are both 138/88."""
    chain = Chain(cfg, svc, monkeypatch)
    r = chain.s.get(f"{BASE}/api/labs/timeline",
                    params={"agent": chain.agent, "topic": "blood pressure"})
    assert r.status_code == 200, r.text
    last = {s["key"]: s["readings"][-1]["value"] for s in r.json()["series"]}
    assert last == {"systolic": 138, "diastolic": 88}

    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   surface="web"))
    bps = [x for x in out["not_evaluated"] if x["analyte"] == "Blood pressure"]
    assert max(bps, key=lambda x: x["date"])["value"] == "138/88"
