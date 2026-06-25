"""Bilingual (en/es), <=6th-grade SMBP patient content.

Administrative only — no diagnosis, no medication adjustment. `msg(key, lang)`
returns the string, falling back to English for unknown languages. Strings with
{placeholders} are .format()-ed from kwargs.

Extend by adding keys to CATALOG for both 'en' and 'es'. Keep both languages in
sync and at or below a 6th-grade reading level.
"""

CATALOG = {
    "reading_prompt": {
        "en": "Good morning! Time to check your blood pressure. Sit, rest 5 minutes, then send your numbers.",
        "es": "¡Buenos días! Es hora de medir su presión. Siéntese, descanse 5 minutos, y mande sus números.",
    },
    "reading_readback": {
        "en": "I see {systolic}/{diastolic}, pulse {pulse}. Is that right? Reply 1 = Yes, 2 = No.",
        "es": "Veo {systolic}/{diastolic}, pulso {pulse}. ¿Es correcto? Responda 1 = Sí, 2 = No.",
    },
    "reading_saved": {
        "en": "Saved. You have done {completed} of {prescribed} readings. Great work!",
        "es": "Guardado. Lleva {completed} de {prescribed} mediciones. ¡Va muy bien!",
    },
    "teach_sit": {
        "en": "Sit with your back supported and feet flat on the floor.",
        "es": "Siéntese con la espalda apoyada y los pies planos en el piso.",
    },
    "teach_arm": {
        "en": "Rest your arm on a table so the cuff is at the level of your heart.",
        "es": "Apoye el brazo en una mesa para que el brazalete quede a la altura del corazón.",
    },
    "teach_rest": {
        "en": "Rest quietly for 5 minutes. Do not talk during the reading.",
        "es": "Descanse en silencio por 5 minutos. No hable durante la medición.",
    },
    "med_lisinopril": {
        "en": "Your care plan added lisinopril for blood pressure. Take 1 pill each day. It may cause a dry cough or dizziness when you stand up. Tell your care team — do not stop on your own.",
        "es": "Su plan de cuidado agregó lisinopril para la presión. Tome 1 pastilla cada día. Puede causar tos seca o mareo al pararse. Avise a su equipo de salud — no la deje por su cuenta.",
    },
    "ask_care_team": {
        "en": "That is a good question for your care team. I can help you ask them.",
        "es": "Esa es una buena pregunta para su equipo de salud. Le puedo ayudar a preguntarles.",
    },
    "emergency": {
        "en": "These numbers need a provider right away. Please call 911 or go to the emergency room now.",
        "es": "Estos números necesitan atención médica ahora. Por favor llame al 911 o vaya a emergencias ahora.",
    },
}

SYMPTOM_PROMPTS = {
    "en": {
        "chest_pain": "Do you have chest pain?",
        "trouble_breathing": "Do you have trouble breathing?",
        "vision_change": "Any change in your vision?",
        "one_sided_weakness": "Any weakness or numbness on one side?",
        "trouble_speaking": "Any trouble speaking?",
        "severe_headache": "Do you have a very bad headache?",
    },
    "es": {
        "chest_pain": "¿Tiene dolor en el pecho?",
        "trouble_breathing": "¿Tiene dificultad para respirar?",
        "vision_change": "¿Algún cambio en su vista?",
        "one_sided_weakness": "¿Debilidad o entumecimiento en un lado?",
        "trouble_speaking": "¿Dificultad para hablar?",
        "severe_headache": "¿Tiene un dolor de cabeza muy fuerte?",
    },
}


def msg(key, lang, **fmt):
    """Return the catalog string for key+lang (English fallback), formatted."""
    entry = CATALOG[key]  # KeyError on unknown key — caller bug, fail loud
    text = entry.get(lang, entry["en"])
    return text.format(**fmt) if fmt else text
