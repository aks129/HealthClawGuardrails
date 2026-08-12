# Design note — guideline-grounded answers for a *diagnosed condition*

**Status: design only. No clinical thresholds ship until a clinician reviews
them**, the same gate `LOINC_RANGES` carries. Nothing here is implemented yet.

---

## The gap this closes

Two grounded engines already exist and both work well:

| engine | question it answers | citation mechanism |
|---|---|---|
| `r6/labs/interpret.py` | "is this **number** normal?" | `LOINC_RANGES[code]["source"]` → `REFERENCES`, test-enforced |
| `r6/caregaps/evaluate.py` | "what **screening** is due?" | `CARE_GAP_RULES[i]["source"]` → `REFERENCES`, test-enforced |

Neither answers **"I have this condition — now what?"** So when the agent named
hyperlipidemia on 2026-08-04, it had nothing guideline-backed to say and the
persona voice filled the silence:

> **Key Health Focus:** The most prominent item in your records is High
> Cholesterol. Knowing this is a big win — it's an "easy win" because it gives
> you and your doctor a clear focus for proactive heart health!

That is ungrounded. It is not *wrong*, exactly, which is what makes it
dangerous: it is fluent, confident, cites nothing, and would read identically
if the condition were something urgent. The failure is structural — no tool
returns condition-level guidance, so the model improvises — and the fix is
structural too. **Prompt-tuning cannot fix a missing data source.**

## The shape: a third engine, same pattern as the first two

`r6/conditions/guidance.py` — pure, no Flask, no DB, mirroring
`caregaps/evaluate.py` exactly so there is one idiom to learn:

```python
REFERENCES = {
    "acc-aha-2018": "2018 AHA/ACC/Multisociety Guideline on the Management "
                    "of Blood Cholesterol.",
    "uspstf-statin": "USPSTF: Statin Use for the Primary Prevention of "
                     "Cardiovascular Disease in Adults.",
    "ada-soc":       "American Diabetes Association Standards of Care.",
}

# Each entry:
#   codes: the code SET that maps to this concept, per system. A condition is
#     not one code — E78.5 and SNOMED 55822004 are the same clinical fact, and
#     matching one but not the other is the same class of failure as the
#     121-entry label table.
#   discuss: talking points for a CLINICIAN CONVERSATION. Never instructions.
#   monitoring: what is typically tracked, so the agent can say what to expect
#     rather than what to do.
#   source: key into REFERENCES. A test enforces presence, as in both engines.
CONDITION_GUIDANCE = [
    {
        "id": "hyperlipidemia",
        "codes": {"icd10": {"E78.5", "E78.0", "E78.2"},
                  "snomed": {"55822004", "13644009"}},
        "discuss": [...],      # pending clinical review
        "monitoring": [...],   # pending clinical review
        "source": "acc-aha-2018",
    },
]
```

Tool surface: one new agent tool, `get_condition_guidance`, returning
`{condition, discuss, monitoring, citation, disclaimer}`.

## The three properties it must not lose

These are not aspirations; each has an obvious test and each corresponds to a
mistake this codebase has already made.

**1. It cites or it does not speak.** A condition with no entry returns
*nothing* — the agent then says it has no guideline-backed information for it,
which is honest and true. The failure mode to prevent is a partially-covered
table making the covered conditions look like the important ones. That is
exactly what the label table did on 2026-08-04: the agent called hyperlipidemia
the patient's "key health focus" when it was merely the only translatable row.
**Coverage must never be mistaken for salience**, and the tool's own output has
to say so.

**2. It discusses; it never directs.** `SAFETY_CORE` already forbids stating a
diagnosis or treatment plan. This engine returns material for a *conversation
with a clinician* — "worth asking about", "typically monitored every N months"
— never "you should take" or "your target is". The distinction is enforceable
in tests as a banned-phrase list over the content, which is cheap and worth
having precisely because the content will be edited by humans later.

**3. Codes are matched as sets, per system.** A condition is not one code.
`E78.5` (ICD-10) and `55822004` (SNOMED) are the same clinical fact, and both
appear in the live record. Matching one but not the other reproduces the
label-table failure at a higher level.

## Scope discipline

Start with **three** conditions, all present in the live record and all
high-prevalence: hyperlipidemia, prediabetes, anxiety. Three is enough to prove
the engine, the tool wiring, and the citation test — and small enough for one
clinical review pass.

Explicitly **not** in scope: risk scoring (ASCVD and friends), anything
medication-specific, and anything that changes with acuity. Those need a
different safety argument than "here is what the guideline says to discuss",
and some need a device/CDS regulatory analysis this note does not attempt.

## Sequencing, and the honest reason for it

1. Engine + tests with **placeholder** content, so the structure can be
   reviewed independently of the clinical claims.
2. The clinical advisor fills `discuss` / `monitoring` for the three conditions and
   confirms the citations.
3. Wire the tool; add a shakeout row: *"what should I know about my
   cholesterol?"* → answer carries a citation, contains no directive phrasing.

Step 2 is a hard gate, not a formality. The whole value of this engine is that
its content is attributable to a named guideline and a named reviewer — an
LLM-drafted "clinical" table with a citation stapled on afterwards would be
strictly worse than the current fluent silence, because it would *look*
grounded. That is the one outcome this design exists to prevent.
