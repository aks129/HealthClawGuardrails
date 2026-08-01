"""Server-derived labels for standard clinical codes.

Why this exists
---------------
Redaction strips every `display` and `CodeableConcept.text`, and it is right to:
upstream systems really do write patient names into those fields. A LOINC coding
in our own test fixtures carries "Glucose for Jane Secret" — that is not a
hypothetical, it is what real feeds look like (#207, #209).

The consequence was that a record reached the agent with no readable name at
all. Measured against a live tenant on 2026-08-01: **0 of 65 records carried any
label after redaction.** The agent could only say "unlabeled record, code
250.00", and in the worst case reported a diagnosis the person actually had as
absent.

The fix is to put the label back from a source we control. A code's meaning is
a property of the code, not of the patient: "E11.9 means type 2 diabetes
mellitus" is true for everyone who has ever been assigned E11.9. Looking the
label up here — rather than trusting the string the upstream sent — restores
readability without reopening the leak, because nothing patient-specific can
enter through a table keyed by code.

Deliberately a plain dict
-------------------------
Not a terminology service, not a network call, not a cache. A dict lookup cannot
time out, cannot fail in production, and can be read and corrected by anyone.
Unknown codes are LEFT UNLABELLED on purpose: the honest "a record is here that
I could not read" fallback in the agent is far better than a confident guess,
and `unlabelled_codes()` reports what is missing so the map can grow from
evidence rather than speculation.

Coverage is primary-care-weighted (the records consumers actually connect):
common chemistry and hematology panels, vitals, the widespread chronic
conditions, and high-frequency maintenance medications.
"""

from __future__ import annotations

import collections

# System URIs, canonical form.
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ICD9 = "http://hl7.org/fhir/sid/icd-9-cm"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SNOMED = "http://snomed.info/sct"
CVX = "http://hl7.org/fhir/sid/cvx"

# Aliases seen in the wild for the same system.
_SYSTEM_ALIASES = {
    "http://loinc.org": LOINC,
    "https://loinc.org": LOINC,
    "urn:oid:2.16.840.1.113883.6.1": LOINC,
    "http://www.nlm.nih.gov/research/umls/rxnorm": RXNORM,
    "urn:oid:2.16.840.1.113883.6.88": RXNORM,
    "http://hl7.org/fhir/sid/icd-9-cm": ICD9,
    "urn:oid:2.16.840.1.113883.6.103": ICD9,
    "http://hl7.org/fhir/sid/icd-10-cm": ICD10,
    "http://hl7.org/fhir/sid/icd-10": ICD10,
    "urn:oid:2.16.840.1.113883.6.90": ICD10,
    "http://snomed.info/sct": SNOMED,
    "urn:oid:2.16.840.1.113883.6.96": SNOMED,
    "http://hl7.org/fhir/sid/cvx": CVX,
    "urn:oid:2.16.840.1.113883.12.292": CVX,
}

_LABELS: dict[tuple[str, str], str] = {
    # --- LOINC: chemistry ---------------------------------------------------
    (LOINC, "2339-0"): "Glucose (blood)",
    (LOINC, "2345-7"): "Glucose (serum or plasma)",
    (LOINC, "4548-4"): "Hemoglobin A1c",
    (LOINC, "17856-6"): "Hemoglobin A1c",
    (LOINC, "2823-3"): "Potassium",
    (LOINC, "2951-2"): "Sodium",
    (LOINC, "2075-0"): "Chloride",
    (LOINC, "2028-9"): "Carbon dioxide (CO2)",
    (LOINC, "3094-0"): "Blood urea nitrogen (BUN)",
    (LOINC, "2160-0"): "Creatinine",
    (LOINC, "33914-3"): "Estimated GFR",
    (LOINC, "17861-6"): "Calcium",
    (LOINC, "1751-7"): "Albumin",
    (LOINC, "1975-2"): "Bilirubin (total)",
    (LOINC, "1920-8"): "AST",
    (LOINC, "1742-6"): "ALT",
    (LOINC, "6768-6"): "Alkaline phosphatase",
    (LOINC, "2885-2"): "Protein (total)",
    (LOINC, "3016-3"): "TSH",
    (LOINC, "2571-8"): "Triglycerides",
    (LOINC, "2093-3"): "Cholesterol (total)",
    (LOINC, "2085-9"): "HDL cholesterol",
    (LOINC, "13457-7"): "LDL cholesterol (calculated)",
    (LOINC, "2089-1"): "LDL cholesterol",
    (LOINC, "14646-4"): "Vitamin D (25-hydroxy)",
    (LOINC, "2132-9"): "Vitamin B12",
    (LOINC, "2498-4"): "Iron",
    (LOINC, "2276-4"): "Ferritin",

    # --- LOINC: hematology --------------------------------------------------
    (LOINC, "718-7"): "Hemoglobin",
    (LOINC, "4544-3"): "Hematocrit",
    (LOINC, "6690-2"): "White blood cell count",
    (LOINC, "789-8"): "Red blood cell count",
    (LOINC, "777-3"): "Platelet count",
    (LOINC, "787-2"): "Mean corpuscular volume (MCV)",
    (LOINC, "785-6"): "Mean corpuscular hemoglobin (MCH)",
    (LOINC, "788-0"): "Red cell distribution width (RDW)",

    # --- LOINC: vitals ------------------------------------------------------
    (LOINC, "55284-4"): "Blood pressure",
    (LOINC, "85354-9"): "Blood pressure panel",
    (LOINC, "8480-6"): "Systolic blood pressure",
    (LOINC, "8462-4"): "Diastolic blood pressure",
    (LOINC, "8867-4"): "Heart rate",
    (LOINC, "9279-1"): "Respiratory rate",
    (LOINC, "8310-5"): "Body temperature",
    (LOINC, "29463-7"): "Body weight",
    (LOINC, "8302-2"): "Body height",
    (LOINC, "39156-5"): "Body mass index (BMI)",
    (LOINC, "2708-6"): "Oxygen saturation",
    (LOINC, "59408-5"): "Oxygen saturation",
    (LOINC, "72166-2"): "Smoking status",

    # --- ICD-10-CM: common conditions ---------------------------------------
    (ICD10, "E11.9"): "Type 2 diabetes mellitus, without complications",
    (ICD10, "E11.8"): "Type 2 diabetes mellitus, with unspecified complications",
    (ICD10, "E10.9"): "Type 1 diabetes mellitus, without complications",
    (ICD10, "E78.5"): "High cholesterol (hyperlipidemia)",
    (ICD10, "E78.00"): "High cholesterol (hypercholesterolemia)",
    (ICD10, "E66.9"): "Obesity",
    (ICD10, "E03.9"): "Hypothyroidism",
    (ICD10, "I10"): "High blood pressure (essential hypertension)",
    (ICD10, "I25.10"): "Coronary artery disease",
    (ICD10, "I48.91"): "Atrial fibrillation",
    (ICD10, "I50.9"): "Heart failure",
    (ICD10, "J45.909"): "Asthma",
    (ICD10, "J44.9"): "COPD",
    (ICD10, "N18.3"): "Chronic kidney disease, stage 3",
    (ICD10, "N18.9"): "Chronic kidney disease",
    (ICD10, "K21.9"): "Acid reflux (GERD)",
    (ICD10, "M54.5"): "Low back pain",
    (ICD10, "M19.90"): "Osteoarthritis",
    (ICD10, "F32.9"): "Depression",
    (ICD10, "F41.9"): "Anxiety",
    (ICD10, "G47.33"): "Obstructive sleep apnea",
    (ICD10, "R51"): "Headache",
    (ICD10, "Z23"): "Encounter for immunization",

    # --- ICD-9-CM: still common in legacy feeds ------------------------------
    (ICD9, "250.00"): "Type 2 diabetes mellitus",
    (ICD9, "401.9"): "High blood pressure (essential hypertension)",
    (ICD9, "272.4"): "High cholesterol (hyperlipidemia)",
    (ICD9, "278.00"): "Obesity",
    (ICD9, "244.9"): "Hypothyroidism",
    (ICD9, "530.81"): "Acid reflux (GERD)",
    (ICD9, "493.90"): "Asthma",
    (ICD9, "496"): "COPD",
    (ICD9, "311"): "Depression",
    (ICD9, "300.00"): "Anxiety",
    (ICD9, "724.2"): "Low back pain",
    (ICD9, "414.01"): "Coronary artery disease",
    (ICD9, "427.31"): "Atrial fibrillation",
    (ICD9, "428.0"): "Heart failure",
    (ICD9, "585.3"): "Chronic kidney disease, stage 3",

    # --- RxNorm: high-frequency maintenance medications ----------------------
    (RXNORM, "860975"): "Metformin 500 mg",
    (RXNORM, "860974"): "Metformin",
    (RXNORM, "6809"): "Metformin",
    (RXNORM, "29046"): "Lisinopril",
    (RXNORM, "314076"): "Lisinopril 10 mg",
    (RXNORM, "83367"): "Atorvastatin",
    (RXNORM, "617314"): "Atorvastatin 20 mg",
    (RXNORM, "36567"): "Simvastatin",
    (RXNORM, "42463"): "Amlodipine",
    (RXNORM, "197361"): "Amlodipine 5 mg",
    (RXNORM, "5487"): "Hydrochlorothiazide",
    (RXNORM, "38454"): "Metoprolol",
    (RXNORM, "1191"): "Aspirin",
    (RXNORM, "7646"): "Omeprazole",
    (RXNORM, "10582"): "Levothyroxine",
    (RXNORM, "35636"): "Sertraline",
    (RXNORM, "32968"): "Albuterol",
    (RXNORM, "6387"): "Losartan",
    (RXNORM, "3616"): "Gabapentin",
    (RXNORM, "8640"): "Prednisone",

    # --- CVX: vaccines ------------------------------------------------------
    (CVX, "140"): "Influenza vaccine",
    (CVX, "150"): "Influenza vaccine",
    (CVX, "158"): "Influenza vaccine",
    (CVX, "213"): "COVID-19 vaccine",
    (CVX, "208"): "COVID-19 vaccine (Pfizer)",
    (CVX, "207"): "COVID-19 vaccine (Moderna)",
    (CVX, "115"): "Tdap vaccine",
    (CVX, "113"): "Td vaccine",
    (CVX, "133"): "Pneumococcal vaccine (PCV13)",
    (CVX, "33"): "Pneumococcal vaccine (PPSV23)",
    (CVX, "121"): "Shingles vaccine (zoster)",
    (CVX, "187"): "Shingles vaccine (Shingrix)",
    (CVX, "43"): "Hepatitis B vaccine",
    (CVX, "03"): "MMR vaccine",
}

# Codes seen that we had no label for. Read with unlabelled_codes(); this is how
# the map grows from evidence instead of guesswork.
_MISSES: collections.Counter = collections.Counter()


def canonical_system(system: str | None) -> str:
    """Normalize a code system URI (OIDs and http/https variants all appear)."""
    if not system:
        return ""
    return _SYSTEM_ALIASES.get(system.strip(), system.strip())


def lookup(system: str | None, code: str | None) -> str | None:
    """The server's label for a code, or None if we don't know it.

    None is a valid, useful answer: the caller says "a record is here I could
    not read" rather than inventing a name.
    """
    if not code:
        return None
    label = _LABELS.get((canonical_system(system), str(code).strip()))
    if label is None:
        _MISSES[(canonical_system(system), str(code).strip())] += 1
    return label


def unlabelled_codes(limit: int = 50) -> list[tuple[tuple[str, str], int]]:
    """Most-requested codes we have no label for, most frequent first."""
    return _MISSES.most_common(limit)


def reset_unlabelled() -> None:
    _MISSES.clear()


def label_codings(obj):
    """Attach server-derived `display` to any coding we recognise, in place.

    Runs AFTER redaction, never before: redaction removes whatever the upstream
    system put in `display` (which may contain PHI), and this puts back only a
    value from the table above, keyed by code. Nothing patient-specific can
    travel this path.

    A CodeableConcept whose codings are all recognised also gets a `text`, since
    that is the field most consumers read first.
    """
    if isinstance(obj, list):
        for item in obj:
            label_codings(item)
        return obj
    if not isinstance(obj, dict):
        return obj

    codings = obj.get("coding")
    if isinstance(codings, list):
        labels = []
        for coding in codings:
            if not isinstance(coding, dict):
                continue
            label = lookup(coding.get("system"), coding.get("code"))
            if label:
                coding["display"] = label
                labels.append(label)
        if labels and not obj.get("text"):
            obj["text"] = labels[0]

    for value in obj.values():
        if isinstance(value, (dict, list)):
            label_codings(value)
    return obj
