"""Advisor registry — capability specializations layered over the agent loop.

Ported from SmartHealthConnect's patient skills (skills/*/SKILL.md, archived —
see aks129/SmartHealthConnect#12). An advisor is WHAT the agent is good at;
a persona (personas.py) is HOW it talks. The two are orthogonal: picking the
refills advisor never changes the user's chosen voice.

Engine-backed by construction: an advisor only contributes a system-prompt
block. The tool set stays agent.py's guarded six (all HealthClaw HTTP API —
redaction, audit, tenant scoping inherited), so there is no bypass path for
an advisor to take. That is the structural fix for the contract violation
that got SmartHealthConnect archived (39/40 of its tools bypassed the
engine).

Same honesty rule as the connector catalog: an advisor only advertises as
available when every capability its guidance mentions actually exists.
`available: False` entries state their blocker and are refused server-side.
"""

from __future__ import annotations

# Wearable-sourced Observations carry LOINC codes; naming them makes
# search_records queries precise. Table inherited from the SHC skills.
_LOINC_HINTS = (
    "LOINC codes worth knowing for wearable-sourced Observations: "
    "heart rate 8867-4, resting HR 40443-4, HRV(SDNN) 80404-7, SpO2 59408-5, "
    "respiratory rate 9279-1, body temp 8310-5, steps 55423-8, sleep duration "
    "93832-4, VO2max 65757-1, body weight 29463-7, systolic BP 8480-6, "
    "diastolic BP 8462-4, blood glucose 15074-8."
)

ADVISORS: dict[str, dict] = {
    "general": {
        "name": "General care",
        "emoji": "🌿",
        "blurb": "A well-rounded agent across your whole record.",
        "available": True,
        "guidance": "",  # the base system prompt is already the general agent
    },
    "healthy-habits": {
        "name": "Healthy Habits",
        "emoji": "📊",
        "blurb": "Trends across sleep, activity, vitals, and adherence.",
        "available": True,
        "origin": "SmartHealthConnect skill: healthy-habits",
        "guidance": (
            "Specialty: the longitudinal operating picture — sleep, exercise, "
            "vital-sign trends, and medication adherence as one story.\n"
            "- Lead with the big picture (get_health_summary), then drill "
            "into trends with get_labs and search_records.\n"
            "- Focus on trends over weeks and months, never single readings. "
            "Celebrate consistency and improvements explicitly.\n"
            "- Connect domains when the data supports it: better sleep "
            "alongside lower blood pressure is worth saying out loud.\n"
            "- Never suggest reducing medication because vitals improved — "
            "that is the prescriber's decision alone.\n"
            f"- {_LOINC_HINTS}"
        ),
    },
    "care-completion": {
        "name": "Care Completion",
        "emoji": "✅",
        "blurb": "Screenings, follow-ups, and care gaps — what's due and why.",
        "available": True,
        "origin": "SmartHealthConnect skill: care-completion",
        "guidance": (
            "Specialty: preventive care and follow-through — screenings, "
            "referrals, and care gaps against quality measures.\n"
            "- Start from get_care_gaps for the current picture; use "
            "get_health_summary and search_records for the why behind each "
            "gap (age, sex, conditions).\n"
            "- Explain in plain language why each screening matters. "
            "Highlight overdue cancer screenings and immunizations with "
            "appropriate urgency.\n"
            "- Care-gap logic is guideline-based (HEDIS/USPSTF), not "
            "individual medical advice — say so, and route specific "
            "screening decisions to their provider.\n"
            "- Never dismiss a gap as unimportant; that call belongs to the "
            "provider."
        ),
    },
    "medication-refills": {
        "name": "Medication Refills",
        "emoji": "💊",
        "blurb": "What you take, what's running low, what to ask about.",
        "available": True,
        "origin": "SmartHealthConnect skill: medication-refills",
        "guidance": (
            "Specialty: the medication picture — active prescriptions, "
            "adherence signals, and refill planning.\n"
            "- Ground every claim in search_records over MedicationRequest "
            "(plus get_health_summary for the roster).\n"
            "- Help them plan ahead: what looks close to running out, what "
            "to raise with the pharmacy or prescriber, and what needs a "
            "renewal rather than a refill.\n"
            "- You cannot SUBMIT refill requests yet — requesting a refill "
            "is a real-world action that will arrive on the approval rail. "
            "Say so plainly and point them to their pharmacy in the "
            "meantime; never imply a request was placed.\n"
            "- Never recommend stopping or changing a dose. If something "
            "reads as overdue or lapsed, urge them to contact their "
            "pharmacy or provider."
        ),
    },
    "diet-exercise": {
        "name": "Diet & Exercise",
        "emoji": "🏃",
        "blurb": "How activity and habits show up in your numbers.",
        "available": True,
        "origin": "SmartHealthConnect skill: diet-exercise",
        "guidance": (
            "Specialty: how activity and lifestyle show up in the clinical "
            "record — BP, glucose, weight, and wearable signals.\n"
            "- Read activity through wearable-sourced Observations "
            "(search_records) and clinical results (get_labs); present "
            "connections in plain language: 'on weeks with more activity, "
            "your average BP reads lower.'\n"
            "- Correlations need enough data — say when the sample is too "
            "thin to mean anything.\n"
            "- Encourage consistency over intensity; patterns over single "
            "workouts.\n"
            "- Anyone with a cardiac condition gets a see-your-provider note "
            "before exercise-intensity suggestions; symptoms during exercise "
            "(chest pain, dizziness) are an immediate seek-care answer.\n"
            f"- {_LOINC_HINTS}"
        ),
    },
    "research-monitor": {
        "name": "Research Monitor",
        "emoji": "🔬",
        "blurb": "New research and trials relevant to your conditions.",
        "available": False,
        "origin": "SmartHealthConnect skill: research-monitor",
        "note": (
            "Needs research-source tools (preprints, ClinicalTrials.gov, "
            "FDA safety signals) that the agent loop does not have yet."
        ),
    },
    "kids-health": {
        "name": "Kids & Family",
        "emoji": "👶",
        "blurb": "Pediatric schedules, school forms, family records.",
        "available": False,
        "origin": "SmartHealthConnect skill: kids-health",
        "note": (
            "Needs caregiver accounts — an adult acting on a dependent's "
            "record with the audit trail recording who acted. Blocked on "
            "the identity work (aks129/HealthClawGuardrails#157)."
        ),
    },
}

DEFAULT_ADVISOR = "general"


def catalog() -> list[dict]:
    """Advisor tiles for the UI — available ones plus honest 'soon' entries."""
    out = []
    for key, a in ADVISORS.items():
        item = {"id": key, "name": a["name"], "emoji": a["emoji"],
                "blurb": a["blurb"], "available": a["available"]}
        if not a["available"]:
            item["note"] = a.get("note", "coming soon")
        out.append(item)
    return out


def get(key: str) -> dict | None:
    return ADVISORS.get(key)


def prompt_block(key: str | None) -> str:
    """The system-prompt addition for an advisor; empty for general/unknown."""
    a = ADVISORS.get(key or DEFAULT_ADVISOR)
    if not a or not a["available"] or not a.get("guidance"):
        return ""
    return f"\n\n{a['guidance']}"
