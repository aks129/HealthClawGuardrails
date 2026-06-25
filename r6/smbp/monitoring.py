"""SMBP monitoring math — pure. BP Observation builder + averages + adherence.

Readings are FHIR BP-panel Observations (LOINC 85354-9) with systolic (8480-6)
and diastolic (8462-4) components in mm[Hg]. AM/PM is derived from the
effectiveDateTime hour (< 12:00 local-naive => AM, else PM).
"""

UCUM_MMHG = {"unit": "mm[Hg]", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}


def build_bp_observation(patient_ref, systolic, diastolic, effective):
    """Return a FHIR BP-panel Observation dict (no id; the store assigns one)."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9",
                             "display": "Blood pressure panel"}]},
        "subject": {"reference": patient_ref},
        "effectiveDateTime": effective,
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6",
                                  "display": "Systolic blood pressure"}]},
             "valueQuantity": {"value": systolic, **UCUM_MMHG}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4",
                                  "display": "Diastolic blood pressure"}]},
             "valueQuantity": {"value": diastolic, **UCUM_MMHG}},
        ],
    }


def _components(obs):
    """Return (systolic, diastolic) from a BP-panel Observation, or (None, None)."""
    sys_v = dia_v = None
    for c in obs.get("component", []):
        code = c.get("code", {}).get("coding", [{}])[0].get("code")
        val = c.get("valueQuantity", {}).get("value")
        if code == "8480-6":
            sys_v = val
        elif code == "8462-4":
            dia_v = val
    return sys_v, dia_v


def slot_of(effective):
    """AM/PM from an ISO effectiveDateTime (hour < 12 => AM).

    Reads the literal hour field of the timestamp; any timezone offset is
    intentionally ignored — the patient's wall-clock hour defines the slot
    (a "morning reading" is morning where the patient is, not in UTC).
    """
    try:
        hour = int(effective[11:13])
    except (ValueError, IndexError):
        return "AM"
    return "AM" if hour < 12 else "PM"


def _avg(pairs):
    if not pairs:
        return None
    sys_vals = [s for s, _ in pairs]
    dia_vals = [d for _, d in pairs]
    return {"systolic": round(sum(sys_vals) / len(sys_vals)),
            "diastolic": round(sum(dia_vals) / len(dia_vals))}


def averages(observations):
    """Compute AM, PM, and overall systolic/diastolic averages + valid_days."""
    am, pm, allp = [], [], []
    days = set()
    for obs in observations:
        s, d = _components(obs)
        if s is None or d is None:
            continue
        eff = obs.get("effectiveDateTime", "")
        allp.append((s, d))
        days.add(eff[:10])
        (am if slot_of(eff) == "AM" else pm).append((s, d))
    return {"am": _avg(am), "pm": _avg(pm), "overall": _avg(allp),
            "valid_days": len(days)}


def adherence(days, observations):
    """Completed readings vs prescribed (2/day over the window)."""
    prescribed = days * 2
    completed = len(observations)
    rate = round(completed / prescribed, 2) if prescribed else 0.0
    return {"completed": completed, "prescribed": prescribed, "rate": rate}
