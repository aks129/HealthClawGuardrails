# Remove the person's name before a model reads the record

- Status: design, not built. Draft for review.
- Date: 2026-11-24
- Scope: the engine's standard redaction profile, the two SDC API routes
  (`$populate`, `$extract`), and the copy that describes them.
- Requirement (owner, 2026-11-24): "all data is de-identified by default
  before it goes to the model."

## 0. Decision

1. The standard profile removes every `name` element, at any depth, of any
   type. It does not keep initials and it does not put a placeholder in their
   place. The stable, non-identifying handle for a person is the resource id
   that already exists (`Patient/<id>`).
2. The change goes in the one redaction walker, `r6/redaction.py`. Every
   engine route in §5 that returns record content to an agent already calls
   it. No new `Profile` member, no model-only layer, no prompt rule.
3. The same walker closes four leaks measured on `main` today. Each one
   passes a full identifier to every agent: a name in `valueHumanName`, a
   street address in `valueAddress`, a phone number in `valueContactPoint`,
   and home coordinates in an Address `geolocation` extension (§2.2).
4. The two SDC routes stop returning identity over HTTP.
   `Questionnaire/$populate` returns the stored full name, date of birth,
   phone, email and street address today, and the MCP
   `questionnaire_populate` tool hands that answer to the model.
   `QuestionnaireResponse/{id}/$extract?dryRun=true` returns a stored
   response's answers that a plain read of the same response hides (§2.2).
   The form-fill action keeps the real name. It populates in process for the
   human review page and the PDF, and the model never sees those answers.
5. A birth year that means 90 or older is removed, as is an Age over 89.
6. The result is still **not HIPAA Safe Harbor**, because dates of care and
   resource ids stay (§2). The copy must not say "de-identified".
   §1 has the phrase to use.

## 1. Term and copy

"De-identified" already names two other things in this repo: the
`$deidentify` preview (`r6/health_compliance.py:172`) and the `deidentified`
share profile. Neither is the standard profile. HIPAA also gives the word a
legal meaning that the standard profile does not meet (§2.3).

The term for what this design delivers is **identifiers removed**. Proposed
product copy, for an owner ruling:

> Before your records reach the AI model, we remove your name, contact
> details, street address and ID numbers. We keep the dates of your care and
> your state, so it can follow trends. This is not HIPAA de-identification.
> What you type in the chat reaches the model as you wrote it.

The current claim "Agents work with de-identified data by default"
(`templates/index.html:156`) is false today and stays false after this
change. PR 5 replaces it.

## 2. What reaches a model, category by category

"Today" is measured on `main` at `db772435` by running `apply_redaction` on
synthetic canary records. "Proposed" is the state after PRs 1 to 4.
"Met" means the category is removed from the standard profile for the
supported resource types (`r6/models.py:92`). It is not a legal finding.

### 2.1 Table

| # | Category (45 CFR 164.514(b)(2)(i)) | Today | Proposed | Met |
|---|---|---|---|---|
| A | Names (the person, relatives, household) | First initial kept on every `name`: `Patient.name`, `RelatedPerson.name`, `Patient.contact[].name`, `FamilyMemberHistory.name`, `CareTeam.name`, a home `Location.name`. **Full name kept** in `valueHumanName`. | Every `name` removed (§3 N1, N2). | Yes |
| B | Geography smaller than a state | `line`, `city`, `district`, `postalCode`, `text` removed from `address`. **Kept in full**: `valueAddress` (for example `patient-birthPlace`), Address `geolocation` latitude and longitude, `Location.position`. | `valueAddress` gets the address rule. Every Address `extension` removed. `Location.position` removed (§3 G1 to G3). State and country stay. | Yes |
| C | Dates, except year, tied to the person. Ages over 89. | `birthDate` and `deceasedDateTime` cut to the year. **Every date of care kept in full** (`effectiveDateTime`, `onset*`, `period`, `issued`, `recorded`, `authoredOn`, `occurrence*`). A birth year that means 90+ is kept (1931 survives). An Age over 89 is kept (`onsetAge` 93 survives). A relative's `bornDate` and `deceasedDate` are kept in full. | Birth year removed at 90+. Relatives' dates cut to the year. An Age over 89 becomes `{"value": 90, "comparator": ">="}` (§3 A1, A2). **Dates of care stay.** | **No** |
| D | Telephone numbers | `telecom[].value` and `contact[].telecom[].value` replaced. **Kept** in `valueContactPoint`. | `valueContactPoint` gets the telecom rule (§3 T1). | Yes |
| E | Fax numbers | As D (a ContactPoint with `system: fax`). | As D. | Yes |
| F | Email addresses | As D. | As D. | Yes |
| G | Social Security numbers | Identifier `value` removed under every key in `_IDENTIFIER_KEYS` (`r6/redaction.py:233`). | No change. | Yes |
| H | Medical record numbers | As G. | No change. | Yes |
| I | Health plan beneficiary numbers | As G, plus Coverage `subscriberId`, `dependent`, `class.value`. | No change. | Yes |
| J | Account numbers | As G. | No change. | Yes |
| K | Certificate and licence numbers | As G, at any depth (`Practitioner.qualification.identifier`). | No change. | Yes |
| L | Vehicle identifiers and licence plates | No element in the supported types. Free text removed. | No change. | Yes, by construction |
| M | Device identifiers and serial numbers | `Device.identifier` value removed. **Kept**: `serialNumber`, `distinctIdentifier`, `udiCarrier.carrierHRF`, `udiCarrier.carrierAIDC`. Device is not a stored type (`r6/fasten/ingester.py:101`) but can arrive `contained`. | Those four keys removed at any depth (§3 D1). `lotNumber` stays: a lot is not one person's. | Yes |
| N | Web URLs | `valueUrl`, `valueUri`, `valueCanonical`, Attachment `url`, `meta.source` removed. **Kept**: an absolute `Reference.reference`, which can name the upstream host. | No change. Follow-up issue. | Partly |
| O | IP addresses | AuditEvent `agent.network` address removed. | No change. | Yes |
| P | Biometric identifiers | No element in the supported types. Attachments emptied. | No change. | Yes, by construction |
| Q | Full-face photographs | `photo` removed. Attachment `data`, `url`, `title` removed. | No change. | Yes |
| R | Any other unique number or code | Free text removed. **Kept**: `valueUuid`, `valueExpression`, resource ids and the ids inside references. Ingest keeps the upstream id (`r6/fasten/ingester.py:493`), so a Patient id can be the EHR's own id for that person. | `valueUuid` and `valueExpression` removed (§3 X1). Ids unchanged. Follow-up issue. | **No** |

### 2.2 Measured leaks this design closes

Run on `main`, synthetic values, `apply_redaction` output:

- `Patient.extension[].valueHumanName` returned `{"family": "Canarypartner",
  "given": ["Hn"]}` in full.
- `Patient.extension[].valueAddress` returned the street, city and postal
  code in full.
- `Patient.extension[].valueContactPoint` returned the phone number in full.
- `Patient.address[].extension` (geolocation) returned latitude and longitude
  to five decimal places, which locates the home to about one metre.
- `birthDate: "1931-04-05"` returned `"1931"`, an age of 95.
- A contained Device returned `serialNumber` and a `carrierHRF` holding the
  serial.
- A string `name` keeps its first letter at any depth. `CareTeam.name`
  ("Zzyzxbarton family care team"), a `Location.name` for the person's home,
  a `contact[].name` written as a string and `valueContactDetail.name` all
  returned `"Z."` or `"C."`.
- `FamilyMemberHistory.bornDate` and `deceasedDate` returned in full.
  `deceasedAge` 95, `ageAge` 93, `performedAge` 92 and `valueAge` 96 returned
  unchanged.
- `valueExpression.expression` and `valueUuid` returned in full.
- **A read-tier bypass on stored form answers.** A stored, subject-less
  QuestionnaireResponse whose answers are a family name and a birth date:
  `GET /QuestionnaireResponse/{id}` hides both. `POST
  /QuestionnaireResponse/{id}/$extract?dryRun=true` with no step-up token
  returns both in full (`r6/sdc/routes.py:118-119`, `:201-204`). A response
  bound to a subject yields no Patient row (#572,
  `r6/sdc/extract.py:99-111`), so the form-fill path does not produce this
  shape. Observation-extract answers on a stored response are echoed the
  same way.

These pass through every agent read today, MCP tools included. They are
higher priority than the initials, so PRs 1 to 3 carry them.

### 2.3 Why this is not Safe Harbor

Safe Harbor needs C and R removed and no actual knowledge that what is left
could identify the person. We keep dates of care because trends, "your last
A1c", and care-gap timing are what the product does. We keep resource ids
because every tool call names records by id. Shifting dates or re-keying ids
is a separate design with its own costs, and it is not proposed here.

What is left is close to a HIPAA limited data set (it also removes city and
ZIP code). A limited data set is a legal arrangement with a data use
agreement. We do not claim that either.

## 3. Field rules for the standard profile

All of these are in the walker (`_redact_fields` / `_redact_recursive`,
`r6/redaction.py:63` and `:372`). They apply at every depth, so contained
resources, Bundle entries and extensions get them too.

- **N1.** Every `name` key is removed, at any depth, whatever its value.
  That covers `HumanName[]` (Patient, RelatedPerson, Practitioner, Person),
  `contact[].name`, `FamilyMemberHistory.name`, `CareTeam.name`,
  `Location.name`, `Organization.name`, `ContactDetail.name` and
  `Device.deviceName[].name`. It replaces three rules:
  `r6/redaction.py:65-77`, `:143-152` and `:416-418`. One rule with no type
  list is the point. Business names lose an initial that told a reader
  nothing, and a business name can carry a person's name ("Dr. Zzyzxbarton
  Family Practice").
- **N2.** `valueHumanName` is removed. Add it to `_FREE_TEXT_KEYS`
  (`r6/redaction.py:216`).
- **G1.** `valueAddress` gets the Address rule: `line`, `text`, `city`,
  `district`, `postalCode` removed, and `state` and `country` kept.
- **G2.** Every Address loses its `extension`. This covers geolocation and
  any other extension an upstream hangs on an address, with no URL list to
  maintain.
- **G3.** `Location.position` is removed. A Location can be the person's home
  (`mode: instance`).
- **T1.** `valueContactPoint.value` becomes `[Redacted]`, as `telecom` does.
- **D1.** `serialNumber`, `distinctIdentifier`, `carrierHRF` and `carrierAIDC`
  are removed at any depth.
- **X1.** `valueExpression` and `valueUuid` are removed. Add both to
  `_FREE_TEXT_KEYS`.
- **A1.** `birthDate` is cut to the year, as today. It is removed when the
  current year minus the birth year is 90 or more. Year arithmetic can only
  err toward removal. The rule reads the clock, so tests compute years from
  today, never from a fixed date (#1084). `FamilyMemberHistory.bornDate` and
  `deceasedDate` join `_DATE_KEYS` (`r6/redaction.py:227`) and are cut to the
  year. The regulation's list counts relatives' dates too.
- **A2.** Any key ending in `Age` whose value is a Quantity in years
  (`onsetAge`, `abatementAge`, `ageAge`, `deceasedAge`, `performedAge`,
  `valueAge`) with a value over 89 becomes `{"value": 90, "comparator": ">=",
  "unit", "system", "code"}`. This is the aggregation the `$deidentify`
  preview uses (`r6/health_compliance.py:217`). An Age in other units is left
  alone.

Not changed, on purpose:

- `gender`, race, ethnicity, language and marital status stay. They are not
  Safe Harbor identifiers and the model uses them.
- The relabel by code (`label_codings`, `r6/redaction.py:55`) still runs
  after the strip. Nothing here preserves an upstream `display` or `text`.

## 4. Where the change goes

**In the walker, `r6/redaction.py`.** The engine cannot tell a model from any
other caller. The standard profile is by definition what an agent reads.
Every route in §5 that returns record content to an agent calls
`apply_redaction`, either directly (17 call sites in `r6/routes.py`, plus
`r6/labs/routes.py:171`, `r6/brief/routes.py:58`) or through
`fhir_response(profile=STANDARD)` (`r6/access.py:1859`). One change covers
all of them.

The other routes in `app.url_map` were classified by reading, not measured.
They return no record content (OAuth, ops, metadata, audit, agent-run
bookkeeping, Fasten job status, wearables sync), or they serve a human (the
filled PDF at `/r6/sdc/documents/{id}`, the review page), or they are
aggregates (`$stats`). `$evaluate-measure`, `$profile-adherence`,
`$inventory` and `/r6/smbp/report/{id}` go into PR 1's canary sweep so the
classification becomes a measurement.

Rejected:

- **At `fhir_response` in `r6/access.py`.** It would miss every route that
  has not adopted the kernel yet, and most have not.
- **A new `Profile.MODEL`.** It adds a state, and a route author would have to
  choose correctly between STANDARD and MODEL on every new route. The default
  would still leak.
- **In the consumer app's prompts or model seam.** It would cover one of
  three surfaces. The MCP surface and the engine's own agent client would
  stay open.

The identified profiles do not change. INTAKE, CHECKIN, KTC and
PATIENT_CONTROLLED rebuild the top-level identity from the stored original
(`r6/access.py:1211-1215`, `:1506`), so removing names in the walker does not
alter what a clinic receives. Their nested resources lose initials, which is
an improvement.

The two SDC routes answer around the walker. `$populate` populates from the
unredacted subject on purpose (`r6/sdc/routes.py:76`, `:100`, `:217-224`).
`$extract` returns its bundle through `jsonify` with no profile
(`r6/sdc/routes.py:201-204`). PR 3 changes both.

## 5. Every path from a record to a model

| Surface | Engine route that feeds it | Redaction | After this design |
|---|---|---|---|
| MCP `fhir_read`, `fetch` (`services/agent-orchestrator/src/tools.ts:1439`, `:2504`) | `GET /{type}/{id}`, `r6/routes.py:448` (`:491`, `:513`) | STANDARD | Covered by PRs 1, 2, 4 |
| MCP `fhir_search`, `search` (`tools.ts:1454`, `:2477`) | `GET /{type}`, `r6/routes.py:672` (`:769`, `:962`) | STANDARD | Covered |
| MCP `context_get` (`tools.ts:1430`) | `GET /context/{id}`, `r6/routes.py:1102` (`:1148`) | STANDARD | Covered |
| MCP `fhir_lastn` (`tools.ts:1675`) | `r6/routes.py:1413` (`:1464`) | STANDARD | Covered |
| MCP `fhir_stats` (`tools.ts:1632`) | `r6/routes.py:1329` | Aggregates only | No identity |
| MCP `fhir_compiled_truth` (`tools.ts:1928`) | `r6/routes.py:3026` (`:3079`, `:3094`) | STANDARD | Covered |
| MCP `curatr_apply_fix` | `r6/routes.py:2889` (`:3018`) | STANDARD | Covered |
| MCP `curatr_evaluate` (`tools.ts:2065`) | `r6/routes.py:2832`, evaluates the unredacted row | Issues text only | **Not verified in this pass.** Issue text is built from codes. Check in PR 1 review. |
| MCP `fhir_interpret_labs` (`tools.ts:1981`) | `Observation/$interpret`, `r6/labs/routes.py:141` (`:171`) | STANDARD, labels from `LOINC_RANGES` | No identity |
| MCP `care_gaps` (`tools.ts:2017`) | `Patient/$care-gaps`, `r6/caregaps/routes.py:270` | Reads the stored, unredacted Patient for age and sex (`patient_for`, `r6/caregaps/routes.py:81-94`). Returns no demographics. | No identity. A1 does not change its output. |
| MCP `fhir_propose_write`, `fhir_commit_write` | POST and PUT echo, `r6/routes.py:401`, `:439`, `:585`, `:634` | STANDARD | Covered |
| MCP `questionnaire_populate` (`tools.ts:537`, `:1715`) | `Questionnaire/$populate`, `r6/sdc/routes.py:76`, `:100` | **Unredacted subject projection** (name, birthDate, telecom, address) | **Identified today.** PR 3. |
| MCP `questionnaire_extract` (`tools.ts:561`, `:1751`) | `QuestionnaireResponse/$extract`, `r6/sdc/routes.py:105` | The tool sends an inline response, so it echoes the model's own input | No new identity through the tool. The `{id}/$extract` route is open to any read-tier caller and echoes **stored** answers (§2.2). PR 3. |
| MCP `shl_generate` (`tools.ts:2303`) | `$share-bundle`, `r6/routes.py:3275` | INTAKE or patient-controlled | Returns links only (`tools.ts:2408-2415`). The bundle never enters the model's context. The link is a bearer credential to an identified record, by design and with the person's consent. |
| MCP `action_status` (`tools.ts:1124`) | action rail status | Outcome holds ids, not field values (`r6/actions/rails/form_fill.py:115`) | No identity |
| Engine agent client (`r6/agent_client.py:121`, `:160`) | The same routes, through MCP (`:295`) or directly (`:361`) | STANDARD | Covered |
| Visit brief | `r6/brief/routes.py:58` | STANDARD, labels by code | No name today. Covered. |
| A consumer app's chat agent | Search and read, `$interpret`, `$care-gaps`, the brief, action status | STANDARD | Covered by the walker. Its search tool offers no Patient type, so it reads no Patient today. Its prompt names the person by relationship only. |
| Form fill (action rail) | In-process populate, `r6/actions/review.py:320`. PDF title from the stored name, `r6/actions/rails/form_fill.py:147-175`. | Identified, for the human | **Not a model exit.** The model gets status and a link. Unchanged. |
| Check-in wallet, KTC share | `_checkin_profile`, `_ktc_profile`, `r6/access.py:1701`, `:1815` | Identified, for a clinic | Not a model exit. Unchanged. |
| Delegated call | `r6/actions/playbooks/render.py:232-252` (#1137) | `preferred_name` is a preamble fact (`r6/actions/playbooks/__init__.py:53`). No builder calls the renderer yet. | **When the builder lands, a voice agent receives a name by design**, in a script the person approves before it is spoken. A named exception to the default. Requirement on that builder: `preferred_name` comes from the person, never from `Patient.name`. |
| Prescription transfer (`tools.ts:1083`) | `r6/actions/rx_transfer.py` | Medicine names from `r6/terminology.py` by code. Pharmacy names and numbers are supplied by the model. | No patient name. Unchanged. |
| The person's own messages | n/a | none | Reach the model verbatim. Out of scope. The copy says so. |

Search does not give a model a name oracle. The engine honours only
`patient`, `code`, `status`, `_lastUpdated`, `_count`, `_sort`, `_summary`,
`context-id` and `_id` (`r6/search_fidelity.py:40-56`). It ignores `name`,
`birthdate`, `identifier`, `telecom` and `address`, with a warning. The MCP
`search` description (`tools.ts:1164`) offers `Patient?name=smith` as an
example. That is misleading, and PR 5 corrects it.

## 6. What breaks

Measured, not only predicted. A scratch patch of N1 (every `name` key), the
`contact[].name` removal and A1 was run against the full Python suite on
`db772435` with `PYTHONDONTWRITEBYTECODE=1`, then reverted: 13374 passed and
6 failed. The 6 are exactly the initial-pinning tests listed below.
`tests/test_guardrail_conformance.py` passed, so Grade A held. The MCP
server's TypeScript tests were not run against the patch.

- **Tests that pin initials.** Six tests in five files failed under the
  scratch patch and must assert absence instead:
  `tests/test_redaction_identifiers.py:160`,
  `tests/test_healthclaw_redact.py:338-339`,
  `tests/test_recursive_redaction.py:148-149` and `:175`,
  `tests/test_record_source_fhi42.py:497`, `tests/test_medplum_in_front.py:45`.
- **`$populate` identity answers.** `tests/test_sdc_populate_bounded.py:366`
  pins a populated given name over HTTP. After PR 3 that leaf is "not
  populated" over HTTP. The in-process form fill keeps it, and a new test pins
  that.
- **The control panel.** `static/js/fhir-control-panel.js:225-230` labels a
  Patient row by its name. After PR 1 it falls through to an empty label.
  Fall back to the resource type and id.
- **The connector title.** `tools.ts:2422-2440` titles a Patient by name.
  It falls back to the id already. Pin that with a test.
- **The demo.** `/demo/agent-loop` (`r6/routes.py:2536`, `:2602`) shows a
  redacted Patient. It now shows no name. Any narration that says "initials"
  changes. The recorded demo video (`static/videos/healthclaw-2.0-demo.vtt`)
  cannot be edited. Note it in the release notes.
- **Docs that say "initials".** `README.md:145`, `:166`, `:742`,
  `SECURITY.md:20`, `templates/faq.html:127`, `templates/wiki.html:210`,
  `templates/privacy.html:102`, `:152`, and the module docstring at
  `r6/redaction.py:7`. Each changes in the PR that changes the behaviour.
  `templates/faq.html:127` and `templates/wiki.html:210` show "M. E. Rivera",
  a full family name that the code has never returned. Fix those in PR 1.
- **Greetings.** None found that read a name from the record. The consumer
  app greets with the agent's own name and the person's relationship.
- **Conformance.** The PHI redaction probe checks that the full family and
  given names are absent (`r6/conformance/probes.py:630-631`, `:655-656`).
  Removal still passes. The record-is-present and terminology-label checks
  still pass. Grade A holds with no harness change. Do not tighten the
  harness here. It grades partner deployments, and a stricter row would
  regrade them (owner ruling, §10).
- **Clinical features.** Care gaps and the brief's care-gap section read the
  stored, unredacted Patient (`r6/caregaps/routes.py:81-94`,
  `r6/brief/routes.py:131-133`), so A1 does not change which screenings fire.
  `$interpret` and the trend line read stored Observations. None reads a
  name. A 90+ person's birth year no longer reaches an agent that reads the
  Patient directly.

## 7. PR split

Each PR is one concern and under 400 lines. Tests come first in each PR and
must fail on `main` before the fix.

### PR 1: names (walker)

N1 and N2, the docs that name initials, and the control-panel fallback.

Tests first:

- A canary sweep: one synthetic Patient carrying a unique name in every
  shape (`name`, `contact[].name` as an object and as a string,
  `valueHumanName`, `valueContactDetail.name`, `name.prefix`, `suffix`,
  `_given` extensions, a contained RelatedPerson and Practitioner, a
  FamilyMemberHistory, a CareTeam, a home Location). Read it through
  `GET /{type}/{id}`, search, `/context/{id}`, `$lastn`, `$compiled-truth`,
  the write echo, `$evaluate-measure`, `$profile-adherence`, `$inventory` and
  the SMBP report. Assert no canary substring and no `name` key anywhere.
- Two-sided: the same responses still contain the record, its coded
  `gender`, and a terminology-derived label on a coded child (as the
  conformance label check does). A redactor that deletes everything fails.
- `_intake_profile`, `_checkin_profile` and patient-controlled output for the
  same Patient are unchanged at the top level.

Mutation targets (each must turn a test red):

- Restore the initial rule for a list-valued `name`.
- Restore the initial rule for a string `name`.
- Skip `contact[].name`.
- Remove `valueHumanName` from `_FREE_TEXT_KEYS`.
- Apply N1 only where `resourceType` is present, so contained names survive.

### PR 2: contact and place in extensions, device serials

G1 to G3, T1, D1, X1.

Tests first: canaries in `valueAddress`, `valueContactPoint`, Address
`geolocation`, `Location.position`, a contained Device with `serialNumber`
and `carrierHRF`. Two-sided: `state` survives in both `address` and
`valueAddress`, and `lotNumber` survives on an Immunization.

Mutation targets: remove the `valueAddress` branch, keep Address
`extension`, drop `serialNumber` from the key set, replace the T1 value with
the original.

### PR 3: the SDC routes answer without identity

`$populate` over HTTP passes `populate_questionnaire` a subject with `name`,
`telecom` and `address` removed and `birthDate` under A1. `$extract` runs
every row of a bundle built from a **stored** response through
`apply_redaction` before it answers. An inline response is the caller's own
data and is echoed as today. The in-process form fill
(`r6/actions/review.py:320`) is unchanged.

Tests first:

- HTTP `$populate` with the stock intake Questionnaire on a canary Patient:
  no canary name, phone, email, street, city or postal code, and no full
  birth date, anywhere in the Parameters body. Identity leaves are reported
  "not populated".
- Two-sided: a clinical leaf (an allergy or a medication) still populates.
- The form-fill review for the same Patient still shows the real name. This
  test pins that the human path keeps identity, so a change that strips
  both paths fails.
- `{id}/$extract?dryRun=true` on the stored, subject-less response from
  §2.2: no canary name or full birth date. Two-sided: the bundle still holds
  the extracted rows, and an inline response still previews in full.

Mutation targets: pass the stored subject to the HTTP route, pass the
identity-free subject to the review path, and skip redaction on the
stored-response branch of `$extract`. Each must fail one test.

### PR 4: ages 90 and over, relatives' dates

A1 and A2.

Tests first, with years computed from today: birth year at today minus 89
is kept, at today minus 90 is removed. An `onsetAge` of 89 is kept, 90
becomes `>= 90`, and so do `deceasedAge` and `performedAge`. An Age in
months is unchanged. `FamilyMemberHistory.bornDate` comes back as a year. Two-sided: a Patient with a
removed birth date is still returned with its other fields.

Mutation targets: `>= 90` changed to `> 90`, the A2 comparator dropped,
the A2 unit check removed, A2 limited to `onsetAge`.

### PR 5: the copy

The phrase from §1 on `templates/index.html:156` and the privacy page, the
MCP `search` description example, and a CHANGELOG entry. No behaviour change.
This PR waits for the owner's ruling on the phrase.

### Order and running the mutations

PRs 1, 2 and 4 all edit `r6/redaction.py`, so they land one after another.
PR 3 is independent and can run in parallel. PR 5 lands last.

Run each mutation from a commit, with `PYTHONDONTWRITEBYTECODE=1` and a
purged `__pycache__`, and assert the replacement count. These are the
traps in `docs/agent-task-guide.md` §6.

## 8. #964 and #1022

**#964** (intake share bundle copies nested `display` and `text` inside
Patient name, address and telecom) is independent. Its fix is open as a
draft (#1162) and touches `r6/access.py` and `r6/routes.py`, not
`r6/redaction.py`. `_intake_profile` and
`_intake_strip` copy those four fields from the stored original after
`apply_redaction` (`r6/access.py:1211-1215`, `r6/routes.py:3259`), so this
design does not change what they emit at the top level. The intake bundle
reaches a clinic, not a model (`tools.ts:2408-2415`). Do not fold it in. Land
it on its own, in either order, reusing the `_patient_identity` allow-list
(`r6/access.py:1506`).

**#1022** (a non-string unit is dropped) is a unit rule in the same file and
has nothing to do with names. Keep it separate. If it is in flight at the
same time as PR 1, rebase rather than combine.

## 9. Out of scope, with follow-up issues to file

- Dates of care (category C). Date shifting is a separate design.
- Upstream resource ids kept on ingest (category R).
- An absolute `Reference.reference` that names the upstream host
  (category N).
- Tightening the conformance PHI probe to "no `name` element" (owner ruling).
- `curatr_evaluate` issue text, not verified in this pass (§5).
- The long-term shape is an allow-list per profile, which #112 named and
  closed. This design extends the deny-list in the walker because it is the
  smaller change that closes measured leaks now. Every new `value[x]` type
  or element FHIR adds is a deny-list gap until then, and §2.2 found six.
- A QuestionnaireResponse answer typed as `valueInteger` or `valueDecimal`
  is kept. A person could type a phone number there.

## 10. Owner rulings needed

1. **Copy phrase.** Approve §1's wording, or another that does not say
   "de-identified".
2. **Birth year.** Keep the year under 90 (this design) or move to an age
   band. A band needs an extension and adds a state. This design keeps the
   year.
3. **Whose names.** This design removes every `name` element: clinicians',
   care teams' and businesses' too. One rule with no type list is simpler,
   the initials carried no value, and a business name can carry a person's
   name. Confirm.
4. **The SDC routes over HTTP.** Confirm that no API caller needs
   `$populate` to pre-fill identity or `$extract` to return a stored
   response's answers unredacted. The form-fill action keeps identity.
5. **Conformance harness.** Whether to add a "no name element" check later,
   knowing it regrades partner deployments.
