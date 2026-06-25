"""SMBP triage — the single source of truth for the §2.2 decision logic.

Pure functions. The hypertension-coordinator skill, the Demo 2 voice script,
and the clinician report flags all classify through here so they cannot drift.

Administrative logic only: this classifies a reading into an action band; it
never produces clinical advice text.
"""

# Home BP diagnostic threshold (NOT the office 140/90).
HOME_SYSTOLIC = 135
HOME_DIASTOLIC = 85

# Severe thresholds that gate the emergency pathway.
SEVERE_SYSTOLIC = 180
SEVERE_DIASTOLIC = 120

# The 6-item symptom screen (asked in the patient's language elsewhere).
SYMPTOMS = (
    "chest_pain",
    "trouble_breathing",
    "vision_change",
    "one_sided_weakness",
    "trouble_speaking",
    "severe_headache",
)


def classify(systolic, diastolic, symptoms=None):
    """Classify a reading into a triage band + agent action.

    Returns a dict: {band, action, emergency, rationale, threshold}.
    `symptoms` is an iterable of SYMPTOMS keys the patient endorsed.
    """
    symptoms = [s for s in (symptoms or []) if s in SYMPTOMS]
    severe = systolic >= SEVERE_SYSTOLIC or diastolic >= SEVERE_DIASTOLIC

    if severe and symptoms:
        return _result("emergency", "call_911", True,
                       "Possible hypertensive emergency")
    if severe:
        return _result("urgent", "recheck_5min_then_careteam", False,
                       "Hypertensive urgency")
    if systolic >= 160 or diastolic >= 100:
        return _result("followup", "symptom_screen_then_visit", False,
                       "Needs timely follow-up, not the ED")
    if systolic >= HOME_SYSTOLIC or diastolic >= HOME_DIASTOLIC:
        return _result("elevated", "log_flag", False,
                       "Elevated — the report and the visit handle it")
    return _result("normal", "log_encourage", False,
                   "Within home target range")


def _result(band, action, emergency, rationale):
    return {
        "band": band,
        "action": action,
        "emergency": emergency,
        "rationale": rationale,
        "threshold": {"systolic": HOME_SYSTOLIC, "diastolic": HOME_DIASTOLIC},
    }
