# Care-gap rule register

**For clinical review and initialling.** One page per what the preventive-care
check actually does, so a reviewing clinician can say which rules are released
to patients and which are not.

Every value below is transcribed from `r6/caregaps/evaluate.py` —
`CARE_GAP_RULES` and the evaluator around it — and not from anyone's
recollection of a guideline. Where the code does not encode something a
reviewer would want, the entry says **not encoded** rather than filling it in.
That is deliberate: an unencoded value written here would become the thing the
next reader trusts.

Generated against the rule table as of this document's commit. The code is the
source of truth: `tests/test_caregaps_evaluate.py` pins the cadences and bands,
and `tests/test_care_gap_register_drift.py` fails if this page stops matching
them.

---

## How to read this

- **Population** — who the rule fires for. Age is in years at the date of the
  check; sex is the FHIR `Patient.gender` value.
- **Cadence** — the interval after which a previous record no longer counts.
  A record older than this is treated as absent.
- **What closes the gap** — the FHIR resource type and the exact code values
  read. **No code system is checked**: a code matches on its value alone,
  wherever it came from.
- **What is NOT read** — evidence that satisfies this screening in practice and
  that this check cannot see. This is the column that decides whether "due" is
  a safe thing to tell someone.
- **Status** — `released` (patients see a verdict), or
  `indeterminate by design` (the rule deliberately declines to decide).

Section [Applies to every rule](#applies-to-every-rule) carries the limits that
are properties of the evaluator rather than of any one rule. **Read it before
initialling any individual rule** — several of them change what a "due" or an
"up to date" means.

---

## 1. `bp-screening` — Blood pressure check

| Field | As encoded |
| --- | --- |
| Population | Any sex, ages 18–120 |
| Cadence | **18–39: every 36 months. 40 and over: every 12 months.** |
| Source | `uspstf` — "U.S. Preventive Services Task Force recommendations (adult, general population)." Guideline year: **not encoded** |
| What closes the gap | `Observation` with code `8480-6`, `85354-9` or `55284-4` |
| What is NOT read | The reading's own value. A recorded blood pressure closes the gap whether it was normal or not — so an under-40 patient on the 36-month band is placed there by age alone, not by having had normal readings |
| Related eCQM | CMS22 (related, not implemented) |
| Status | released |

**Changed by council ruling D14 (2026-09-02).** This rule read every 12 months
from age 18, which told an under-40 patient with a normal reading two years ago
that they were due. The age band is encoded inside this one rule
(`cadence_bands`) rather than as a second rule, so rule ids and the rule count
are unchanged for every consumer.

**For the reviewer:** the 36-month figure is the conservative end of a 3–5 year
interval, and the guideline conditions that interval on the previous readings
being normal — which, per the row above, this rule does not check.

---

## 2. `lipid-screening` — Cholesterol (lipid) screening

| Field | As encoded |
| --- | --- |
| Population | Any sex, ages 40–75 |
| Cadence | Every 60 months |
| Source | `uspstf`. Guideline year: **not encoded** |
| What closes the gap | `Observation` with code `2093-3`, `13457-7`, `2571-8` or `18262-6` |
| What is NOT read | Risk factors of any kind. The interval is a flat 5 years for everyone in the age band |
| Related eCQM | none — no clean mapping, recorded as `None` rather than guessed |
| Status | released |

---

## 3. `diabetes-a1c` — Diabetes A1c monitoring

| Field | As encoded |
| --- | --- |
| Population | Any sex, ages 18–120, **and** a diabetes diagnosis in the connected records |
| Cadence | Every 6 months |
| Source | `ada` — "American Diabetes Association Standards of Care." Guideline year: **not encoded** |
| What closes the gap | `Observation` with code `4548-4` or `17856-6` |
| What is NOT read | The A1c result itself — any recorded A1c closes the gap regardless of value, so the rule reports *monitoring*, never control. `Condition.clinicalStatus` is also ignored: a resolved or refuted diabetes Condition still puts a patient in the population |
| Related eCQM | CMS122 (related, not implemented) |
| Status | released — **and the one rule flagged as unreviewed** |

**Diagnosis detection** reads `Condition` codes and matches:

- ICD-10 / ICD-9 by prefix: `E10`, `E11`, `E13`, `250`
- SNOMED exactly (never by prefix): `73211009`, `44054006`, `46635009`,
  `190330002`, `422034002`

**When no diagnosis is found**, the rule reports `not_applicable` with the note
*"no diabetes diagnosis found in your connected records"*. Changed by council
ruling D14 from *"applies to patients with a diabetes diagnosis"*, which read,
beside a screening that had been set aside, as a finding that the person does
not have diabetes. The gate established no such thing — it saw the Conditions
that were shared.

**For the reviewer:** PRD 5 records this rule as patient-visible with no
clinician having passed on its 6-month cadence (#389). It is the rule this
register most needs an initial against.

---

## 4. `colorectal-screening` — Colorectal cancer screening

| Field | As encoded |
| --- | --- |
| Population | Any sex, ages 45–75 |
| Cadence | Every 120 months (colonoscopy interval, taken as a conservative upper bound) |
| Source | `uspstf`. Guideline year: **not encoded** |
| What closes the gap | `Procedure` with code `45378`, `45380`, `45385`, `44388` or `45330` |
| What is NOT read | **stool-based tests (FIT or Cologuard)** — accepted by USPSTF on their own schedules, and arriving as lab `Observation`s rather than `Procedure`s. This rule reads neither |
| Related eCQM | CMS130 (related, not implemented) |
| Status | **indeterminate by design** |

Because a whole class of qualifying evidence is invisible to it, this rule never
reports "due". A patient screening exactly as advised with annual FIT would
have matched nothing and been told they were overdue. Instead it reports
`indeterminate` and says what was not read, and the patient still gets a line
telling them to raise it with their clinician (#425, #428, #436).

---

## 5. `cervical-screening` — Cervical cancer screening (Pap)

| Field | As encoded |
| --- | --- |
| Population | **Female**, ages 21–65 |
| Cadence | Every 36 months |
| Source | `uspstf`. Guideline year: **not encoded** |
| What closes the gap | `Procedure` with code `88175`, `88164` or `88142` |
| What is NOT read | Which test was done. The 36-month interval is applied to any match, so cytology and co-testing are not distinguished, and hysterectomy history is not read at all |
| Related eCQM | CMS124 (related, not implemented) |
| Status | released |

---

## 6. `mammography` — Breast cancer screening (mammogram)

| Field | As encoded |
| --- | --- |
| Population | **Female**, ages 40–74 |
| Cadence | Every 24 months |
| Source | `uspstf`. Guideline year: **not encoded** |
| What closes the gap | `Procedure` with code `77067`, `77066` or `77065` |
| What is NOT read | Personal or family history, prior abnormal results, or breast density — none of which the rule reads, so the interval does not shorten for anyone |
| Related eCQM | CMS125 (related, not implemented) |
| Status | released |

---

## 7. `flu-immunization` — Influenza (flu) vaccine

| Field | As encoded |
| --- | --- |
| Population | Any sex, ages 18–120 |
| Cadence | Every 12 months |
| Source | `acip` — "CDC/ACIP adult immunization schedule." Schedule year: **not encoded** |
| What closes the gap | `Immunization` with code `88`, `140`, `141`, `150`, `158`, `161` or `171` |
| What is NOT read | The influenza season. The window is 12 rolling months from the last dose, not a season boundary, so someone vaccinated in one season reads as covered part-way into the next |
| Related eCQM | CMS147 (related, not implemented) |
| Status | released |

---

## Applies to every rule

These are properties of the evaluator, not of any one rule, and each changes
what a verdict above means.

**Code matching**

- A code matches on its **value alone** — `code.coding[].code`. The code system
  is never compared, so an identical numeric code from a different system would
  match. Encoded as such; there is no allowlist of systems.

**Dates**

- A record's date is read from the first of `effectiveDateTime`,
  `performedDateTime`, `occurrenceDateTime`, `authoredOn`. A record carrying
  none of these has **no date and cannot close a gap**.
- **Future-dated records never close a gap**, so bad source data cannot produce
  a false "up to date".
- A matching record **older than the cadence** is treated exactly as if it were
  absent.
- A partial `birthDate` is padded to the earliest day: `YYYY` becomes 1 January,
  `YYYY-MM` becomes the 1st, which carries **up to a year of age error** at band
  boundaries. How often that happens depends on the source, not on us: this
  check reads the stored Patient directly (`r6/caregaps/routes.py:106-109`), so
  the redaction that truncates birth dates elsewhere does not apply here. A
  partial date reaches this rule only if the record arrived that way.

**Resource status**

- The `status` of a closing record is **never checked**. A `Procedure`,
  `Observation` or `Immunization` marked `entered-in-error` closes the gap. This
  applies to all seven rules.
- Soft-deleted records are excluded (#422).

**When something is unknown**

- Unknown age on an age-gated rule → `indeterminate`, never a false "due".
  On a rule whose cadence is banded by age, that row also carries **no
  cadence at all** rather than the rule's default, because the default is
  itself one band's figure — for `bp-screening`, the 40-and-over one. A row
  that has said it does not know the person's age states no interval that
  depends on it (#616), and the range covering both bands is not offered
  either: it is still an answer to the question the row has just declined.
  **`diabetes-a1c` is the exception**: its diagnosis gate runs *before* the age
  gate, so with no date of birth on file it reports `not_applicable` — "no
  diabetes diagnosis found in your connected records" — rather than declining
  to decide. The sentence is true of the records that were read, but the rule
  reaches it without ever establishing the person's age.
- A rule with a sex requirement and no sex on file → `indeterminate`.
- A patient the operation could not resolve at all → **no rules are evaluated**,
  the result set is empty, and the audit records `evaluated=0` (#542). Nothing
  in this register is asserted about someone who was never identified.

**What the patient is told**

- "Due" means **no satisfying record was found in the connected data**. It is
  not a claim the screening was not done elsewhere, and the consumer wording
  says so.
- Every screening the patient is eligible for gets a patient-facing line,
  including one that could not be decided — labelled *"could not check"* rather
  than folded in with the due items (#436).
- Cadences are **population-level adult defaults**. Individual risk — family
  history, prior abnormal results, pregnancy — legitimately changes them, and
  nothing in this engine reads any of it.

**What this is not**

- Not the Da Vinci DEQM `$care-gaps` operation, and not a certified eCQM. The
  per-rule `related_ecqm` ids are for reconciling against a certified measure
  engine and are **not** a claim that this rule implements that measure's logic.

---

## Known gaps in this register

1. **No guideline year is encoded for any rule.** `REFERENCES` names the
   organisation and nothing more. A reviewer cannot tell which revision of a
   recommendation a cadence came from without reading the commit that set it.
2. **`diabetes-a1c` is patient-visible and has never been clinically
   reviewed** (#389, PRD 5 §3).
3. **Resource `status` is ignored on every closing record**, so an
   `entered-in-error` result can report a patient as up to date.
4. **`diabetes-a1c` decides before it knows the patient's age.** The diagnosis
   gate precedes the age gate, so an unknown date of birth yields
   `not_applicable` rather than `indeterminate`. `Patient/$care-gaps` no longer
   reaches this — since #542 it evaluates no rules at all for a subject it
   could not resolve — but the appointment brief calls the evaluator directly
   with no patient, and lands here every time.

---

## Sign-off

Signing this page means: *these rules, these populations, these cadences, and
these disclosed limits are what I am willing to have shown to a patient.*

A rule marked `indeterminate by design` is asserting nothing about the patient
and is released on that basis.

**Reviewing clinician**

- Name: ______________________________
- Role / credential: ______________________________
- Date: ______________________________
- Rules released as written: ______________________________
- Rules held back, and why: ______________________________
- Signature: ______________________________

**Engineer**

- Name: ______________________________
- Date: ______________________________
- Confirms the values on this page were transcribed from
  `r6/caregaps/evaluate.py`, that the cadences and bands are pinned by
  `tests/test_caregaps_evaluate.py`, and that this page is held to those
  values by `tests/test_care_gap_register_drift.py`:
  ______________________________
- Signature: ______________________________
