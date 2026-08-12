"""Synthetic blood-pressure history for the demo tenant.

Three composite patients, each built to exercise a different capability of
the SMBP rail. Nothing here describes a real person: the personas, the
readings and the clinic are invented, and the file is here so a demo,
a screenshot or an acceptance test has data with a shape worth looking at.

THE SHAPE, AND WHY IT IS NOT A FLAT SERIES

An earlier version gave all three patients an identical monthly series.
That is the wrong shape for this domain, and wrong in a way that shows:

    Office readings go back years and are sparse. Home readings are recent
    and dense.

Which draws a slow drift nobody caught at ordinary visits, then a dense
recent cluster where somebody finally measured properly. The three-year tail
earns its place because it shows the miss. A uniform series shows nothing.

  marisol  stage 2 confirmed at home, treated, responds imperfectly
  elena    high in clinic, at goal at home — watched, not medicated
  ray      one elevated reading, no home stream at all

The third one is a deliberate absence. A patient on a landline with no
smartphone has no home series, and that is exactly the case the rail exists
to serve; filling him out with invented monitoring would destroy it.

HOW A HOME READING AND AN OFFICE READING TELL THEMSELVES APART

The white-coat case depends on the record distinguishing them, so it does it
the way a record actually would, with no invented codes:

    office  Observation.encounter -> the visit, performer -> the practice
    home    no encounter,          performer -> the patient (self-measured)

One sentence: the record says who took the measurement and whether it
happened at a visit.

CODES ARE VERIFIED, NOT REMEMBERED

Every RxNorm code here was looked up in RxNav and every LOINC code in the
HL7 terminology server. Not ceremony: the losartan/HCTZ combination is
979468, and the value this file would have carried from memory was 979485 —
a real code for a different product. It would have looked right in review.

COUNTS ARE DERIVED, NOT LABELLED

Adherence percentages must fall out of the data. Seeding 28 readings and
labelling them "86%" produces a number that survives the data changing
underneath it, so the missed days are genuinely missing instead: two full
days for marisol, one for elena. tests/test_smbp_demo_history.py asserts
both the counts and the averages that follow from them.
"""

from datetime import date, timedelta

# --- verified codes ---------------------------------------------------------
LOINC = {
    "bp_panel": ("85354-9", "Blood pressure panel"),
    "systolic": ("8480-6", "Systolic blood pressure"),
    "diastolic": ("8462-4", "Diastolic blood pressure"),
    "heart_rate": ("8867-4", "Heart rate"),
    "creatinine": ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma"),
    "potassium": ("2823-3", "Potassium [Moles/volume] in Serum or Plasma"),
    "sodium": ("2951-2", "Sodium [Moles/volume] in Serum or Plasma"),
    "egfr": ("33914-3",
             "Glomerular filtration rate [Volume Rate/Area] in Serum or Plasma"),
    "a1c": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "lipid_panel": ("57698-3", "Lipid panel with direct LDL - Serum or Plasma"),
    "uacr": ("9318-7", "Albumin/Creatinine [Mass Ratio] in Urine"),
    "weight": ("29463-7", "Body weight"),
    "height": ("8302-2", "Body height"),
    "bmi": ("39156-5", "Body mass index (BMI) [Ratio]"),
    "smoking": ("72166-2", "Tobacco smoking status"),
}

RXNORM = {
    "losartan_hctz": ("979468",
                      "losartan potassium 50 MG / hydrochlorothiazide "
                      "12.5 MG Oral Tablet"),
    "metformin": ("860975", "metformin hydrochloride 500 MG Oral Tablet"),
    "amlodipine": ("197361", "amlodipine 5 MG Oral Tablet"),
}

ICD10 = {
    "essential_htn": ("I10", "Essential (primary) hypertension"),
    "t2dm": ("E11.9", "Type 2 diabetes mellitus without complications"),
    "elevated_bp": ("R03.0", "Elevated blood-pressure reading, without "
                             "diagnosis of hypertension"),
}

PRACTICE_ID = "beluma-demo-practice"

# --- marisol: confirmed at home, treated -----------------------------------
#
# Office tail: high 120s systolic in 2023, low-to-mid 130s through 2024, high
# 130s and low 140s in 2025, 138-144 in early 2026. The two elevated readings
# in the last six months are what prompt home monitoring. The climb is NOT
# linear on purpose — real office readings bounce, and a straight line reads
# as generated to anyone who looks at charts for a living.
MARISOL_OFFICE = [
    ("2023-08-17", 128, 82, 76), ("2023-11-02", 126, 80, 74),
    ("2024-02-15", 133, 84, 78), ("2024-06-06", 130, 82, 72),
    ("2024-10-24", 135, 86, 80), ("2025-01-30", 132, 84, 75),
    ("2025-05-22", 139, 88, 79), ("2025-09-11", 141, 90, 77),
    ("2025-12-04", 138, 87, 74), ("2026-02-19", 144, 92, 81),
    ("2026-05-14", 142, 91, 78),
]

#: Fixed reference values. Printed material and acceptance tests both
#: quote these, so they are literals rather than generated.
MARISOL_CARD = {
    3: ((155, 97), (148, 93)), 6: ((151, 95), (146, 91)),
    9: ((154, 96), (147, 93)), 12: ((150, 94), (145, 91)),
    15: ((153, 95), (148, 92)),
}
#: The seven non-card days, chosen so morning averages 153/96 and evening
#: 147/92 across all twelve — which makes the overall exactly 150/94.
MARISOL_FILL = {
    2: ((153, 96), (147, 92)), 4: ((154, 97), (148, 93)),
    7: ((152, 96), (146, 91)), 8: ((155, 97), (147, 92)),
    10: ((153, 96), (148, 93)), 13: ((152, 96), (147, 92)),
    14: ((154, 97), (147, 91)),
}
#: Two full missed days. Adherence is 24/28 because these are absent.
MARISOL_MISSED = (5, 11)

# --- elena: white coat ------------------------------------------------------
ELENA_OFFICE = [
    ("2024-06-11", 134, 84, 74), ("2024-11-19", 132, 83, 72),
    ("2025-04-08", 136, 85, 76), ("2025-10-14", 133, 84, 71),
    ("2026-01-27", 138, 86, 75), ("2026-05-19", 136, 84, 73),
]
ELENA_CARD = {
    3: ((121, 77), (117, 73)), 6: ((119, 75), (115, 71)),
    9: ((122, 78), (118, 74)), 12: ((118, 74), (114, 70)),
    15: ((120, 76), (116, 72)),
}
ELENA_FILL = {
    2: ((120, 76), (116, 72)), 4: ((121, 77), (117, 73)),
    5: ((119, 75), (115, 71)), 7: ((120, 76), (116, 72)),
    10: ((121, 77), (117, 73)), 11: ((119, 75), (115, 71)),
    13: ((120, 76), (116, 72)), 14: ((120, 76), (116, 72)),
}
ELENA_MISSED = (8,)

# --- ray: thin on purpose ----------------------------------------------
RAY_OFFICE = [
    ("2023-10-05", 148, 92, 74), ("2024-09-19", 152, 94, 76),
    ("2025-11-13", 146, 90, 72),
]
RAY_CURRENT = ("2026-08-11T09:15:00Z", 164, 98, 78)


def _coding(entry):
    code, display = entry
    return {"system": "http://loinc.org", "code": code, "display": display}


def _bp(rid, patient_id, systolic, diastolic, pulse, effective,
        encounter_id=None):
    """A BP panel. `encounter_id` is what makes it an office reading."""
    obs = {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs"}]}],
        "code": {"coding": [_coding(LOINC["bp_panel"])]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": effective,
        "component": [
            {"code": {"coding": [_coding(LOINC["systolic"])]},
             "valueQuantity": {"value": systolic, "unit": "mm[Hg]",
                               "system": "http://unitsofmeasure.org",
                               "code": "mm[Hg]"}},
            {"code": {"coding": [_coding(LOINC["diastolic"])]},
             "valueQuantity": {"value": diastolic, "unit": "mm[Hg]",
                               "system": "http://unitsofmeasure.org",
                               "code": "mm[Hg]"}},
            {"code": {"coding": [_coding(LOINC["heart_rate"])]},
             "valueQuantity": {"value": pulse, "unit": "/min",
                               "system": "http://unitsofmeasure.org",
                               "code": "/min"}},
        ],
    }
    if encounter_id:
        obs["encounter"] = {"reference": f"Encounter/{encounter_id}"}
        obs["performer"] = [{"reference": f"Organization/{PRACTICE_ID}"}]
    else:
        # Self-measured. No encounter, and the patient is the performer.
        obs["performer"] = [{"reference": f"Patient/{patient_id}"}]
    return obs


def _encounter(eid, patient_id, when):
    return {
        "resourceType": "Encounter",
        "id": eid,
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                  "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": f"Patient/{patient_id}"},
        "actualPeriod": {"start": f"{when}T09:00:00Z",
                         "end": f"{when}T09:30:00Z"},
        "serviceProvider": {"reference": f"Organization/{PRACTICE_ID}"},
    }


def _office_series(key, patient_id, rows):
    """Office readings, each with the Encounter that makes it one."""
    out = []
    for i, (when, systolic, diastolic, pulse) in enumerate(rows):
        eid = f"enc-{key}-{i:02d}"
        out.append(_encounter(eid, patient_id, when))
        out.append(_bp(f"bp-{key}-office-{when.replace('-', '')}", patient_id,
                       systolic, diastolic, pulse, f"{when}T09:10:00Z",
                       encounter_id=eid))
    return out


def _home_series(key, patient_id, start, card, fill, missed, pulse_base=72):
    """The dense recent fortnight. Missed days are simply absent.

    Keyed by CALENDAR day, not by an offset from `start`. The first version
    walked 1..14 as offsets while the card dicts were keyed 2..15, so every
    reading landed a day early and the two "missed" days were populated —
    which the card tests caught immediately, being literal values.
    """
    out = []
    for day in range(start.day, start.day + 14):
        if day in missed:
            continue
        values = card.get(day) or fill.get(day)
        if values is None:
            continue
        (am_s, am_d), (pm_s, pm_d) = values
        when = date(start.year, start.month, day)
        stamp = when.strftime("%Y%m%d")
        out.append(_bp(f"bp-{key}-home-{stamp}-am", patient_id, am_s, am_d,
                       pulse_base + (day % 7), f"{when.isoformat()}T07:30:00Z"))
        out.append(_bp(f"bp-{key}-home-{stamp}-pm", patient_id, pm_s, pm_d,
                       pulse_base + 3 + (day % 6),
                       f"{when.isoformat()}T20:15:00Z"))
    return out


def _marisol_on_treatment(patient_id):
    """Jun 17 - Jul 14, twice daily, trending to roughly 134/84.

    Better, and deliberately not at goal. A clean drop to 118/74 reads as
    fiction to a clinician, so the decline is uneven: a plateau in the
    middle week and two days that go back up.
    """
    out = []
    start = date(2026, 6, 17)
    # 28 days, six missed -> 44 of 56, the count the spec names. Four missed
    # days gave 48, which is "roughly 44" only until someone puts the
    # adherence percentage on a card: 44/56 is 79%, 48/56 is 86%.
    missed = {5, 9, 12, 19, 23, 26}
    for i in range(28):
        if i in missed:
            continue
        when = start + timedelta(days=i)
        # 150 -> ~134 with a plateau and two rebounds, not a line.
        drop = min(i, 20) * 0.75
        bounce = 3 if i in (9, 17) else 0
        plateau = 2 if 11 <= i <= 15 else 0
        am_s = int(153 - drop + bounce + plateau)
        am_d = int(96 - drop * 0.55 + (1 if bounce else 0))
        pm_s = am_s - 5
        pm_d = am_d - 3
        stamp = when.strftime("%Y%m%d")
        out.append(_bp(f"bp-marisol-tx-{stamp}-am", patient_id, am_s, am_d,
                       70 + (i % 8), f"{when.isoformat()}T07:30:00Z"))
        out.append(_bp(f"bp-marisol-tx-{stamp}-pm", patient_id, pm_s, pm_d,
                       73 + (i % 7), f"{when.isoformat()}T20:15:00Z"))
    return out


def _patient(pid, family, given, birth, gender, language, phone):
    return {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"use": "official", "family": family, "given": list(given)}],
        "birthDate": birth,
        "gender": gender,
        "communication": [{"language": {"coding": [{
            "system": "urn:ietf:bcp:47", "code": language}]},
            "preferred": True}],
        "telecom": [{"system": "phone", "value": phone, "use": "mobile"}],
    }


def _condition(cid, pid, entry, onset):
    code, display = entry
    return {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": "active"}]},
        "verificationStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "code": "confirmed"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                             "code": code, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "onsetDateTime": onset,
    }


def _medication(mid, pid, entry, start):
    code, display = entry
    return {
        "resourceType": "MedicationRequest",
        "id": mid,
        "status": "active",
        "intent": "order",
        "medication": {"concept": {"coding": [{
            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "code": code, "display": display}]}},
        "subject": {"reference": f"Patient/{pid}"},
        "authoredOn": start,
        "dosageInstruction": [{"text": "1 tablet by mouth every morning"}],
    }


def _lab(rid, pid, entry, value, unit, when):
    code, display = entry
    return {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": code,
                             "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit,
                          "system": "http://unitsofmeasure.org", "code": unit},
    }


def _organization():
    return {"resourceType": "Organization", "id": PRACTICE_ID,
            "name": "Beluma Demo Clinic", "active": True}


def marisol_resources():
    pid = "demo-marisol"
    out = [_patient(pid, "Reyes", ["Marisol"], "1978-04-12", "female", "es",
                    "555-0142")]
    out.append(_condition("cond-marisol-htn", pid, ICD10["essential_htn"],
                          "2026-06-16"))
    out.append(_condition("cond-marisol-dm2", pid, ICD10["t2dm"], "2022-03-09"))
    out.append(_medication("med-marisol-losartan-hctz", pid,
                           RXNORM["losartan_hctz"], "2026-06-16"))
    out.append(_medication("med-marisol-metformin", pid, RXNORM["metformin"],
                           "2022-03-20"))
    out += _office_series("marisol", pid, MARISOL_OFFICE)
    out += _home_series("marisol", pid, date(2026, 6, 2), MARISOL_CARD,
                        MARISOL_FILL, MARISOL_MISSED, pulse_base=70)
    out += _marisol_on_treatment(pid)
    # Two lab sets. Potassium declines on HCTZ, which is the reason
    # follow-up labs exist and the number most worth getting right here.
    for tag, when, k, creat, egfr, a1c in (
            ("base", "2026-06-16T08:00:00Z", 4.2, 0.9, 82, 7.4),
            ("fu", "2026-07-10T08:00:00Z", 3.6, 1.0, 78, 7.1)):
        out.append(_lab(f"lab-marisol-{tag}-k", pid, LOINC["potassium"], k,
                        "mmol/L", when))
        out.append(_lab(f"lab-marisol-{tag}-creat", pid, LOINC["creatinine"],
                        creat, "mg/dL", when))
        out.append(_lab(f"lab-marisol-{tag}-na", pid, LOINC["sodium"], 139,
                        "mmol/L", when))
        out.append(_lab(f"lab-marisol-{tag}-egfr", pid, LOINC["egfr"], egfr,
                        "mL/min/{1.73_m2}", when))
        out.append(_lab(f"lab-marisol-{tag}-a1c", pid, LOINC["a1c"], a1c,
                        "%", when))
    out.append(_lab("lab-marisol-uacr", pid, LOINC["uacr"], 18, "mg/g",
                    "2026-06-16T08:00:00Z"))
    out.append(_lab("vs-marisol-weight", pid, LOINC["weight"], 78.5, "kg",
                    "2026-06-16T08:00:00Z"))
    out.append(_lab("vs-marisol-height", pid, LOINC["height"], 161, "cm",
                    "2026-06-16T08:00:00Z"))
    out.append(_lab("vs-marisol-bmi", pid, LOINC["bmi"], 30.3, "kg/m2",
                    "2026-06-16T08:00:00Z"))
    return out


def elena_resources():
    pid = "demo-elena"
    out = [_patient(pid, "Marchetti", ["Elena"], "1971-02-26", "female", "es",
                    "555-0177")]
    # NOT essential hypertension. The case is that she does not have one.
    out.append(_condition("cond-elena-elevated-bp", pid, ICD10["elevated_bp"],
                          "2026-01-27"))
    out += _office_series("elena", pid, ELENA_OFFICE)
    out += _home_series("elena", pid, date(2026, 6, 2), ELENA_CARD,
                        ELENA_FILL, ELENA_MISSED, pulse_base=68)
    out.append(_lab("lab-elena-k", pid, LOINC["potassium"], 4.1, "mmol/L",
                    "2026-05-19T08:30:00Z"))
    out.append(_lab("lab-elena-creat", pid, LOINC["creatinine"], 0.8, "mg/dL",
                    "2026-05-19T08:30:00Z"))
    out.append(_lab("lab-elena-na", pid, LOINC["sodium"], 140, "mmol/L",
                    "2026-05-19T08:30:00Z"))
    out.append(_lab("vs-elena-weight", pid, LOINC["weight"], 66.0, "kg",
                    "2026-05-19T08:30:00Z"))
    out.append(_lab("vs-elena-height", pid, LOINC["height"], 165, "cm",
                    "2026-05-19T08:30:00Z"))
    return out


def ray_resources():
    pid = "demo-ray"
    out = [_patient(pid, "Whitfield", ["Ray"], "1959-07-08", "male", "en",
                    "555-0106")]
    out.append(_condition("cond-ray-htn", pid, ICD10["essential_htn"],
                          "2019-05-02"))
    # Already treated and still 164/98 — the more interesting case.
    out.append(_medication("med-ray-amlodipine", pid, RXNORM["amlodipine"],
                           "2023-10-05"))
    out += _office_series("ray", pid, RAY_OFFICE)
    when, systolic, diastolic, pulse = RAY_CURRENT
    # The current reading arrives by phone, so it has no encounter and no
    # home series behind it. That absence is the case.
    out.append(_bp("bp-ray-current", pid, systolic, diastolic, pulse, when))
    out.append(_lab("lab-ray-k", pid, LOINC["potassium"], 4.4, "mmol/L",
                    "2024-09-19T09:00:00Z"))
    out.append(_lab("lab-ray-creat", pid, LOINC["creatinine"], 1.1, "mg/dL",
                    "2024-09-19T09:00:00Z"))
    out.append(_lab("vs-ray-weight", pid, LOINC["weight"], 91.0, "kg",
                    "2024-09-19T09:00:00Z"))
    return out


def smbp_history_resources():
    """Every resource for the three demo patients. Pure data, no I/O.

    Fixed ids throughout: r6/seed.py skips ids it already holds, so this is
    what makes re-seeding a no-op instead of a second copy (#457).
    """
    return ([_organization()] + marisol_resources() + elena_resources()
            + ray_resources())
