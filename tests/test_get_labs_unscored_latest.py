"""get_labs gives each unscored analyte once: its latest reading (QA #1021 F2).

With the sample's BP history, "how's my blood pressure?" went to get_labs,
which listed all seven unscored BP readings in storage order. The model then
worked out the direction itself from raw dates, which is what
_timeline_in_words exists to stop (#942). Unscored readings now collapse the
way scored ones do (_latest_per_analyte): the latest, with its date, and a
count of earlier readings.
"""
from __future__ import annotations

import json

from careagents.agent import _execute_tool, _not_evaluated
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

LOINC = "http://loinc.org"


def _bp(date, sys_value, dia_value):
    def part(code, value):
        return {"code": {"coding": [{"system": LOINC, "code": code}]},
                "valueQuantity": {"value": value, "unit": "mmHg"}}
    return {"resource": {
        "resourceType": "Observation",
        "code": {"coding": [{"system": LOINC, "code": "55284-4"}]},
        "effectiveDateTime": date,
        "component": [part("8480-6", sys_value), part("8462-4", dia_value)]}}


def test_unscored_readings_collapse_to_the_latest_by_date():
    """MUTATION: one entry per reading -> red. MUTATION: take the last in
    storage order instead of by date -> red."""
    bundle = {"entry": [_bp("2026-05-01", 131, 83), _bp("2026-10-01", 138, 88),
                        _bp("2025-01-01", 146, 92)]}
    consumer = {"unevaluated_analytes": ["Blood pressure"] * 3,
                "unevaluated_codes": ["55284-4"] * 3}
    [bp] = _not_evaluated(consumer, bundle)
    assert (bp["value"], bp["date"]) == ("138/88", "2026-10-01")
    assert bp["earlier_readings"] == 2


def test_get_labs_on_the_sample_lists_blood_pressure_once(cfg, svc,  # noqa: F811
                                                          monkeypatch):
    chain = Chain(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   surface="web"))
    rows = out["not_evaluated"]
    assert [r["analyte"] for r in rows].count("Blood pressure") == 1
    [bp] = [r for r in rows if r["analyte"] == "Blood pressure"]
    assert bp["value"] == "138/88"
    assert bp["earlier_readings"] == 6

    # QA #1021: the summary beside the rows counted readings (8, with seven
    # "Blood pressure" names) while the note counted tests. One count now.
    summary = out["consumer_summary"]
    assert summary["unevaluated_count"] == len(rows) == 2
    assert summary["unevaluated_analytes"] == [r["analyte"] for r in rows]
    assert len(summary["unevaluated_codes"]) == len(rows)
    assert "2 test(s) in not_evaluated" in out["note"]
    # R9: the counted earlier readings must not become a direction.
    assert ("earlier_readings are counted, not listed, so give no "
            "direction from them") in out["note"]
