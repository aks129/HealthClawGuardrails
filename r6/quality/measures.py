"""NQF 0018 / CMS165 — Controlling High Blood Pressure — pure measure engine.

This is a MEASURE CALCULATOR (numerator / denominator / exclusions computed in
Python), NOT a CQL execution engine. It emits standards-shaped results that the
report layer turns into a FHIR MeasureReport.

Measure definition (core logic):
  - Initial population: patients 18–85 at the start of the measurement period.
  - Denominator: initial population WITH an active essential-hypertension
    diagnosis.
  - Denominator exclusions (v1, PARTIAL): pregnancy, ESRD/dialysis. The full
    CMS165 exclusion set (hospice, palliative/advanced-illness+frailty for 66+,
    kidney transplant, etc.) is a documented v1 gap — do not represent this as
    the complete certified eCQM.
  - Numerator: most recent BP during the period is CONTROLLED, i.e.
    systolic < 140 AND diastolic < 90.

Threshold note (clinicians check this): the measure CONTROL target is 140/90.
That is deliberately different from the 130/80 home *diagnostic* threshold used
by r6/smbp/triage.py — a patient can be hypertensive (>130/80) yet controlled
for the measure (<140/90). Both are correct for their purpose.
"""

# Control target for the numerator (CMS165 office control threshold).
CONTROL_SYSTOLIC = 140
CONTROL_DIASTOLIC = 90

# Representative essential-hypertension value set (NOT the full VSAC expansion).
HYPERTENSION_ICD10_PREFIXES = ("I10",)  # essential (primary) hypertension
HYPERTENSION_SNOMED = {
    "38341003",   # Hypertensive disorder, systemic arterial
    "59621000",   # Essential hypertension
    "1201005",    # Benign essential hypertension
}

# Representative exclusion value sets (v1, partial).
PREGNANCY_SNOMED = {"77386006", "72892002"}          # pregnancy
ESRD_ICD10_PREFIXES = ("N18.6",)                     # end-stage renal disease
ESRD_SNOMED = {"46177005", "236434000"}              # ESRD / dependence on dialysis

_ACTIVE_STATUSES = {"active", "recurrence", "relapse"}


def _codings(resource):
    return (resource.get("code", {}) or {}).get("coding", []) or []


def _clinical_active(condition):
    for c in (condition.get("clinicalStatus", {}) or {}).get("coding", []):
        if c.get("code") in _ACTIVE_STATUSES:
            return True
    return False


def _matches(condition, icd10_prefixes=(), snomed=frozenset()):
    for coding in _codings(condition):
        system = (coding.get("system") or "").lower()
        code = coding.get("code") or ""
        if "icd-10" in system or "icd10" in system:
            if any(code.startswith(p) for p in icd10_prefixes):
                return True
        if "snomed" in system and code in snomed:
            return True
    return False


def _has_hypertension(conditions):
    return any(
        _clinical_active(c) and _matches(
            c, HYPERTENSION_ICD10_PREFIXES, HYPERTENSION_SNOMED)
        for c in conditions
    )


def _has_exclusion(conditions):
    for c in conditions:
        if not _clinical_active(c):
            continue
        if _matches(c, (), PREGNANCY_SNOMED):
            return True
        if _matches(c, ESRD_ICD10_PREFIXES, ESRD_SNOMED):
            return True
    return False


def _age_at(birth_date, on_date):
    """Whole years from birth_date to on_date (ISO YYYY-MM-DD)."""
    try:
        by, bm, bd = int(birth_date[0:4]), int(birth_date[5:7]), int(birth_date[8:10])
        oy, om, od = int(on_date[0:4]), int(on_date[5:7]), int(on_date[8:10])
    except (ValueError, IndexError, TypeError):
        return None
    age = oy - by
    if (om, od) < (bm, bd):
        age -= 1
    return age


def _bp_components(obs):
    sys_v = dia_v = None
    for c in obs.get("component", []):
        code = (c.get("code", {}).get("coding", [{}]) or [{}])[0].get("code")
        val = c.get("valueQuantity", {}).get("value")
        if code == "8480-6":
            sys_v = val
        elif code == "8462-4":
            dia_v = val
    return sys_v, dia_v


def _most_recent_bp_in_period(observations, period_start, period_end):
    """Return (systolic, diastolic) of the latest BP-panel Observation whose
    effectiveDateTime date falls within [period_start, period_end]."""
    best = None
    best_when = ''
    for obs in observations:
        when = (obs.get("effectiveDateTime") or "")[:10]
        if not when or when < period_start or when > period_end:
            continue
        s, d = _bp_components(obs)
        if s is None or d is None:
            continue
        if when >= best_when:
            best_when = when
            best = (s, d)
    return best


def evaluate_nqf0018(patient, conditions, bp_observations,
                     period_start, period_end,
                     control_systolic=CONTROL_SYSTOLIC,
                     control_diastolic=CONTROL_DIASTOLIC):
    """Evaluate NQF 0018 for a single patient. Returns a result dict.

    period_start / period_end are ISO dates ('YYYY-MM-DD'). Age is taken at the
    start of the measurement period.
    """
    # Accept full ISO dates or a bare year ('2026' -> Jan 1 / Dec 31).
    ps, pe = str(period_start), str(period_end)
    period_start = ps if len(ps) >= 10 else f"{ps[:4]}-01-01"
    period_end = pe if len(pe) >= 10 else f"{pe[:4]}-12-31"

    age = _age_at(patient.get("birthDate", ""), period_start)
    in_ip = age is not None and 18 <= age <= 85

    has_htn = _has_hypertension(conditions)
    excluded = _has_exclusion(conditions)

    in_denominator = bool(in_ip and has_htn and not excluded)

    recent = _most_recent_bp_in_period(bp_observations, period_start, period_end)
    in_numerator = bool(
        in_denominator and recent is not None
        and recent[0] < control_systolic and recent[1] < control_diastolic
    )

    return {
        "age": age,
        "in_initial_population": in_ip,
        "has_hypertension": has_htn,
        "denominator_exclusion": bool(in_ip and has_htn and excluded),
        "in_denominator": in_denominator,
        "most_recent_bp": ({"systolic": recent[0], "diastolic": recent[1]}
                           if recent else None),
        "in_numerator": in_numerator,
        "control_threshold": {"systolic": control_systolic,
                              "diastolic": control_diastolic},
    }


def evaluate_population(patients_bundle, period_start, period_end):
    """Evaluate a cohort. `patients_bundle` is a list of dicts:
    {patient, conditions, observations}. Returns population counts + rate."""
    denom = numer = excl = 0
    per_patient = []
    for entry in patients_bundle:
        r = evaluate_nqf0018(entry["patient"], entry.get("conditions", []),
                             entry.get("observations", []),
                             period_start, period_end)
        per_patient.append(r)
        if r["denominator_exclusion"]:
            excl += 1
        if r["in_denominator"]:
            denom += 1
            if r["in_numerator"]:
                numer += 1
    rate = round(numer / denom, 4) if denom else None
    return {
        "denominator": denom,
        "numerator": numer,
        "exclusions": excl,
        "performance_rate": rate,
        "per_patient": per_patient,
    }
