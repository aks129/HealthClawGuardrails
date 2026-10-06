# Clinical knowledge architecture

Status: proposed, 2026-10-06. It comes from a research and architecture sprint. The sprint inventoried the clinical logic in the code, reviewed external best practice, and checked the roadmap and governance docs.

HealthClaw's clinical logic works, but it is hand-coded in about 20 places. Almost none of it is signed by a clinician, and in places it contradicts itself. This plan makes every number a patient sees come from one versioned, cited, clinician-signed rule. The agent only explains that result. The work comes in three steps, each tied to a beta stage. There is no big rewrite and no CQL engine yet.

## Owner decisions

These five need to be settled before stage 1 opens real records. None of them needs action while the beta runs on sample records.

| # | Decision | Recommendation | Needed before |
| --- | --- | --- | --- |
| 1 | Urgency wording a patient sees, such as "contact your doctor promptly", computed from our own thresholds | Use the lab's own flag as the trigger. Use three fixed urgency tiers, each signed. Get a regulatory counsel review. FDA's Jan 2026 CDS guidance exempts only software aimed at clinicians, so a time-critical alert to a patient is device-shaped. | Stage 1 |
| 2 | The FTC Health Breach Notification Rule, previously ruled "n/a" | Revisit with counsel. The 2024 amendment covers non-HIPAA health apps that draw from multiple sources. | Stage 1 |
| 3 | CPT codes in the care-gap rules | Match on CPT codes but never display CPT descriptors. Prefer LOINC, SNOMED and CVX evidence. Buy an AMA licence only if descriptors must be shown. | Stage 1 |
| 4 | A second clinical reviewer | Name the P1.4 recruit as a reviewer, so no clinical change waits on one person. | Stage 1 |
| 5 | A record that names no patient | Until our physician advisor rules, such a record keeps a gap open and never closes one. | Now |

## What the inventory found

Defects, filed as issues:

- #890: blood pressure crisis guidance disagrees across `r6/smbp/triage.py`, the skill, and `r6/smbp/content.py`. There are four red-flag symptom lists.
- #891: the visit brief reads problems, medications, labs and visits tenant-wide.
- #892: chat passes the free-text lab unit to the model.
- #893: quality and SMBP match the patient by exact string. SMBP adherence counts every Observation. A year-only birth date drops the patient from the measure.
- #894: analyte names come from three tables, and code comments contradict the label table.

Structural problems:

- The status filter is inconsistent.
- Date handling is inconsistent: timezones, string comparison, month counting.
- Unit handling is inconsistent.
- Some clinical logic lives only in prompts and skills.
- Quality and SMBP cite no sources.
- The only clinical register (care gaps) has never been signed.

## Principles

1. **The source wins.** The lab's own range and flag beat ours. Our ranges are a labelled fallback.
2. **Compute once, in code.** Numbers, flags, trends, gaps and urgency tiers are computed deterministically and stamped with the rule id and version. The model explains them. It never computes or infers.
3. **One definition per concept.** One unit layer, one patient matcher, one active-status set, one analyte table, one symptom list. Any copy that has to exist is pinned by a drift test.
4. **Honest absence.** When the system couldn't check, it says so. It never says "checked, none". An unrecognised unit, a missing patient or an unusable date gives no answer instead of a guess.
5. **No upstream free text.** Labels come from our terminology, looked up by code. Only allowlisted unit tokens are shown.
6. **Fixed words for urgency.** There are three signed templates. Code picks one. The model never rewrites it.
7. **Every rule is reviewable.** Each rule records its source and version, an owner, effective dates, boundary test cases, and a clinician signature on a commit.

## Target architecture

```
record feeds (lab range and flag kept)
  -> canonical facts (r6/clinical: units, patient matcher, status, dates; labels by code)
  -> versioned rules table (thresholds, windows, code lists, wording tier; source, tests, signature)
  -> stamped results (rule id + version; fixed urgency wording)
  -> surfaces: visit brief | agent chat and text (explains only, numbers checked) | FHIR operations
review loop: registers and sign-off (weekly batch, two reviewers) <- evals and incidents <- outcomes
```

## Standards

- **Terminology.** Keep a curated local subset of LOINC, SNOMED, CVX, RxNorm and ICD. Pull value sets from VSAC at build time against a pinned eCQM expansion profile, and commit them with their source and version. Check membership locally at runtime. tx.fhir.org is not for production. Add a terminology server later, only if mapping volume justifies it.
- **Units.** Show a unit only when it is an exact UCUM token on the allowlist (#884). Convert between molar and mass units per analyte, from a table keyed by LOINC. Never compare a value with a range in another unit. Compute trends only on series with the same specimen and the same unit. Next step: parse units with `ucumvert`/pint.
- **Rules.** Keep one declarative YAML rules file with a Python evaluator. Each row carries: id, version, status, effective_from/to, source and source_version, inputs, logic, wording_tier, test_cases, signed_by, signed_commit. Name each rule after the FHIR resource it would export to: rule to PlanDefinition, code list to ValueSet. CQL comes later. No mature Python engine exists, and we have about ten rules.
- **Wording.** Keep templates by tier at a 6th to 8th grade reading level, scored with the CDC Clear Communication Index (90 or above). When a flag comes from the lab, say "your lab marked this".
- **Agent contract.** Tools return structured facts with source ids, the rule id and version, and the wording tier. A post-generation check rejects any number not present in those facts. A set of 50 to 150 synthetic cases, labelled by a clinician and scored HealthBench-style, runs in CI on every prompt or model change.

## Governance

1. One register page per domain (labs, trends, care gaps, SMBP, quality, wording), generated from the rules file. Each row carries HTI-1-style source attributes. A drift test holds each page to the code.
2. Add a V7 clinical-correctness row to `docs/qa/sign-off-standard.md`. A change to any rule, threshold, code list or patient sentence needs a register diff and a clinician signature before it reaches real records. A change that only affects the sample beta may ship as `pending-signature`.
3. A weekly batch: "rows changed since last Tuesday", brought to the existing advisor meeting and signed against the commit SHA.
4. Two reviewers. Either can sign a row. Urgency tiers and new rules need both.
5. Change triggers: a source-version calendar opens review issues when USPSTF, ACIP, KDIGO, eCQM or VSAC publish updates. Retired rules keep their rows. Every output carries its rule id and version.
6. Clinical incidents (a wrong "due", a missed alert, a wrong unit) are logged, root-caused to a rule version, and turned into test cases.

## Plan

| Step | Gate | Work | Done when |
| --- | --- | --- | --- |
| Now | Before stage 1 | Fix #890–#894. Put shared helpers for units, the patient matcher, status and dates in `r6/clinical/`. Apply status filters everywhere. Write registers for labs, trends, SMBP, quality and wording. Add V7. Set the three urgency templates. Get counsel review of decisions 1 and 2. | Every patient-facing number traces to one helper, and every rule the stage 1 testers will see has a signed row |
| Next | Before stage 2 | Move to the YAML rules file and evaluator. Stamp results with rule id and version. Pull value sets from VSAC. Run the evals and the number-provenance check in CI. Parse units against UCUM. | No clinical constants are left in Python, and evals gate every model or prompt change |
| Later | When a partner or reporting requires it | Export PlanDefinition, Library and ValueSet. Run CMS165 in CQL through a sidecar. Add a terminology server. Set up a QMS if any feature becomes device-shaped. | A partner can load our rules |

#112 (terminology binding) and Workstream C (`to_canonical()`) fold into Now and Next. #53, #54 and #62 become rows in the rules file.

## Open clinical calls for our physician advisor

These are grouped by system. Each answer becomes a signed register row:
- **Urgency tiers:** the wording and timeframe for each tier, and the asymptomatic BP crisis call (#890).
- **Plausibility bounds:** for creatinine and for home BP (#883).
- **Flu gap:** the CVX set and the exclusions (#887).
- **Unnamed records:** whether a record with no patient named can count toward a gap.
- **Fallback ranges:** whether to show them, and their sources.
- **Lab additions:** new analytes and unit conversions (#53, #54).
- **Trends:** which analytes get trend rules (#62).
- **CMS165 exclusions:** the remaining exclusions (#55).
- **Care-gap register:** initialling the register, including the A1c rule.

## Sources

- FDA, Clinical Decision Support Software guidance (Jan 2026): https://www.fda.gov/regulatory-information/search-fda-guidance-documents/clinical-decision-support-software
- ONC HTI-5 proposed rule: https://www.federalregister.gov/documents/2025/12/29/2025-23896/health-data-technology-and-interoperability-astponc-deregulatory-actions-to-unleash-prosperity
- FTC Health Breach Notification Rule: https://www.ftc.gov/business-guidance/resources/complying-ftcs-health-breach-notification-rule-0
- CPG-on-FHIR: https://build.fhir.org/ig/HL7/cqf-recommendations/
- VSAC FHIR API: https://www.nlm.nih.gov/vsac/support/usingvsac/vsacfhirapi.html
- AMA CPT licensing: https://www.ama-assn.org/practice-management/cpt/cpt-coding-resources
- tx.fhir.org documentation: https://confluence.hl7.org/display/FHIR/tx.fhir.org+Documentation
- ucumvert: https://github.com/dalito/ucumvert
- CMS MADiE: https://ecqi.healthit.gov/tool/madie
- HealthBench: https://arxiv.org/abs/2505.08775
- CDC Clear Communication Index: https://www.cdc.gov/ccindex/tool/how-to-use.html
