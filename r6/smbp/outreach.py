"""SMBP outreach payload builders — channel wiring for the action layer.

These produce the payloads consumed by r6/actions (propose -> human-confirm ->
commit). They do not place calls or send SMS themselves. Demo 1 = SMS/Telegram
reminder; Demo 2 = Bland voice script (guided reading, read-back, keypad
fallback, symptom screen). Administrative only — no clinical advice.
"""

from r6.smbp.content import msg, SYMPTOM_PROMPTS


def reminder_action(patient_ref, to, lang, completed, prescribed):
    """Build an `sms` action payload for a bilingual reading reminder."""
    body = msg("reading_prompt", lang)
    return {
        "kind": "sms",
        "payload": {"to": to, "body": body,
                    "meta": {"patient_ref": patient_ref,
                             "completed": completed, "prescribed": prescribed}},
    }


def voice_reading_script(lang="en"):
    """Build the Demo 2 Bland voice script: guided reading + read-back + screen."""
    steps = [
        msg("teach_sit", lang),
        msg("teach_arm", lang),
        msg("teach_rest", lang),
        # Pass literal {placeholders} through .format() so the read-back line keeps
        # them for the Bland voice agent to fill with the heard values at call time.
        msg("reading_readback", lang, systolic="{systolic}",
            diastolic="{diastolic}", pulse="{pulse}"),
    ]
    return {
        "lang": lang,
        "steps": steps,
        "keypad_fallback": True,
        "symptom_screen": list(SYMPTOM_PROMPTS.get(lang, SYMPTOM_PROMPTS["en"]).values()),
    }
