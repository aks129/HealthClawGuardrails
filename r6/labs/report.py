# r6/labs/report.py
"""Report builders for lab interpretation — pure (no Flask/DB).

annotate_observation() returns a COPY of the Observation with an HL7 v3
ObservationInterpretation code (and, for table-sourced ranges, a stamped
referenceRange). build_interpretation_summary() is the clinician view;
build_consumer_summary() is the plain-language, outcomes-oriented consumer view.
Neither summary may be placed in audit detail (PHI).
"""
import copy

from r6.labs.interpret import (NO_NUMERIC_VALUE, RANGE_NOT_ASSERTED,
                               UNIT_MISMATCH, UNKNOWN_ANALYTE)
from r6.terminology import LOINC, lookup

V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

_DISPLAY = {"N": "Normal", "L": "Low", "H": "High",
            "LL": "Critically low", "HH": "Critically high"}


def annotate_observation(obs, result):
    out = copy.deepcopy(obs)
    flag = result.get("flag")
    if flag:
        out["interpretation"] = [{"coding": [{
            "system": V3_INTERPRETATION, "code": flag,
            "display": _DISPLAY.get(flag, flag)}]}]
    if result.get("range_source") == "table":
        rng = {"text": "HealthClaw population default (adult); "
                       "not the performing lab's range"}
        if result.get("low") is not None:
            rng["low"] = {"value": result["low"], "unit": result.get("unit")}
        if result.get("high") is not None:
            rng["high"] = {"value": result["high"], "unit": result.get("unit")}
        out.setdefault("referenceRange", []).insert(0, rng)
    return out


def build_interpretation_summary(results):
    buckets = {"normal": 0, "low": 0, "high": 0, "critical": 0, "indeterminate": 0}
    flagged = []
    for r in results:
        flag = r.get("flag")
        if flag is None:
            buckets["indeterminate"] += 1
            continue
        if r.get("critical"):
            buckets["critical"] += 1
        elif flag == "N":
            buckets["normal"] += 1
        elif flag == "L":
            buckets["low"] += 1
        elif flag == "H":
            buckets["high"] += 1
        if flag != "N":
            flagged.append({"analyte": r.get("analyte"), "value": r.get("value"),
                            "unit": r.get("unit"), "flag": flag})
    return {**buckets, "flagged": flagged, "total": len(results),
            "indeterminate_analytes": _undecided_names(results)}


def _undecided_name(r):
    """What to call an analyte that was not scored.

    `interpret_observation` sets `analyte` from the reference-range table, so
    a result that is undecided *because* the code is not in that table has no
    name from it. The label comes from r6/terminology.py keyed by the code —
    never from the Observation's own `display` or `code.text`, which is where
    real feeds put a patient's name. Failing that, the code itself: a LOINC is
    not PHI and is more use to a reader than "an analyte".
    """
    if r.get("analyte"):
        return r["analyte"]
    code = r.get("loinc")
    return lookup(LOINC, code) or (f"LOINC {code}" if code
                                   else "an unidentified analyte")


def _undecided_names(results):
    return [_undecided_name(r) for r in results if r.get("flag") is None]


#: One sentence per cause, each saying what was not decided and why, and
#: none of them saying anything about the person. Wording follows
#: r6/caregaps/report.py: a limit on the check, never a finding.
_CAUSE_NOTES = {
    UNKNOWN_ANALYTE: "there is no reference range on file for it",
    NO_NUMERIC_VALUE: "the result carried no number to compare",
    UNIT_MISMATCH: "the units did not match the reference range",
    RANGE_NOT_ASSERTED: "the performing lab asserted only one bound, and the "
                        "value falls on the side it does not cover",
}


def _unevaluated_marker(results):
    """What was not evaluated and why, or None when every result was decided.

    An unscored analyte used to leave no trace here: it never entered
    `flagged`, and `build_consumer_summary` skipped it for having no flag. A
    panel where nothing could be scored therefore rendered exactly like a
    panel where everything came back normal, which is how a patient whose
    only readings were stage 2 blood pressures got a summary reading "0 high,
    0 critical" (#689).

    The marker rides on ANY undecided result, not only on a wholly undecided
    panel. Care gaps paid for that granularity already (#417): sex-gated
    screenings dropped in silence beside screenings that reported.
    """
    undecided = [r for r in results if r.get("flag") is None]
    if not undecided:
        return None
    names = [_undecided_name(r) for r in undecided]
    causes = {r.get("indeterminate_reason") for r in undecided}
    causes.discard(None)
    if len(causes) == 1:
        reason = causes.pop()
        why = _CAUSE_NOTES.get(reason, "it could not be interpreted")
    else:
        reason = "partly-unevaluated"
        why = "they could not be interpreted, for more than one reason"
    listed = ", ".join(names)
    return {"unevaluated": reason,
            "unevaluated_count": len(undecided),
            "unevaluated_analytes": names,
            "unevaluated_note": (
                f"This check did not evaluate {listed}, because {why}. That is "
                f"a limit on the check and not a statement about your health — "
                f"ask your clinician to read {'them' if len(names) > 1 else 'it'}"
                f" directly.")}


def _consumer_line(r):
    analyte, flag = r.get("analyte"), r.get("flag")
    if flag == "N":
        return {"analyte": analyte, "flag": flag,
                "message": f"Your {analyte.lower()} is within the typical range."}
    if r.get("critical"):
        direction = "well above" if flag == "HH" else "well below"
        return {"analyte": analyte, "flag": flag,
                "message": f"Your {analyte.lower()} is {direction} the typical "
                           f"range — contact your clinician promptly to review it."}
    direction = "above" if flag == "H" else "below"
    return {"analyte": analyte, "flag": flag,
            "message": f"Your {analyte.lower()} is {direction} the typical range — "
                       f"worth discussing with your clinician."}


def build_consumer_summary(results):
    lines = [_consumer_line(r) for r in results if r.get("flag")]
    out = {"lines": lines,
           "note": "This is general information to help you understand your "
                   "results — not a diagnosis. Your clinician interprets what "
                   "these numbers mean for you."}
    marker = _unevaluated_marker(results)
    if marker:
        out.update(marker)
    return out
