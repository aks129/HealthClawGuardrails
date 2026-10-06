"""The sample records can show a trend.

A demo of "Has my cholesterol changed?" needs more than one reading. The
built-in seed (r6/seed.py, which CareAgents' sample connection posts to via
/internal/seed) carries dated series over about 18 months: LDL and total
cholesterol improving, A1c, and a creatinine series that is stable and then
rises within a week, so the KDIGO check (#865) has something to show.

All values are synthetic. Labels come from r6/terminology.py by code, so the
new Observations carry no `display` at all.
"""

from __future__ import annotations

import json
from datetime import datetime

from r6.labs.trend import evaluate_creatinine_aki
from r6.seed import _built_in_resources

UCUM = "http://unitsofmeasure.org"


def _obs(code):
    return [r for r in _built_in_resources()
            if r.get("resourceType") == "Observation"
            and any(c.get("code") == code
                    for c in (r.get("code") or {}).get("coding") or [])]


def _when(o):
    return datetime.fromisoformat(o["effectiveDateTime"].replace("Z", "+00:00"))


def test_each_trend_series_has_several_dated_readings():
    for code, at_least in (("13457-7", 4), ("2093-3", 4), ("4548-4", 4),
                           ("2160-0", 4)):
        assert len(_obs(code)) >= at_least, code


def test_cholesterol_improves_over_about_eighteen_months():
    for code in ("13457-7", "2093-3"):
        series = sorted(_obs(code), key=_when)
        values = [o["valueQuantity"]["value"] for o in series]
        assert values == sorted(values, reverse=True), code
        span = _when(series[-1]) - _when(series[0])
        assert 450 <= span.days <= 600, (code, span.days)


def test_every_new_reading_is_synthetic_shaped_and_terminology_labelled():
    seen = 0
    for r in _built_in_resources():
        if r.get("resourceType") != "Observation" or "valueQuantity" not in r:
            continue
        seen += 1
        assert r["id"].startswith("demo-obs-")          # stable id (#457)
        vq = r["valueQuantity"]
        assert vq["unit"] and vq["system"] == UCUM and vq["code"]
        assert _when(r).tzinfo is not None, r["id"]
        if r["id"] not in ("demo-obs-glucose", "demo-obs-a1c"):
            # New rows: no display text at all. The label is the code's.
            for c in r["code"]["coding"]:
                assert "display" not in c, r["id"]
            assert "text" not in r["code"]
    assert seen >= 18


def test_ids_are_unique():
    ids = [r.get("id") for r in _built_in_resources()]
    assert len(ids) == len(set(ids))


def test_the_creatinine_rise_meets_kdigo_criterion_b():
    result = evaluate_creatinine_aki(json.loads(json.dumps(_obs("2160-0"))))
    assert result["status"] == "evaluated"
    assert "B" in result["kdigo_criterion"]
    assert result["stage"] == 1
