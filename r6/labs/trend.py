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

The end point is always the latest comparable result. A rise that has
already come back down (0.8 -> 1.5 -> 1.0 within 5 days) is B not-met: the
question is about the current value, not the peak.

Comparability before arithmetic (#62). A value enters the arithmetic only
when it is a plain, positive, finite valueQuantity on LOINC 2160-0, in mg/dL
or umol/L, with a timezone-qualified effective time, and no comparator or
dataAbsentReason. Anything else is skipped with a reason, and when what is
left cannot answer the question the check abstains instead of guessing.
"Did not fire" is never reported as "kidneys fine": a criterion with no
prior result inside its window is `not-evaluable`, not `not-met`.

Every field is read defensively: the write API stores what it is given, so
a list where a string belongs must be skipped with a reason, never raise —
one malformed row otherwise turns the whole $interpret call into a 500.

The analyte label comes from LOINC_RANGES keyed by code — never from the
Observation's own `display` or `code.text`, which is where real feeds put a
patient's name.

Decision support, not diagnosis.
"""
import math
import re
from datetime import date, datetime, timedelta, timezone

from r6.labs.interpret import LOINC_RANGES, LOINC_SYSTEM

#: Serum/plasma creatinine, the specimen KDIGO's thresholds are defined on.
CREATININE_LOINC = "2160-0"
#: Creatinine in whole blood (often point-of-care). Not mixed with serum:
#: KDIGO is defined on serum creatinine and we have no validated equivalence
#: between the two specimens, so these are skipped with a reason.
WHOLE_BLOOD_CREATININE_LOINC = "38483-4"

UCUM_SYSTEM = "http://unitsofmeasure.org"

_ANALYTE = LOINC_RANGES[CREATININE_LOINC]["name"]

#: umol/L per mg/dL. Creatinine molar mass 113.12 g/mol gives
#: 1 mg/dL = 88.4 umol/L — a creatinine-specific factor, not a general one
#: (source "si-creatinine" in interpret.REFERENCES).
UMOL_PER_MG_DL = 88.4
#: Accepted units, mapped to the canonical name used for quoting. Both micro
#: spellings (U+00B5 micro sign, U+03BC Greek mu) appear in real feeds.
_UNITS = {"mg/dL": "mg/dL", "umol/L": "umol/L", "µmol/L": "umol/L",
          "μmol/L": "umol/L"}

WINDOW_A = timedelta(hours=48)
WINDOW_B = timedelta(days=7)
#: KDIGO 2012 writes criterion A as ">=0.3 mg/dL (>=26.5 umol/L)"
#: (Recommendation 2.1.1; Table 2). The two are not equal after conversion
#: (26.5 / 88.4 = 0.2998), so a pair the lab reported in umol/L is held to
#: the umol/L figure and every other pair to the mg/dL one.
RISE_A_MG_DL = 0.3
RISE_A_UMOL_L = 26.5
RATIO_B = 1.5
STAGE_3_ABSOLUTE_MG_DL = 4.0
#: Above this a serum creatinine is a unit or entry error, not a result:
#: 40 mg/dL = 3536 umol/L, beyond any reported clinical value.
MAX_PLAUSIBLE_MG_DL = 40.0
#: Below this a serum creatinine is a unit or entry error too: 0.1 mg/dL =
#: 8.84 umol/L. It also keeps a near-zero baseline from producing a huge or
#: infinite ratio. An interim value; our physician advisor may tune it.
MIN_PLAUSIBLE_MG_DL = 0.1
#: Threshold comparisons allow this much binary floating-point error, and no
#: more: 1.2 / 0.8 is 1.4999999999999998 and must count as 1.5, while
#: 1.999 / 1.333 is 1.4996 and must not. Rounding to a few places, which
#: this replaced, got the second case wrong.
_EPS = 1e-9
#: A consumer line about a result older than this is softened from "contact
#: your clinician promptly" to "ask whether this was followed up".
STALE_AFTER = timedelta(days=30)

_NOT_A_RESULT = {"entered-in-error", "cancelled"}

#: Why a creatinine result was left out of the arithmetic.
NOT_SERUM = "not-serum-creatinine"
AMBIGUOUS_TIME = "ambiguous-time"
DATA_ABSENT = "data-absent"
NO_NUMERIC_VALUE = "no-numeric-value"
IMPLAUSIBLE = "implausible-value"
CENSORED = "censored-value"
UNIT_NOT_COMPARABLE = "unit-not-comparable"
NOT_A_RESULT = "not-a-result"
MALFORMED = "malformed-field"

#: Why the check as a whole, or one criterion, could not be decided.
LATEST_NOT_COMPARABLE = "latest-not-comparable"
NEWER_UNUSABLE = "newer-result-unusable"
INSUFFICIENT = "insufficient-comparable-results"
NO_PRIOR_IN_WINDOW = "no-prior-in-window"
BASELINE_NOT_USABLE = "baseline-not-usable"

_BASELINE_NOTE = (
    "Criterion B baseline is the lowest comparable creatinine in the 7 days "
    "before the latest result (inclusive of exactly 7 days); criterion A "
    "compares against the lowest comparable result in the 48 hours before "
    "it. The end point is the latest comparable result, so a rise that has "
    "already come back down is not reported. Results that were censored "
    "(<, >), absent, non-positive, in another unit, or without a "
    "timezone-qualified time were not used.")


def _str(value):
    return value if isinstance(value, str) else None


def _id(obs):
    return _str(obs.get("id"))


def _loinc(obs):
    code = obs.get("code")
    coding = code.get("coding") if isinstance(code, dict) else None
    if not isinstance(coding, list):
        return None
    for c in coding:
        if isinstance(c, dict) and c.get("system") == LOINC_SYSTEM \
                and _str(c.get("code")):
            return c["code"]
    return None


def _raw_time(obs):
    return _str(obs.get("effectiveDateTime")) or _str(obs.get("effectiveInstant"))


def _when(obs):
    """A timezone-aware datetime, or None when the time is missing or too
    coarse to place a result inside a 48-hour window.

    A date-only value ("2026-09-01") or a time with no offset is ambiguous by
    up to a day, and a naive datetime cannot be compared with an aware one.
    effectivePeriod is not used: which end the draw happened at is unknown.
    """
    raw = _raw_time(obs)
    if raw is None or "T" not in raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


_PARTIAL_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def _earliest_date(obs):
    """For a result `_when` could not place: the earliest calendar date it
    could have been drawn on, or None if even that is unreadable.

    Earliest, so "after the latest result" means definitely after: a bare
    year or a month that overlaps the latest result's date does not count.
    """
    raw = _raw_time(obs)
    if raw is None:
        return None
    if "T" in raw:
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None
    m = _PARTIAL_DATE.match(raw)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2] or 1), int(m[3] or 1))
    except ValueError:
        return None


def _unit(vq):
    """The coded UCUM unit when present, else the human-readable unit."""
    if vq.get("system") == UCUM_SYSTEM and _str(vq.get("code")):
        return vq["code"]
    return _str(vq.get("unit"))


def _gate(obs):
    """((mg/dL, value as float, canonical unit), None) for a comparable
    result, else (None, reason)."""
    if obs.get("dataAbsentReason"):
        return None, DATA_ABSENT
    vq = obs.get("valueQuantity")
    if not isinstance(vq, dict):
        return None, NO_NUMERIC_VALUE
    value = vq.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, NO_NUMERIC_VALUE
    # JSON integers are unbounded, and float(10**400) raises OverflowError.
    # Convert once, here, so nothing after this line does arithmetic on a
    # value that has not already been made a finite float.
    try:
        value = float(value)
    except (OverflowError, ValueError, TypeError):
        return None, IMPLAUSIBLE
    if not math.isfinite(value) or value <= 0:
        return None, IMPLAUSIBLE
    if vq.get("comparator"):
        # "<0.5" is a bound, not the point 0.5 — subtracting it would
        # manufacture a rise.
        return None, CENSORED
    unit = _UNITS.get(_unit(vq))
    if unit is None:
        return None, UNIT_NOT_COMPARABLE
    # Not rounded: rounding each value before comparing moved a 0.2998 mg/dL
    # rise onto the 0.3 threshold. Values are rounded only for display.
    mg_dl = value if unit == "mg/dL" else value / UMOL_PER_MG_DL
    # The lower bound takes the tolerance because 8.84 / 88.4 is
    # 0.09999999999999999; the upper one needs none (3536 / 88.4 is 40.0).
    if mg_dl > MAX_PLAUSIBLE_MG_DL or mg_dl < MIN_PLAUSIBLE_MG_DL - _EPS:
        return None, IMPLAUSIBLE
    return (mg_dl, value, unit), None


def _point(p):
    when, mg_dl, obs, value, unit = p
    return {"id": _id(obs), "effective": when.isoformat(),
            "value_mg_dl": round(mg_dl, 3), "value": value,
            "original_unit": unit}


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


def _at_least(value, threshold):
    return value >= threshold - _EPS


def _stage_for_ratio(ratio):
    if _at_least(ratio, 3.0):
        return 3
    if _at_least(ratio, 2.0):
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
    points, unusable, unplaced, skipped, seen = [], [], [], [], False
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        code = _loinc(obs)
        if code == WHOLE_BLOOD_CREATININE_LOINC:
            seen = True
            skipped.append({"id": _id(obs), "reason": NOT_SERUM})
            continue
        if code != CREATININE_LOINC:
            continue
        seen = True
        status = obs.get("status")
        if isinstance(status, str) and status in _NOT_A_RESULT:
            skipped.append({"id": _id(obs), "reason": NOT_A_RESULT})
            continue
        when = _when(obs)
        if when is None:
            skipped.append({"id": _id(obs), "reason": AMBIGUOUS_TIME})
            day = _earliest_date(obs)
            if day is not None:
                unplaced.append(day)
            continue
        if status is not None and not isinstance(status, str):
            # Could be anything, including entered-in-error. A timed result
            # we cannot read still counts as a draw, so it can block below.
            skipped.append({"id": _id(obs), "reason": MALFORMED})
            unusable.append(when)
            continue
        gated, reason = _gate(obs)
        if reason:
            skipped.append({"id": _id(obs), "reason": reason})
            unusable.append(when)
            continue
        mg_dl, value, unit = gated
        points.append((when, mg_dl, obs, value, unit))
    if not seen:
        return None
    if not points:
        return _abstain(INSUFFICIENT, skipped)

    points.sort(key=lambda p: p[0])
    latest = points[-1]
    latest_when, latest_val = latest[0], latest[1]
    # The most recent result is the one the question is about. If it is
    # censored or otherwise not comparable, an older "latest" would answer a
    # question about the past as if it were the present.
    if any(u >= latest_when for u in unusable):
        return _abstain(LATEST_NOT_COMPARABLE, skipped)
    # The same holds for a draw we can see but cannot place to the hour: if
    # it was on a later calendar date, the pair below is no longer current.
    if any(day > latest_when.date() for day in unplaced):
        return _abstain(NEWER_UNUSABLE, skipped)
    if len(points) < 2:
        return _abstain(INSUFFICIENT, skipped)

    criteria, met, stages = {}, [], []

    prior_a = _lowest_prior(points, latest_when, WINDOW_A)
    if prior_a is None:
        criteria["A"] = _not_evaluable(NO_PRIOR_IN_WINDOW)
    else:
        delta = latest_val - prior_a[1]
        if prior_a[4] == latest[4] == "umol/L":
            compared_in = "umol/L"
            ok = _at_least(latest[3] - prior_a[3], RISE_A_UMOL_L)
        else:
            compared_in = "mg/dL"
            ok = _at_least(delta, RISE_A_MG_DL)
        criteria["A"] = {"status": "met" if ok else "not-met",
                         "prior": _point(prior_a),
                         "rise_mg_dl": round(delta, 4),
                         "compared_in": compared_in,
                         "elapsed_hours": round(
                             (latest_when - prior_a[0]).total_seconds() / 3600, 2)}
        if ok:
            met.append("A")
            stages.append(1)

    baseline = _lowest_prior(points, latest_when, WINDOW_B)
    if baseline is None:
        criteria["B"] = _not_evaluable(NO_PRIOR_IN_WINDOW)
    elif not baseline[1] > 0 or not math.isfinite(latest_val / baseline[1]):
        # Unreachable while MIN_PLAUSIBLE_MG_DL holds, and kept so the
        # division stays safe if that clinical bound is ever lowered: a
        # subnormal umol/L value converts to 0.0 mg/dL, and a subnormal
        # mg/dL one gives an infinite ratio.
        criteria["B"] = _not_evaluable(BASELINE_NOT_USABLE)
    else:
        ratio = latest_val / baseline[1]
        ok = _at_least(ratio, RATIO_B)
        criteria["B"] = {"status": "met" if ok else "not-met",
                         "baseline": _point(baseline),
                         "ratio": round(ratio, 4),
                         "elapsed_hours": round(
                             (latest_when - baseline[0]).total_seconds() / 3600, 2)}
        if ok:
            met.append("B")
            stages.append(_stage_for_ratio(ratio))

    stage = max(stages) if stages else None
    if stage is not None and _at_least(latest_val, STAGE_3_ABSOLUTE_MG_DL):
        stage = 3  # KDIGO stage 3 includes a rise to >= 4.0 mg/dL
    return {"check": "kdigo-aki-creatinine", "analyte": _ANALYTE,
            "loinc": CREATININE_LOINC, "source": "kdigo-2012",
            "status": "evaluated", "abstained_reason": None,
            "kdigo_criterion": met, "stage": stage, "criteria": criteria,
            "latest": _point(latest),
            "skipped": skipped, "note": _BASELINE_NOTE}


def _num(value, unit):
    # mg/dL is reported to two places, umol/L to whole numbers or one place.
    places = 2 if unit == "mg/dL" else 1
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _quoted(point, unit):
    """`point`'s value in `unit`: the lab's own number when it was reported
    in that unit, else converted."""
    if point["original_unit"] == unit:
        return _num(point["value"], unit)
    mg_dl = point["value"] / UMOL_PER_MG_DL \
        if point["original_unit"] == "umol/L" else point["value"]
    return _num(mg_dl if unit == "mg/dL" else mg_dl * UMOL_PER_MG_DL, unit)


def _span(hours):
    if hours < 1:
        return "within an hour"
    if hours < 48:
        n = round(hours)
        return f"in {n} hour" + ("" if n == 1 else "s")
    return f"in {round(hours / 24)} days"


def _day(d, with_year):
    return f"{d:%b} {d.day}" + (f", {d.year}" if with_year else "")


def _when_said(start, end):
    if start.date() == end.date():
        return f"On {_day(end, True)}"
    return (f"Between {_day(start, start.year != end.year)} and "
            f"{_day(end, True)}")


def kdigo_consumer_line(result, now=None):
    """The plain-language sentence for a fired check, or None.

    Says when, what the numbers did and what to do — never a diagnosis.
    Numbers are quoted in the latest result's own unit, so a lab that
    reports umol/L is quoted in umol/L. Criterion B's baseline is quoted
    when B fired, since it is the lower of the two comparisons; otherwise
    criterion A's. A result more than 30 days before `now` keeps its flag
    but is worded as a question about follow-up, not an instruction to act.
    """
    if not result or not result.get("kdigo_criterion"):
        return None
    crit = result["criteria"]["B" if "B" in result["kdigo_criterion"] else "A"]
    before = crit.get("baseline") or crit.get("prior")
    latest = result["latest"]
    unit = latest["original_unit"]
    start = datetime.fromisoformat(before["effective"])
    end = datetime.fromisoformat(latest["effective"])
    rise = (f"{result['analyte'].lower()} rose from {_quoted(before, unit)} "
            f"to {_quoted(latest, unit)} "
            f"{'mg/dL' if unit == 'mg/dL' else chr(0xB5) + 'mol/L'} "
            f"{_span(crit['elapsed_hours'])}.")
    now = now or datetime.now(timezone.utc)
    if now - end > STALE_AFTER:
        message = (f"In {end:%b} {end.year}, your {rise} If you haven't "
                   f"already, ask your clinician whether this was followed up.")
    else:
        message = (f"{_when_said(start, end)}, your {rise} A rise like this "
                   f"can mean the kidneys are under strain. Contact your "
                   f"clinician promptly.")
    return {"analyte": result["analyte"], "check": result["check"],
            "message": message}
