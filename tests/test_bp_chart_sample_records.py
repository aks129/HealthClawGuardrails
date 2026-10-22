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


def test_the_footnote_claims_a_range_check_only_where_one_ran(
        cfg, svc, monkeypatch):  # noqa: F811
    """QA #1021: under a BP chart, where the engine scored nothing, "each
    result is compared with typical adult ranges" is untrue.

    MUTATION: always send CHART_FOOTNOTE -> red."""
    from careagents.app import CHART_FOOTNOTE
    chain = Chain(cfg, svc, monkeypatch)

    def footnote(topic=None):
        params = {"agent": chain.agent}
        if topic:
            params["topic"] = topic
        return chain.s.get(f"{BASE}/api/labs/timeline",
                           params=params).json()["disclaimer"]

    bp = footnote("blood pressure")
    assert "not a diagnosis" in bp
    assert "typical adult ranges" not in bp
    assert "not compared with any range" in bp
    # A scored lab keeps the footnote it had.
    assert footnote("cholesterol") == CHART_FOOTNOTE
    # Both on one card: the range sentence, and which readings it skips.
    both = footnote()
    assert "typical adult ranges" in both
    assert "not compared with any range" in both


def test_an_unflagged_point_on_a_ranged_series_gets_its_caption():
    """QA #1021 R14: one grey (IND) dot on an otherwise scored series had
    no caption, because only a wholly unscored series counted.

    MUTATION: decide by series, not by point -> red."""
    from careagents.app import (CHART_FOOTNOTE, CHART_FOOTNOTE_MIXED,
                                CHART_FOOTNOTE_UNSCORED, _chart_footnote)

    def series(*flags):
        return {"readings": [{"flag": f} for f in flags]}

    assert _chart_footnote([series("H", "N")]) == CHART_FOOTNOTE
    assert _chart_footnote([series("H", "IND")]) == CHART_FOOTNOTE_MIXED
    assert _chart_footnote([series("IND"), series("N")]) == \
        CHART_FOOTNOTE_MIXED
    assert _chart_footnote([series("IND", "IND")]) == CHART_FOOTNOTE_UNSCORED
    assert _chart_footnote([]) == CHART_FOOTNOTE


def test_the_brief_still_lists_every_sample_lab_beside_the_bp_history():
    """QA #1021: the brief's Recent labs is capped at ten readings, newest
    first. The BP history must not push another test off it."""
    from r6.brief.engine import build_labs
    observations = [r for r in _built_in_resources()
                    if r["resourceType"] == "Observation"]
    labels = {f.label for f in build_labs(observations)}
    assert {"Glucose (blood)", "Hemoglobin A1c", "Creatinine",
            "LDL cholesterol (calculated)", "Cholesterol (total)",
            "Blood pressure"} <= labels


def test_the_timeline_tool_offers_blood_pressure():
    """QA #1021 F2: a description listing only labs sent "how's my blood
    pressure?" to get_labs instead of the chart."""
    from careagents.agent import TOOLS
    [tool] = [t for t in TOOLS if t["name"] == "show_lab_timeline"]
    assert "blood pressure" in tool["description"]


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
