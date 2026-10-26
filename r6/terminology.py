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

A plain dict, consulted first
-----------------------------
The table below is still a plain dict, and it still answers without a network
call, a timeout, or a cache. Every read hits it first.

It used to be the ONLY source, on the reasoning that a dict cannot fail in
production. That reasoning was right and is unchanged; what changed is the
evidence about coverage. Measured against a live MEDENT import on 2026-08-04:
**1 of 15 distinct ICD-10-CM codes and 0 of 11 SNOMED codes carried a label**,
so the agent named one condition and called the other 25 unreadable. A
121-entry hand-curated map cannot cover a real EHR export.

So a MISS — and only a miss — may now consult `r6/terminology_resolver.py`,
which is opt-in per deployment, budgeted so it cannot slow a read, and returns
None on every failure. Read its module docstring before changing it; the three
properties it must not lose are written down there.

Unknown codes are still LEFT UNLABELLED rather than guessed: the honest "a
record is here that I could not read" fallback in the agent is far better than
a confident invention, and `unlabelled_codes()` reports what is missing so the
static map can still grow from evidence.

Coverage is primary-care-weighted (the records consumers actually connect):
common chemistry and hematology panels, vitals, the widespread chronic
conditions, and high-frequency maintenance medications.
"""

from __future__ import annotations

import collections
import re

from r6 import terminology_resolver as _resolver_mod
from r6.safe_read import coding_system

# System URIs, canonical form.
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ICD9 = "http://hl7.org/fhir/sid/icd-9-cm"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SNOMED = "http://snomed.info/sct"
CVX = "http://hl7.org/fhir/sid/cvx"
#: The prefix of HL7's own code systems (terminology.hl7.org).
_HL7 = "http://terminology.hl7.org/CodeSystem/"

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
    # The rest of a basic and comprehensive metabolic panel. Without them the
    # visit brief showed the resolver's LOINC long name ("Anion gap in Serum
    # or Plasma by calculation").
    (LOINC, "33037-3"): "Anion gap",
    (LOINC, "1863-0"): "Anion gap",
    (LOINC, "10466-1"): "Anion gap",
    (LOINC, "3097-3"): "BUN/creatinine ratio",
    (LOINC, "10834-0"): "Globulin",
    (LOINC, "1759-0"): "Albumin/globulin ratio",
    (LOINC, "48642-3"): "Estimated GFR",
    (LOINC, "48643-1"): "Estimated GFR",
    (LOINC, "62238-1"): "Estimated GFR",
    (LOINC, "98979-8"): "Estimated GFR",
    # "INR in Platelet poor plasma by Coagulation assay" (tx.fhir.org
    # $lookup, 2026-10-26).
    (LOINC, "6301-6"): "INR",

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
    # The check-in test patient's allergen (#959), the ingredient
    # penicillin G. Unlabelled, a clinic saw the bare code 7980. "Penicillin",
    # the name the wallet's review shows (tests/test_checkin_wallet.py).
    (RXNORM, "7980"): "Penicillin",

    # --- SNOMED CT: codes the product carries (#577) -------------------------
    # Only codes found in our seed/demo data, fixtures or rule sets
    # (tests/test_terminology_snomed.py enforces that). Each label is the
    # display tx.fhir.org's $lookup returned, verbatim, from the International
    # edition 20250201 — not a paraphrase.
    (SNOMED, "91936005"): "Allergy to penicillin",
    (SNOMED, "91935009"): "Allergy to peanut",
    (SNOMED, "227493005"): "Cashew nut",
    (SNOMED, "73211009"): "Diabetes mellitus",
    (SNOMED, "44054006"): "Type 2 diabetes mellitus",
    (SNOMED, "46635009"): "Type 1 diabetes mellitus",
    (SNOMED, "190330002"): "Hyperosmolar coma due to type 1 diabetes mellitus",
    (SNOMED, "422034002"): "Retinopathy due to type 2 diabetes mellitus",
    (SNOMED, "38341003"): "Hypertensive disorder",
    (SNOMED, "59621000"): "Essential hypertension",
    (SNOMED, "1201005"): "Benign essential hypertension",
    (SNOMED, "46177005"): "End-stage renal disease",
    (SNOMED, "236434000"): (
        "End stage renal failure untreated by renal replacement therapy"),
    (SNOMED, "77386006"): "Pregnancy",
    (SNOMED, "72892002"): "Normal pregnancy",
    (SNOMED, "9014002"): "Psoriasis",
    (SNOMED, "6142004"): "Influenza",
    (SNOMED, "82272006"): "Common cold",
    (SNOMED, "118944007"): "Disorder of shoulder",
    (SNOMED, "80146002"): "Appendectomy",
    (SNOMED, "26643006"): "Oral route",
    (SNOMED, "77176002"): "Smoker",
    (SNOMED, "65568007"): "Cigarette smoker",
    (SNOMED, "8517006"): "Ex-smoker",
    (SNOMED, "449868002"): "Smokes tobacco daily",
    (SNOMED, "428041000124106"): "Occasional tobacco smoker",
    (SNOMED, "266919005"): "Never smoked tobacco",
    (SNOMED, "221000119102"): "Never smoked any substance",
    # The check-in test patient (#959). tx.fhir.org $lookup, 2026-10-16.
    (SNOMED, "195967001"): "Asthma",
    (SNOMED, "271807003"): "Eruption",

    # --- HL7 terminology: the check-in test patient's coded fields (#959) --
    # Each label is the code system's own display, so a validator that
    # compares coding.display against it agrees. Not v3-NullFlavor: a
    # null flavour gets no label (#957 round 3).
    (_HL7 + "allergyintolerance-clinical", "active"): "Active",
    (_HL7 + "allergyintolerance-verification", "confirmed"): "Confirmed",
    (_HL7 + "condition-clinical", "active"): "Active",
    (_HL7 + "condition-ver-status", "confirmed"): "Confirmed",
    (_HL7 + "condition-category", "problem-list-item"): "Problem List Item",
    (_HL7 + "coverage-class", "group"): "Group",
    (_HL7 + "coverage-class", "plan"): "Plan",
    (_HL7 + "medicationrequest-category", "community"): "Community",
    (_HL7 + "organization-type", "pay"): "Payer",
    (_HL7 + "subscriber-relationship", "self"): "Self",
    (_HL7 + "v2-0203", "MB"): "Member Number",
    (_HL7 + "v3-orderableDrugForm", "TAB"): "Tablet",
    ("https://nahdo.org/sopt", "5"): "PRIVATE HEALTH INSURANCE",
    ("urn:ietf:bcp:47", "en"): "English",

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

    # --- v3-ActReason: the reason r6/curatr.py writes on a fix's Provenance -
    ("http://terminology.hl7.org/CodeSystem/v3-ActReason", "PATADMIN"):
        "Patient administration",
    # The synthetic-data marker the check-in export carries across
    # (r6/access.py _test_markers). The code system's own display, so a
    # validator that checks coding.display agrees with it.
    ("http://terminology.hl7.org/CodeSystem/v3-ActReason", "HTEST"):
        "test health data",
}

#: SNOMED "no known allergy": 716186003 and every descendant (tx.fhir.org,
#: isa/716186003, 2026-10-13). And "patient not asked" (1631000175102), an
#: allergy-status concept. Their names are statements about allergies, not
#: names of anything: written as `display`/`text`, "No known allergy" reached
#: every MCP client as a bare fact about the person that no one attested
#: (#950). The one source of truth: r6/brief/engine.py imports these, and
#: careagents/agent.py mirrors them under a parity test.
#: 160244002 is the inactive predecessor of 716186003 (retired 2016-07-31,
#: tx.fhir.org $lookup 2026-10-14): not a descendant, but legacy feeds still
#: carry it and the resolver still names it "No known allergies" (#957).
NKA_CONCEPTS = frozenset((SNOMED, c) for c in (
    "716186003", "409137002", "428197003", "428607008", "429625007",
    "716220001", "1003774007", "160244002",
    # "None" (qualifier value): an allergy coded with it read back named
    # "None" (#957 delta 4).
    "260413007"))
#: Not LOINC LA14761-3 "Not asked": a generic answer used on any question,
#: so "allergies were not checked" was false on "Smoking status: Not asked"
#: (#957 delta 5). Only an allergy-specific concept belongs in this set.
ALLERGY_STATUS_CONCEPTS = frozenset({
    (SNOMED, "1631000175102"),      # Patient not asked
})
#: LOINC answers that state an absence: "None known", "None or none known"
#: and "None or not known" (tx.fhir.org $lookup, 2026-10-14). An Observation
#: for 52571-7, known allergies, answered LA10030-7 read back as a bare
#: "None known" (#957 round 2).
LOINC_ABSENCE_ANSWERS = frozenset((LOINC, c) for c in (
    "LA10030-7", "LA9134-3", "LA4715-4"))
ABSENCE_CONCEPTS = NKA_CONCEPTS | ALLERGY_STATUS_CONCEPTS | LOINC_ABSENCE_ANSWERS

#: What label_codings writes as `text` for an absence concept: a record
#: entry, never a fact. Keyed by code only, so no date or other detail from
#: the record can enter it. Wording owner-approved 2026-10-14 (#950); new
#: rows follow the approved pattern.
_NKA_ENTRY = ("Recorded entry: '{}'. Not a confirmed absence; confirm with "
              "a clinician.")
_ABSENCE_TEXT: dict[tuple[str, str], str] = {
    (SNOMED, "716186003"): _NKA_ENTRY.format("no known allergy"),
    (SNOMED, "409137002"): _NKA_ENTRY.format("no known drug allergy"),
    (SNOMED, "428197003"): _NKA_ENTRY.format("no known insect allergy"),
    (SNOMED, "428607008"): _NKA_ENTRY.format("no known environmental allergy"),
    (SNOMED, "429625007"): _NKA_ENTRY.format("no known food allergy"),
    (SNOMED, "716220001"): _NKA_ENTRY.format("no known animal allergy"),
    (SNOMED, "1003774007"): _NKA_ENTRY.format("no known latex allergy"),
    (SNOMED, "160244002"): _NKA_ENTRY.format("no known allergies"),
    (SNOMED, "260413007"): _NKA_ENTRY.format("none"),
    (LOINC, "LA10030-7"): _NKA_ENTRY.format("none known"),
    (LOINC, "LA9134-3"): _NKA_ENTRY.format("none known"),
    (LOINC, "LA4715-4"): _NKA_ENTRY.format("none or not known"),
    (SNOMED, "1631000175102"): (
        "Recorded entry: 'patient not asked about allergies'. Allergies were "
        "not checked; confirm with a clinician."),
}

# Codes seen that we had no label for. Read with unlabelled_codes(); this is how
# the map grows from evidence instead of guesswork.
_MISSES: collections.Counter = collections.Counter()

#: A SNOMED CT concept id: 6 to 18 digits, no leading zero.
_SNOMED_CODE = re.compile(r"[1-9][0-9]{5,17}")
#: Resolver answers that state an absence about allergies, not a name. The
#: closed sets above are the real control; this is the backstop for codes
#: they lag. A label is refused when it is:
#:   - "no known ..." anything, or an NK..A abbreviation (NKA, NKDA, NKFA);
#:   - a bare "None" / "Nil", or "None known" / "Nil known" (or "None or
#:     none/not known"), as the whole label (#957 delta 4);
#:   - a negation (English, German, French, Spanish, Italian) in a label
#:     that is about allergies, sensitivities, intolerances or reactions.
#: A negation on any other topic is a name: "None detected", "Nil by mouth",
#: "Sin Nombre virus" (#957 round 3).
_ABSENCE_WORDING = re.compile(
    r"\bno known\b|\bnk[a-z]{0,2}a\b"
    r"|^\s*(?:none|nil)(?: or (?:none|not))? known\s*$"
    r"|^\s*(?:none|nil)\s*$"
    r"|^(?=.*(?:al+erg|sensitiv|intoleran|reaction))"
    r"(?=.*(?:\bno\b|\bnone\b|\bnil\b|\bnot (?:asked|known)\b"
    r"|\bkeine?\b|\baucune?\b|\bsin\b|\bning[uú]n[ao]?\b|\bnessun[ao]?\b))",
    re.IGNORECASE)


def canonical_system(system: str | None) -> str:
    """Normalize a code system URI (OIDs and http/https variants all appear).

    A system that is not a string (a list, an object, a number) reads as
    absent. Stored as written, one such Coding made every redacted read on
    the tenant raise here and return 500 (#885).
    """
    if not isinstance(system, str) or not system:
        return ""
    return _SYSTEM_ALIASES.get(system.strip(), system.strip())


def lookup(system: str | None, code: str | None) -> str | None:
    """The server's label for a code, or None if we don't know it.

    Static table first, then the opt-in runtime resolver on a miss. None is
    still a valid, useful answer: the caller says "a record is here I could
    not read" rather than inventing a name.

    The resolver is reached through the MODULE attribute, not a bound import,
    so a test can replace `r6.terminology_resolver.resolve` by path and have
    this see it.
    """
    # A code is a string; an int (CVX "3" written as 3) still reads as one.
    # A list, an object or a bool is no code, and is never stringified into
    # the miss counter (#885).
    if isinstance(code, bool) or not isinstance(code, (str, int)) or code == "":
        return None
    key = (canonical_system(system), str(code).strip())
    # A SNOMED code that is not a well-formed concept id is never looked up:
    # the resolver names "0716186003" and "716186003|No known allergy|" as
    # absences the guard cannot key (#957), and free text sent to it leaves
    # the deployment. Not counted as a miss either.
    if key[0] == SNOMED and not _SNOMED_CODE.fullmatch(key[1]):
        return None
    label = _LABELS.get(key)
    if label is not None:
        return label

    try:
        label = _resolver_mod.resolve(*key)
    except Exception:  # noqa: BLE001 — a label is never worth failing a read
        label = None
    # An answer that reads as an absence or status statement is never a
    # label, whatever the code: ABSENCE_CONCEPTS can lag SNOMED (#957).
    if label is not None and _ABSENCE_WORDING.search(label):
        return None
    if label is not None:
        return label

    _MISSES[key] += 1
    return None


def _absence_key(system, code) -> tuple[str, str] | None:
    """(system, code) keyed exactly as lookup keys it, if it is an absence
    concept: an int code reads as its digits, a bool or list is no code."""
    if isinstance(code, bool) or not isinstance(code, (str, int)) or code == "":
        return None
    key = (canonical_system(system), str(code).strip())
    return key if key in ABSENCE_CONCEPTS else None


def malformed_snomed_code(system, code) -> bool:
    """A string code in SNOMED's system that is not a concept id, such as
    "716186003|No known allergy|": it carries free text in the code slot."""
    return (isinstance(code, str) and canonical_system(system) == SNOMED
            and not _SNOMED_CODE.fullmatch(code.strip()))


def is_absence_coding(coding) -> bool:
    """True for a Coding of one of ABSENCE_CONCEPTS, in any system spelling."""
    return (isinstance(coding, dict)
            and _absence_key(coding_system(coding), coding.get("code")) is not None)


def absence_text(system, code) -> str | None:
    """The qualified entry text for an absence concept, else None."""
    key = _absence_key(system, code)
    return _ABSENCE_TEXT.get(key) if key else None


#: ICD-10-CM Z00-Z13: encounters for examination and screening. They say why
#: a visit happened, not what the person has, so a problem list that carries
#: them is not a list of conditions. Z23, "Encounter for immunization", is
#: the same kind of code and already in the table above.
_ICD10_ENCOUNTER = re.compile(r"Z(?:0\d|1[0-3]|23)")

#: SNOMED CT encounter, examination and screening concepts that real feeds
#: write onto a problem list. Each is a procedure in SNOMED's own hierarchy
#: (tx.fhir.org $lookup FSN, 2026-10-26), not a disorder or finding. A closed
#: list: there is no local SNOMED hierarchy to ask, and a concept missing here
#: stays a condition rather than being guessed away.
_SNOMED_ENCOUNTER = frozenset({
    "268565007",  # Adult health examination (procedure)
    "185349003",  # Encounter for check up (procedure)
    "162673000",  # General examination of patient (procedure)
    "410620009",  # Well child visit (procedure)
    "86013001",   # Periodic reevaluation and management of healthy individual
    "390906007",  # Follow-up encounter (procedure)
    "308335008",  # Patient encounter procedure (procedure)
    "185389009",  # Follow-up visit (procedure)
    "185317003",  # Telephone encounter (procedure)
    "439708006",  # Home visit (procedure)
    "4525004",    # Emergency department patient visit (procedure)
    "11429006",   # Consultation (procedure)
    "281036007",  # Follow-up consultation (procedure)
    "270427003",  # Patient-initiated encounter (procedure)
    "448337001",  # Telemedicine consultation with patient (procedure)
    "185345009",  # Encounter for symptom (procedure)
    "171207006",  # Depression screening (procedure)
    "268547008",  # Screening for malignant neoplasm of breast (procedure)
})


def is_encounter_concept(system, code) -> bool:
    """True for a code that names a visit, an examination or a screening
    rather than a condition: ICD-10-CM Z00-Z13 (and Z23), and the SNOMED
    concepts above. Keyed by code only; an unknown code is not one."""
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        return False
    system, code = canonical_system(system), str(code).strip().upper()
    if system == ICD10:
        return bool(_ICD10_ENCOUNTER.match(code))
    if system == SNOMED:
        return code in _SNOMED_ENCOUNTER
    return False


#: LOINC's long names carry the property in brackets, the method after "by"
#: and the specimen after "in". A person needs the analyte. The specimen is
#: dropped only when it is blood, serum or plasma, the default a reader
#: assumes; "Protein in Urine" keeps its specimen, which changes the meaning.
_LOINC_PROPERTY = re.compile(r"\s*\[[^\]]*\]")
_LOINC_METHOD = re.compile(r"\s+by\s+.*$", re.IGNORECASE)
_LOINC_BLOOD = re.compile(
    r"\s+in\s+(?:(?:Serum|Plasma|Blood)(?:,\s*|\s+or\s+)?)+"
    r"(?:\s+(?:Serum|Plasma|Blood))?\s*$",
    re.IGNORECASE)


def plain_lab_name(name: str) -> str:
    """A LOINC long name as a person reads it: "Anion gap in Serum or Plasma
    by calculation" is "Anion gap". Works on a terminology service's name
    for a code, never on upstream text."""
    if not isinstance(name, str):
        return ""
    out = _LOINC_PROPERTY.sub("", name)
    out = _LOINC_METHOD.sub("", out)
    out = _LOINC_BLOOD.sub("", out)
    return " ".join(out.split()) or name.strip()


def lab_label(system, code) -> str | None:
    """`lookup`, with a LOINC name the table does not carry made plain. The
    table's own labels are already plain and are returned as written."""
    label = lookup(system, code)
    if label is None or canonical_system(system) != LOINC:
        return label
    key = (LOINC, str(code).strip())
    return label if key in _LABELS else plain_lab_name(label)


#: What kind of visit an Encounter was, keyed by code. Kept apart from
#: _LABELS: these are words for a visit list, not names a coding should
#: carry as its `display` everywhere (label_codings).
_ACT = _HL7 + "v3-ActCode"
_CPT = "http://www.ama-assn.org/go/cpt"
_VISIT_LABELS: dict[tuple[str, str], str] = {
    # Encounter.class, v3-ActCode.
    (_ACT, "AMB"): "Office visit",
    (_ACT, "EMER"): "Emergency visit",
    (_ACT, "IMP"): "Hospital stay",
    (_ACT, "ACUTE"): "Hospital stay",
    (_ACT, "NONAC"): "Hospital stay",
    (_ACT, "OBSENC"): "Hospital observation",
    (_ACT, "SS"): "Same-day procedure",
    (_ACT, "VR"): "Video visit",
    (_ACT, "HH"): "Home visit",
    (_ACT, "FLD"): "Visit outside the office",
    # SNOMED encounter types (the procedure concepts above).
    (SNOMED, "185349003"): "Check-up",
    (SNOMED, "268565007"): "Check-up",
    (SNOMED, "162673000"): "Check-up",
    (SNOMED, "86013001"): "Check-up",
    (SNOMED, "410620009"): "Well-child visit",
    (SNOMED, "390906007"): "Follow-up visit",
    (SNOMED, "185389009"): "Follow-up visit",
    (SNOMED, "281036007"): "Follow-up visit",
    (SNOMED, "185317003"): "Phone visit",
    (SNOMED, "448337001"): "Video visit",
    (SNOMED, "439708006"): "Home visit",
    (SNOMED, "4525004"): "Emergency visit",
    (SNOMED, "11429006"): "Consultation",
}
# CPT evaluation-and-management ranges, in our own words for each range (not
# the code set's descriptors).
_VISIT_LABELS.update({(_CPT, str(c)): "Office visit"
                      for c in (*range(99202, 99206), *range(99211, 99216))})
_VISIT_LABELS.update({(_CPT, str(c)): "Preventive visit"
                      for c in (*range(99381, 99388), *range(99391, 99398))})
_VISIT_LABELS.update({(_CPT, str(c)): "Emergency visit"
                      for c in range(99281, 99286)})
_VISIT_LABELS.update({(_CPT, str(c)): "Hospital stay"
                      for c in (*range(99221, 99224), *range(99231, 99234))})
_VISIT_LABELS.update({(_CPT, str(c)): "Phone visit"
                      for c in range(99441, 99444)})


def visit_label(system, code) -> str | None:
    """A plain word for a kind of visit, by code, or None."""
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        return None
    return _VISIT_LABELS.get((canonical_system(system), str(code).strip()))


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

    An absence concept (ABSENCE_CONCEPTS) gets no `display`: its name reads as
    a fact about the person (#950). A concept coded ONLY with absence concepts
    gets the qualified entry text from _ABSENCE_TEXT instead. Beside any other
    coding it gets none, since that coding may name a real substance.
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
        absences = []
        for coding in codings:
            if not isinstance(coding, dict):
                continue
            entry = absence_text(coding_system(coding), coding.get("code"))
            if entry:
                coding.pop("display", None)
                absences.append(entry)
                continue
            label = lookup(coding_system(coding), coding.get("code"))
            if label:
                coding["display"] = label
                labels.append(label)
        if absences and len(absences) == len(codings):  # absences only
            labels = absences
        if labels and not obj.get("text"):
            obj["text"] = labels[0]

    for value in obj.values():
        if isinstance(value, (dict, list)):
            label_codings(value)
    return obj
