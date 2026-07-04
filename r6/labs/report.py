# r6/labs/report.py
"""Report builders for lab interpretation — pure (no Flask/DB).

annotate_observation() returns a COPY of the Observation with an HL7 v3
ObservationInterpretation code (and, for table-sourced ranges, a stamped
referenceRange). build_interpretation_summary() is the clinician view;
build_consumer_summary() is the plain-language, outcomes-oriented consumer view.
Neither summary may be placed in audit detail (PHI).
"""
import copy

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
    return {**buckets, "flagged": flagged, "total": len(results)}


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
    return {"lines": lines,
            "note": "This is general information to help you understand your "
                    "results — not a diagnosis. Your clinician interprets what "
                    "these numbers mean for you."}
