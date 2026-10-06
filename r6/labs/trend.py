# r6/labs/trend.py
"""Creatinine trend check against KDIGO 2012 AKI criteria — pure engine.

KDIGO defines AKI by TWO SEPARATE creatinine criteria, and this module keeps
them separate rather than folding them into one rule:

    A  a rise of >= 0.3 mg/dL within 48 hours
    B  a rise to >= 1.5 x baseline, known or presumed to have occurred
       within the prior 7 days

Each is evaluated on its own and the result says which fired. Outpatient
draws are usually days or weeks apart, so a 48-hour-only rule almost never
fires there; our physician advisor's example — 0.8 -> 1.3 mg/dL over six
days, 1.625 x — is criterion B, stage 1, and is invisible to A.

Comparability before arithmetic (#62). A value enters the arithmetic only
when it is a plain valueQuantity on LOINC 2160-0, in mg/dL or umol/L, with a
timezone-qualified effective time, and no comparator or dataAbsentReason.
Anything else is skipped with a reason, and when what is left cannot answer
the question the check abstains instead of guessing. "Did not fire" is
never reported as "kidneys fine": a criterion with no prior result inside
its window is `not-evaluable`, not `not-met`.

The analyte label comes from LOINC_RANGES keyed by code — never from the
Observation's own `display` or `code.text`, which is where real feeds put a
patient's name.

Decision support, not diagnosis.
"""
from datetime import datetime, timedelta

from r6.labs.interpret import LOINC_RANGES, LOINC_SYSTEM

#: Serum/plasma creatinine, the specimen KDIGO's thresholds are defined on.
CREATININE_LOINC = "2160-0"
#: Creatinine in whole blood (often point-of-care). Not mixed with serum:
#: KDIGO is defined on serum creatinine and we have no validated equivalence
#: between the two specimens, so these are skipped with a reason.
WHOLE_BLOOD_CREATININE_LOINC = "38483-4"

UCUM_SYSTEM = "http://unitsofmeasure.org"

_ANALYTE = LOINC_RANGES[CREATININE_LOINC]["name"]

#: mg/dL per unit. Creatinine molar mass 113.12 g/mol gives
#: 1 mg/dL = 88.4 umol/L — a creatinine-specific factor, not a general one
#: (source "si-creatinine" in interpret.REFERENCES). Both micro spellings
#: (U+00B5 micro sign, U+03BC Greek mu) appear in real feeds.
_TO_MG_DL = {"mg/dL": 1.0, "umol/L": 1 / 88.4, "µmol/L": 1 / 88.4,
             "μmol/L": 1 / 88.4}

WINDOW_A = timedelta(hours=48)
WINDOW_B = timedelta(days=7)
RISE_A_MG_DL = 0.3
RATIO_B = 1.5
STAGE_3_ABSOLUTE_MG_DL = 4.0

_NOT_A_RESULT = {"entered-in-error", "cancelled"}

#: Why a creatinine result was left out of the arithmetic.
NOT_SERUM = "not-serum-creatinine"
AMBIGUOUS_TIME = "ambiguous-time"
DATA_ABSENT = "data-absent"
NO_NUMERIC_VALUE = "no-numeric-value"
CENSORED = "censored-value"
UNIT_NOT_COMPARABLE = "unit-not-comparable"
NOT_A_RESULT = "not-a-result"

#: Why the check as a whole, or one criterion, could not be decided.
LATEST_NOT_COMPARABLE = "latest-not-comparable"
INSUFFICIENT = "insufficient-comparable-results"
NO_PRIOR_IN_WINDOW = "no-prior-in-window"

_BASELINE_NOTE = (
    "Criterion B baseline is the lowest comparable creatinine in the 7 days "
    "before the latest result (inclusive of exactly 7 days); criterion A "
    "compares against the lowest comparable result in the 48 hours before "
    "it. Results that were censored (<, >), absent, in another unit, or "
    "without a timezone-qualified time were not used.")


def _loinc(obs):
    for c in obs.get("code", {}).get("coding", []):
        if c.get("system") == LOINC_SYSTEM and c.get("code"):
            return c["code"]
    return None


def _when(obs):
    """A timezone-aware datetime, or None when the time is missing or too
    coarse to place a result inside a 48-hour window.

    A date-only value ("2026-09-01") or a time with no offset is ambiguous by
    up to a day, and a naive datetime cannot be compared with an aware one.
    effectivePeriod is not used: which end the draw happened at is unknown.
    """
    raw = obs.get("effectiveDateTime") or obs.get("effectiveInstant")
    if not isinstance(raw, str) or "T" not in raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _unit(vq):
    """The coded UCUM unit when present, else the human-readable unit."""
    if vq.get("system") == UCUM_SYSTEM and vq.get("code"):
        return vq["code"]
    return vq.get("unit")


def _gate(obs):
    """(value in mg/dL, None) for a comparable result, else (None, reason)."""
    if obs.get("dataAbsentReason"):
        return None, DATA_ABSENT
    vq = obs.get("valueQuantity")
    if not isinstance(vq, dict):
        return None, NO_NUMERIC_VALUE
    value = vq.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, NO_NUMERIC_VALUE
    if vq.get("comparator"):
        # "<0.5" is a bound, not the point 0.5 — subtracting it would
        # manufacture a rise.
        return None, CENSORED
    factor = _TO_MG_DL.get(_unit(vq))
    if factor is None:
        return None, UNIT_NOT_COMPARABLE
    return round(value * factor, 3), None


def _point(obs, when, mg_dl):
    return {"id": obs.get("id"), "effective": when.isoformat(),
            "value_mg_dl": mg_dl,
            "original_unit": _unit(obs.get("valueQuantity") or {})}


def _lowest_prior(points, latest_when, window):
    """The lowest result strictly before the latest and no more than
    `window` before it. Ties go to the most recent."""
    prior = [p for p in points
             if p[0] < latest_when and latest_when - p[0] <= window]
    if not prior:
        return None
    return min(prior, key=lambda p: (p[1], -p[0].timestamp()))


def _not_evaluable(reason):
    return {"status": "not-evaluable", "reason": reason}


def _stage_for_ratio(ratio):
    if ratio >= 3.0:
        return 3
    if ratio >= 2.0:
        return 2
    return 1


def _abstain(reason, skipped):
    return {"check": "kdigo-aki-creatinine", "analyte": _ANALYTE,
            "loinc": CREATININE_LOINC, "source": "kdigo-2012",
            "status": "abstained", "abstained_reason": reason,
            "kdigo_criterion": [], "stage": None,
            "criteria": {"A": _not_evaluable(reason),
                         "B": _not_evaluable(reason)},
            "latest": None, "skipped": skipped, "note": _BASELINE_NOTE}



def evaluate_creatinine_aki(observations):
    """Evaluate KDIGO criteria A and B on one patient's Observations.

    The caller must pass ONE patient's results; this function cannot tell two
    people apart. Returns None when there is no creatinine at all, else a
    dict carrying `kdigo_criterion` (the criteria met, e.g. ["A", "B"]),
    `stage` (1-3 or None), per-criterion detail with the compared results,
    and `skipped` (id + reason for every result left out).
    """
    points, unusable, skipped, seen = [], [], [], False
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        code = _loinc(obs)
        if code == WHOLE_BLOOD_CREATININE_LOINC:
            seen = True
            skipped.append({"id": obs.get("id"), "reason": NOT_SERUM})
            continue
        if code != CREATININE_LOINC:
            continue
        seen = True
        if obs.get("status") in _NOT_A_RESULT:
            skipped.append({"id": obs.get("id"), "reason": NOT_A_RESULT})
            continue
        when = _when(obs)
        if when is None:
            skipped.append({"id": obs.get("id"), "reason": AMBIGUOUS_TIME})
            continue
        mg_dl, reason = _gate(obs)
        if reason:
            skipped.append({"id": obs.get("id"), "reason": reason})
            unusable.append(when)
            continue
        points.append((when, mg_dl, obs))
    if not seen:
        return None
    if not points:
        return _abstain(INSUFFICIENT, skipped)

    points.sort(key=lambda p: p[0])
    latest_when, latest_val, latest_obs = points[-1]
    # The most recent result is the one the question is about. If it is
    # censored or otherwise not comparable, an older "latest" would answer a
    # question about the past as if it were the present.
    if any(u >= latest_when for u in unusable):
        return _abstain(LATEST_NOT_COMPARABLE, skipped)
    if len(points) < 2:
        return _abstain(INSUFFICIENT, skipped)

    # Rounded before comparing: in binary floating point 1.2 - 0.9 is
    # 0.29999999999999993 and 1.2 / 0.8 is 1.4999999999999998, so a result
    # exactly on a KDIGO threshold would otherwise miss it.
    criteria, met, stages = {}, [], []

    prior_a = _lowest_prior(points, latest_when, WINDOW_A)
    if prior_a is None:
        criteria["A"] = _not_evaluable(NO_PRIOR_IN_WINDOW)
    else:
        delta = round(latest_val - prior_a[1], 3)
        ok = delta >= RISE_A_MG_DL
        criteria["A"] = {"status": "met" if ok else "not-met",
                         "prior": _point(prior_a[2], prior_a[0], prior_a[1]),
                         "rise_mg_dl": delta,
                         "elapsed_hours": round(
                             (latest_when - prior_a[0]).total_seconds() / 3600, 1)}
        if ok:
            met.append("A")
            stages.append(1)

    baseline = _lowest_prior(points, latest_when, WINDOW_B)
    if baseline is None:
        criteria["B"] = _not_evaluable(NO_PRIOR_IN_WINDOW)
    else:
        ratio = round(latest_val / baseline[1], 3) if baseline[1] > 0 else None
        ok = ratio is not None and ratio >= RATIO_B
        criteria["B"] = {"status": "met" if ok else "not-met",
                         "baseline": _point(baseline[2], baseline[0], baseline[1]),
                         "ratio": ratio,
                         "elapsed_hours": round(
                             (latest_when - baseline[0]).total_seconds() / 3600, 1)}
        if ok:
            met.append("B")
            stages.append(_stage_for_ratio(ratio))

    stage = max(stages) if stages else None
    if stage is not None and latest_val >= STAGE_3_ABSOLUTE_MG_DL:
        stage = 3  # KDIGO stage 3 includes a rise to >= 4.0 mg/dL
    return {"check": "kdigo-aki-creatinine", "analyte": _ANALYTE,
            "loinc": CREATININE_LOINC, "source": "kdigo-2012",
            "status": "evaluated", "abstained_reason": None,
            "kdigo_criterion": met, "stage": stage, "criteria": criteria,
            "latest": _point(latest_obs, latest_when, latest_val),
            "skipped": skipped, "note": _BASELINE_NOTE}


def _fmt(mg_dl):
    return f"{mg_dl:.2f}".rstrip("0").rstrip(".")


def _span(hours):
    if hours < 48:
        n = round(hours)
        return f"{n} hour" + ("" if n == 1 else "s")
    n = round(hours / 24)
    return f"{n} days"


def kdigo_consumer_line(result):
    """The plain-language sentence for a fired check, or None.

    Says what the numbers did and what to do, never a diagnosis. Criterion
    B's baseline is quoted when B fired, since it is the lower of the two
    comparisons; otherwise criterion A's.
    """
    if not result or not result.get("kdigo_criterion"):
        return None
    crit = result["criteria"]["B" if "B" in result["kdigo_criterion"] else "A"]
    before = crit.get("baseline") or crit.get("prior")
    latest = result["latest"]
    return {"analyte": result["analyte"], "check": result["check"],
            "message": (
                f"Your {result['analyte'].lower()} rose from "
                f"{_fmt(before['value_mg_dl'])} to "
                f"{_fmt(latest['value_mg_dl'])} mg/dL in "
                f"{_span(crit['elapsed_hours'])}. A rise like this can mean "
                f"the kidneys are under strain. Contact your clinician "
                f"promptly.")}
