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
- Missing age/sex → `indeterminate`, never a false "due". Each such result also
  carries `indeterminate_reason` naming WHICH demographic was missing, because
  the consumer summary quotes it back to the person and a reason covering both
  is a false claim about the one that was on file (#417). It says what THIS
  CALL was given, not what the record holds: a caller that passes no patient
  gets `birth-date-unknown` on every rule, and report.py is careful not to
  read that as a statement about the person's data.
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
#   related_ecqm: the RELATED CMS eCQM id, for reconciling with certified-measure
#     stacks (e.g. CQL/ELM-to-SQL engines). "Related" — NOT a claim this rule
#     implements the eCQM's logic. None where no clean mapping exists.
CARE_GAP_RULES = [
    {
        "id": "bp-screening", "title": "Blood pressure check",
        "applies": {"sex": None, "min_age": 18, "max_age": 120},
        # The 40+ cadence, and the fallback for any age no band claims.
        "cadence_months": 12,
        # USPSTF screens adults 18-39 with normal readings every 3-5 years and
        # 40+ annually. One yearly figure from 18 told an under-40 patient
        # with a normal reading two years ago that they were due — a gap the
        # guideline does not describe. 36 months is the conservative end of
        # the 3-5 year range.
        #
        # The band lives inside this rule rather than in a second rule
        # because `summary.total` and the rule ids are read by the MCP App
        # page, the brief and the eCQM crosswalk; a cadence correction should
        # not move any of them.
        "cadence_bands": [{"min_age": 18, "max_age": 39,
                           "cadence_months": 36}],
        "satisfied_by": {"resource": "Observation",
                         "codes": {"8480-6", "85354-9", "55284-4"}},
        "source": "uspstf",
        "related_ecqm": "CMS22",
    },
    {
        "id": "lipid-screening", "title": "Cholesterol (lipid) screening",
        "applies": {"sex": None, "min_age": 40, "max_age": 75},
        "cadence_months": 60,
        "satisfied_by": {"resource": "Observation",
                         "codes": {"2093-3", "13457-7", "2571-8", "18262-6"}},
        "source": "uspstf",
        "related_ecqm": None,
    },
    {
        "id": "diabetes-a1c", "title": "Diabetes A1c monitoring",
        "applies": {"sex": None, "min_age": 18, "max_age": 120,
                    "requires_condition": True},
        "cadence_months": 6,
        "satisfied_by": {"resource": "Observation", "codes": {"4548-4", "17856-6"}},
        "source": "ada",
        "related_ecqm": "CMS122",
    },
    {
        "id": "colorectal-screening", "title": "Colorectal cancer screening",
        "applies": {"sex": None, "min_age": 45, "max_age": 75},
        "cadence_months": 120,  # colonoscopy interval (conservative upper bound)
        "satisfied_by": {"resource": "Procedure",
                         "codes": {"45378", "45380", "45385", "44388", "45330"}},
        # USPSTF accepts stool-based screening on its own schedule — FIT
        # annually, FIT-DNA every 1-3 years — and those arrive as lab
        # Observations, not Procedures. This rule reads neither, so an absent
        # colonoscopy is not evidence of an absent screening (#425). Until
        # the modalities are modelled, say what we did not read instead of
        # calling the patient due. Removing this line does not add coverage;
        # it only stops the disclosure.
        "unread_evidence": "stool-based tests (FIT or Cologuard)",
        "source": "uspstf",
        "related_ecqm": "CMS130",
    },
    {
        "id": "cervical-screening", "title": "Cervical cancer screening (Pap)",
        "applies": {"sex": "female", "min_age": 21, "max_age": 65},
        "cadence_months": 36,
        "satisfied_by": {"resource": "Procedure", "codes": {"88175", "88164", "88142"}},
        "source": "uspstf",
        "related_ecqm": "CMS124",
    },
    {
        "id": "mammography", "title": "Breast cancer screening (mammogram)",
        "applies": {"sex": "female", "min_age": 40, "max_age": 74},
        "cadence_months": 24,
        "satisfied_by": {"resource": "Procedure",
                         "codes": {"77067", "77066", "77065"}},
        "source": "uspstf",
        "related_ecqm": "CMS125",
    },
    {
        "id": "flu-immunization", "title": "Influenza (flu) vaccine",
        "applies": {"sex": None, "min_age": 18, "max_age": 120},
        "cadence_months": 12,
        "satisfied_by": {"resource": "Immunization",
                         "codes": {"88", "140", "141", "150", "158", "161", "171"}},
        "source": "acip",
        "related_ecqm": "CMS147",
    },
]

_DIABETES_PREFIXES = ("E10", "E11", "E13", "250")  # ICD-10 / ICD-9 diabetes
# SNOMED diabetes concepts — real-world pulls (Fasten/Epic) code in SNOMED,
# not ICD; matched exactly, never by prefix (SNOMED ids aren't hierarchical
# strings). DM, T2DM, T1DM + common subtype descendants seen in US Core data.
_DIABETES_SNOMED = {
    "73211009",   # Diabetes mellitus
    "44054006",   # Type 2 diabetes mellitus
    "46635009",   # Type 1 diabetes mellitus
    "190330002",  # Type 1 diabetes mellitus with ketoacidosis (legacy)
    "422034002",  # Diabetic retinopathy assoc. with type 2 (marker of dx)
}


def _parse_date(s):
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_birth_date(s):
    """birthDate may be a FHIR partial date (YYYY or YYYY-MM) — our own
    redaction truncates it to the year. Pad to the first day; screening age
    bands are wide enough that the ±1y edge error is acceptable for advisory
    output (and the consumer note already says to confirm with a clinician)."""
    if not isinstance(s, str):
        return None
    if len(s) == 4 and s.isdigit():
        s = f"{s}-01-01"
    elif len(s) == 7:
        s = f"{s}-01"
    return _parse_date(s)


def _age_years(birth, as_of):
    b, a = _parse_birth_date(birth), _parse_date(as_of)
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
            if code in _DIABETES_SNOMED:
                return True
            if any(code.startswith(p) for p in _DIABETES_PREFIXES):
                return True
    return False


def _most_recent(resources, wanted_codes, as_of_date, cadence_months):
    """Most recent matching resource within the cadence window -> its date, else None."""
    best = None
    for r in resources or []:
        if _codes_of(r) & wanted_codes:
            d = _resource_date(r)
            # future-dated records never satisfy a gap — bad source data
            # must not produce a false "up to date"
            if d and d <= as_of_date and (best is None or d > best):
                best = d
    if best is None:
        return None
    if _months_between(best, as_of_date) <= cadence_months:
        return best
    return None  # found, but stale — treat as due (returns None = not satisfied)


def _cadence_for(rule, age):
    """Months between screenings for a patient of `age`, or None when this rule
    has bands and there is no age to choose among them.

    A cadence may depend on age (USPSTF blood pressure: every 3-5 years at
    18-39, annually at 40+). With no age, a banded rule has NOTHING to report:
    its own `cadence_months` is one band's figure — the 40+ one — so quoting it
    on a row that has just said the date of birth is unknown asserts an age
    band the row disclaims in the next field (#616). None is what that row
    knows, and its `note` is what does the talking.

    An age that no band claims is a different case and still gets
    `cadence_months`: a band was consulted for this person and none applied,
    which is a decision rather than an absence.
    """
    if age is None:
        return None if rule.get("cadence_bands") else rule["cadence_months"]
    for band in rule.get("cadence_bands") or ():
        if band["min_age"] <= age <= band["max_age"]:
            return band["cadence_months"]
    return rule["cadence_months"]


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
        # Resolved with the patient's real age, ONCE, so that every row this
        # loop can emit carries a cadence somebody chose for this person — or
        # no cadence at all. It used to be `_cadence_desc(rule["cadence_months"])`
        # here and the banded figure only after the age gate, which gave the
        # rows that never reach the gate the 40+ figure by default (#616).
        # `None` rather than a dropped key: the field is part of the row's
        # shape for every consumer, exactly as `last_done` is.
        cadence_months = _cadence_for(rule, age)
        cadence = (_cadence_desc(cadence_months)
                   if cadence_months is not None else None)
        base = {"rule_id": rule["id"], "title": rule["title"],
                "cadence": cadence, "source": rule["source"],
                "related_ecqm": rule["related_ecqm"],
                "last_done": None, "note": ""}

        # Sex gate
        if applies["sex"] and gender and gender != applies["sex"]:
            results.append({**base, "applicable": False,
                            "status": "not_applicable",
                            "note": f"applies to {applies['sex']} patients"})
            continue
        # Condition gate (e.g. A1c only for known diabetes).
        #
        # The note is a claim about the DATA, not about the person. "Applies
        # to patients with a diabetes diagnosis" reads, next to a screening
        # that has been set aside, as a finding that this person does not have
        # diabetes — which this gate never established. All it saw was the
        # Conditions in the connected records, and those are routinely
        # incomplete.
        if applies.get("requires_condition") and not diabetic:
            results.append({**base, "applicable": False,
                            "status": "not_applicable",
                            "note": ("no diabetes diagnosis found in your "
                                     "connected records")})
            continue
        # Age gate — unknown age on an age-gated rule is indeterminate, never a false due
        if age is None:
            results.append({**base, "applicable": None, "status": "indeterminate",
                            "indeterminate_reason": "birth-date-unknown",
                            "note": "date of birth unknown — cannot determine eligibility"})
            continue
        if not (applies["min_age"] <= age <= applies["max_age"]):
            results.append({**base, "applicable": False, "status": "not_applicable",
                            "note": f"recommended ages {applies['min_age']}-{applies['max_age']}"})
            continue
        if applies["sex"] and not gender:
            results.append({**base, "applicable": None, "status": "indeterminate",
                            "indeterminate_reason": "sex-unknown",
                            "note": "sex not recorded — cannot determine eligibility"})
            continue

        # Is there a satisfying record in the connected data? `cadence_months`
        # is never None below: the age gate above returns for `age is None`,
        # and only a banded rule with no age has no cadence.
        last = _most_recent(by_resource[rule["satisfied_by"]["resource"]],
                            rule["satisfied_by"]["codes"], as_of_date,
                            cadence_months)
        if last is not None:
            results.append({**base, "applicable": True, "status": "up_to_date",
                            "last_done": last.isoformat(),
                            "note": f"recommended {cadence}"})
        elif rule.get("unread_evidence"):
            # We did not find the evidence this rule reads, and this rule is
            # known not to read everything that satisfies it. "Due" would be a
            # claim about the patient built out of a gap in our own model
            # (#425), so name the gap. The prompt to act survives, because a
            # patient who has genuinely never been screened still needs it.
            results.append({**base, "applicable": True, "status": "indeterminate",
                            "indeterminate_reason": "evidence-not-read",
                            "unread_evidence": rule["unread_evidence"],
                            "note": (f"we do not yet read {rule['unread_evidence']}, "
                                     "so we cannot tell whether this is up to "
                                     "date — worth raising with your clinician")})
        else:
            results.append({**base, "applicable": True, "status": "due",
                            "note": ("no record found in your connected data — "
                                     "you may already be up to date elsewhere; "
                                     "confirm with your clinician")})
    return results
