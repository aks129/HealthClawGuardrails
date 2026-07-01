"""SMBP triage — the single source of truth for the §2.2 decision logic.

Pure functions. The hypertension-coordinator skill, the Demo 2 voice script,
and the clinician report flags all classify through here so they cannot drift.

Administrative logic only: this classifies a reading into an action band; it
never produces clinical advice text.

Aligned to the 2025 AHA/ACC High Blood Pressure Guideline (Gigi Magan spec,
updated June 2026). Two INDEPENDENT axes:

  - BP number (how high): at_goal < 130/80, stage1 130-139/80-89 (log+flag),
    stage2 140-179/90-119 (symptom screen -> timely visit), crisis >= 180/120
    (recheck -> same-day care team).
  - Red-flag symptoms (whether an organ is being harmed now): ANY red-flag
    symptom -> emergency / 911, REGARDLESS of the reading. These are also
    stroke and heart-attack signs; the agent is a navigator, not a diagnostic
    device, and must never reassure a symptomatic patient because the number
    looks normal.

Home diagnostic threshold is 130/80 (the 2025 line; 135/85 was the older
threshold tied to the retired 140/90 office cutoff).
"""

# Home BP diagnostic threshold (2025 AHA/ACC; NOT the older 135/85 or 140/90).
HOME_SYSTOLIC = 130
HOME_DIASTOLIC = 80

# Stage-2 thresholds (timely visit, not the ED) — 2025 line is 140/90.
STAGE2_SYSTOLIC = 140
STAGE2_DIASTOLIC = 90

# Crisis thresholds (urgency: recheck then same-day care team).
CRISIS_SYSTOLIC = 180
CRISIS_DIASTOLIC = 120

# The 7-item red-flag symptom screen (asked in the patient's language
# elsewhere). Any endorsed red-flag routes to the emergency pathway regardless
# of the BP number — these are stroke / heart-attack signs.
SYMPTOMS = (
    "chest_pain",
    "shortness_of_breath",
    "one_sided_weakness",
    "trouble_speaking",
    "vision_change",
    "severe_headache",
    "confusion",
)


def classify(systolic, diastolic, symptoms=None):
    """Classify a reading into a triage band + agent action.

    Returns a dict: {band, action, emergency, rationale, threshold}.
    `symptoms` is an iterable of SYMPTOMS keys the patient endorsed.

    Symptoms are evaluated FIRST and independently of the number: any endorsed
    red-flag symptom is an emergency regardless of the BP reading.
    """
    symptoms = [s for s in (symptoms or []) if s in SYMPTOMS]

    # Symptom axis (independent): any red-flag symptom -> 911, any reading.
    if symptoms:
        return _result("emergency", "call_911", True,
                       "Red-flag symptom — possible acute event; 911 regardless of BP")

    # BP axis (no symptoms).
    if systolic >= CRISIS_SYSTOLIC or diastolic >= CRISIS_DIASTOLIC:
        return _result("crisis", "recheck_5min_then_careteam", False,
                       "Hypertensive urgency — recheck, then same-day care team")
    if systolic >= STAGE2_SYSTOLIC or diastolic >= STAGE2_DIASTOLIC:
        return _result("stage2", "symptom_screen_then_visit", False,
                       "Stage 2 — timely follow-up, not the ED")
    if systolic >= HOME_SYSTOLIC or diastolic >= HOME_DIASTOLIC:
        return _result("stage1", "log_flag", False,
                       "Stage 1 — the report and the visit handle it")
    return _result("at_goal", "log_encourage", False,
                   "At goal — within home target range")


def _result(band, action, emergency, rationale):
    return {
        "band": band,
        "action": action,
        "emergency": emergency,
        "rationale": rationale,
        "threshold": {"systolic": HOME_SYSTOLIC, "diastolic": HOME_DIASTOLIC},
    }
