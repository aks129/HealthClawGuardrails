"""The blood pressure chart on the sample records (QA 2026-10-22).

A tester on the sample records opened the chart for "blood pressure" and was
told there were no readings. The records held one, which the chart could
not read, and one reading draws no trend anyway. These pin the sample's BP
history and the chart endpoint on the real engine.
"""
from __future__ import annotations

from r6.seed import _built_in_resources
from tests.test_beta_acceptance_rows import BASE, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

_PANELS = {"85354-9", "55284-4"}


def _bp_panels():
    out = []
    for res in _built_in_resources():
        codes = {c.get("code") for c in
                 ((res.get("code") or {}).get("coding") or [])}
        if res.get("resourceType") == "Observation" and codes & _PANELS:
            out.append(res)
    return out


def test_the_sample_records_hold_a_dated_bp_history():
    """MUTATION: drop the seeded BP series -> red."""
    panels = _bp_panels()
    assert len({p["effectiveDateTime"][:10] for p in panels}) >= 4
    for p in panels:
        assert p.get("id"), "a seeded resource without an id is re-inserted"
        parts = {c["code"]["coding"][0]["code"] for c in p["component"]}
        assert parts == {"8480-6", "8462-4"}


def test_the_bp_history_carries_no_upstream_display():
    """Labels come from r6/terminology.py by code, never from the record."""
    for p in _bp_panels():
        if p["id"] == "demo-obs-bp":
            continue  # predates the rule; redaction strips it on read
        codings = list(p["code"]["coding"])
        for c in p["component"]:
            codings += c["code"]["coding"]
        assert all("display" not in c for c in codings)
        assert "text" not in p["code"]


def test_the_bp_chart_on_the_real_engine_has_readings(cfg, svc,  # noqa: F811
                                                       monkeypatch):
    """What the card fetches, end to end: sample connect, $interpret on the
    real engine, series built in CareAgents."""
    chain = Chain(cfg, svc, monkeypatch)
    r = chain.s.get(f"{BASE}/api/labs/timeline",
                    params={"agent": chain.agent, "topic": "blood pressure"})
    assert r.status_code == 200, r.text
    series = r.json()["series"]
    assert [s["name"] for s in series] == \
        ["Systolic blood pressure", "Diastolic blood pressure"]
    for s in series:
        assert len(s["readings"]) >= 4
        assert s["trend_plottable"] is True
        # One unit across the series, the existing reading's included: the
        # agent gives no direction across units that differ as strings
        # (agent._timeline_in_words), and "mm[Hg]" is not "mmHg".
        assert {r["unit"] for r in s["readings"]} == {"mmHg"}
