"""Care-gaps engine — pure preventive-care evaluation (no Flask/DB).

Given a patient's own record (Patient + Conditions/Observations/Immunizations/
Procedures), evaluate a small set of adult preventive-care rules and report what
appears due. This is decision support based on published guidelines — NOT a
directive and NOT personalized medical advice.

Honesty posture (matches the lab interpreter):
- Every rule cites a guideline `source`; a test enforces it.
- Population-level adult defaults. Individual risk (family history, prior
  abnormal results, pregnancy) legitimately changes cadence — noted in output.
- "Due" means no satisfying record was found in the CONNECTED data. It is NOT a
  claim the screening wasn't done elsewhere — the consumer wording says so.
- Missing age/sex → `indeterminate`, never a false "due".
"""

from __future__ import annotations

from datetime import date

REFERENCES = {
    "uspstf": "U.S. Preventive Services Task Force recommendations (adult, general population).",
    "acip": "CDC/ACIP adult immunization schedule.",
    "ada": "American Diabetes Association Standards of Care.",
}

# Each rule:
#   applies: {sex: "female"|"male"|None, min_age, max_age}  (age in years, at as_of)
#   cadence_months: expected interval; satisfied if a matching resource falls within
#   satisfied_by: {resource, systemless codes matched on code value}
#   source: key into REFERENCES
CARE_GAP_RULES = [
    {
        "id": "bp-screening", "title": "Blood pressure check",
        "applies": {"sex": None, "min_age": 18, "max_age": 120},
        "cadence_months": 12,
        "satisfied_by": {"resource": "Observation",
                         "codes": {"8480-6", "85354-9", "55284-4"}},
        "source": "uspstf",
    },
    {
        "id": "lipid-screening", "title": "Cholesterol (lipid) screening",
        "applies": {"sex": None, "min_age": 40, "max_age": 75},
        "cadence_months": 60,
        "satisfied_by": {"resource": "Observation",
                         "codes": {"2093-3", "13457-7", "2571-8", "18262-6"}},
        "source": "uspstf",
    },
    {
        "id": "diabetes-a1c", "title": "Diabetes A1c monitoring",
        "applies": {"sex": None, "min_age": 18, "max_age": 120,
                    "requires_condition": True},
        "cadence_months": 6,
        "satisfied_by": {"resource": "Observation", "codes": {"4548-4", "17856-6"}},
        "source": "ada",
    },
    {
        "id": "colorectal-screening", "title": "Colorectal cancer screening",
        "applies": {"sex": None, "min_age": 45, "max_age": 75},
        "cadence_months": 120,  # colonoscopy interval (conservative upper bound)
        "satisfied_by": {"resource": "Procedure",
                         "codes": {"45378", "45380", "45385", "44388", "45330"}},
        "source": "uspstf",
    },
    {
        "id": "cervical-screening", "title": "Cervical cancer screening (Pap)",
        "applies": {"sex": "female", "min_age": 21, "max_age": 65},
        "cadence_months": 36,
        "satisfied_by": {"resource": "Procedure", "codes": {"88175", "88164", "88142"}},
        "source": "uspstf",
    },
    {
        "id": "mammography", "title": "Breast cancer screening (mammogram)",
        "applies": {"sex": "female", "min_age": 40, "max_age": 74},
        "cadence_months": 24,
        "satisfied_by": {"resource": "Procedure",
                         "codes": {"77067", "77066", "77065"}},
        "source": "uspstf",
    },
    {
        "id": "flu-immunization", "title": "Influenza (flu) vaccine",
        "applies": {"sex": None, "min_age": 18, "max_age": 120},
        "cadence_months": 12,
        "satisfied_by": {"resource": "Immunization",
                         "codes": {"88", "140", "141", "150", "158", "161", "171"}},
        "source": "acip",
    },
]

_DIABETES_PREFIXES = ("E10", "E11", "E13", "250")  # ICD-10 / ICD-9 diabetes


def _parse_date(s):
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _age_years(birth, as_of):
    b, a = _parse_date(birth), _parse_date(as_of)
    if not b or not a:
        return None
    return a.year - b.year - ((a.month, a.day) < (b.month, b.day))


def _months_between(earlier, later):
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def _codes_of(resource):
    out = set()
    for c in resource.get("code", {}).get("coding", []):
        if c.get("code"):
            out.add(c["code"])
    return out


def _resource_date(resource):
    for f in ("effectiveDateTime", "performedDateTime", "occurrenceDateTime",
              "authoredOn"):
        d = _parse_date(resource.get(f))
        if d:
            return d
    return None


def _has_diabetes(conditions):
    for c in conditions or []:
        for code in _codes_of(c):
            if any(code.startswith(p) for p in _DIABETES_PREFIXES):
                return True
    return False


def _most_recent(resources, wanted_codes, as_of_date, cadence_months):
    """Most recent matching resource within the cadence window -> its date, else None."""
    best = None
    for r in resources or []:
        if _codes_of(r) & wanted_codes:
            d = _resource_date(r)
            if d and (best is None or d > best):
                best = d
    if best is None:
        return None
    if _months_between(best, as_of_date) <= cadence_months:
        return best
    return None  # found, but stale — treat as due (returns None = not satisfied)


def _cadence_desc(months):
    if months % 12 == 0:
        yrs = months // 12
        return "yearly" if yrs == 1 else f"every {yrs} years"
    return f"every {months} months"


def evaluate_care_gaps(patient, conditions=None, observations=None,
                       immunizations=None, procedures=None, as_of=None):
    """Return a list of per-rule results. `as_of` is 'YYYY-MM-DD' (defaults handled
    by the caller; the engine requires it to be deterministic/testable)."""
    by_resource = {
        "Observation": observations or [],
        "Procedure": procedures or [],
        "Immunization": immunizations or [],
    }
    gender = (patient or {}).get("gender")
    age = _age_years((patient or {}).get("birthDate"), as_of)
    as_of_date = _parse_date(as_of)
    diabetic = _has_diabetes(conditions)

    results = []
    for rule in CARE_GAP_RULES:
        applies = rule["applies"]
        cadence = _cadence_desc(rule["cadence_months"])
        base = {"rule_id": rule["id"], "title": rule["title"],
                "cadence": cadence, "source": rule["source"],
                "last_done": None, "note": ""}

        # Sex gate
        if applies["sex"] and gender and gender != applies["sex"]:
            results.append({**base, "applicable": False,
                            "status": "not_applicable",
                            "note": f"applies to {applies['sex']} patients"})
            continue
        # Condition gate (e.g. A1c only for known diabetes)
        if applies.get("requires_condition") and not diabetic:
            results.append({**base, "applicable": False,
                            "status": "not_applicable",
                            "note": "applies to patients with a diabetes diagnosis"})
            continue
        # Age gate — unknown age on an age-gated rule is indeterminate, never a false due
        if age is None:
            results.append({**base, "applicable": None, "status": "indeterminate",
                            "note": "date of birth unknown — cannot determine eligibility"})
            continue
        if not (applies["min_age"] <= age <= applies["max_age"]):
            results.append({**base, "applicable": False, "status": "not_applicable",
                            "note": f"recommended ages {applies['min_age']}-{applies['max_age']}"})
            continue
        if applies["sex"] and not gender:
            results.append({**base, "applicable": None, "status": "indeterminate",
                            "note": "sex not recorded — cannot determine eligibility"})
            continue

        # Applicable — is there a satisfying record in the connected data?
        last = _most_recent(by_resource[rule["satisfied_by"]["resource"]],
                            rule["satisfied_by"]["codes"], as_of_date,
                            rule["cadence_months"])
        if last is not None:
            results.append({**base, "applicable": True, "status": "up_to_date",
                            "last_done": last.isoformat(),
                            "note": f"recommended {cadence}"})
        else:
            results.append({**base, "applicable": True, "status": "due",
                            "note": ("no record found in your connected data — "
                                     "you may already be up to date elsewhere; "
                                     "confirm with your clinician")})
    return results
