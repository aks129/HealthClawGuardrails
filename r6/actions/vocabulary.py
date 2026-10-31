"""
Fixed phrases and code lists the action rail shares with optional modules.

They live here, in the core, so the rail does not import the modules that
also use them (r6/extensions.py). Those modules import from here instead.
"""

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

#: A medicine on the list whose name we cannot show (#884 G7).
MEDICINE_UNNAMED = ("A medicine is listed, but we can't show its name. "
                    "Ask your doctor.")
