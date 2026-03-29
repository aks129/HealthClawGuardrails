# Skill Standards — Hypotheses (need more data)

## HYP-1: YAML block scalar (`>`) in `description` is parser-safe

**Hypothesis:** OpenClaw's "single-line keys" requirement applies to `metadata` specifically (where structured parsing is needed), not to `description`. YAML block scalars in `description` are rendered as a single string by the YAML parser and should work.

**Evidence for:** All four HealthClaw skills use `>` without issues in the Claude Code skill loader. OpenClaw docs show `metadata` specifically called out for single-line JSON.

**Evidence against:** The wording "Frontmatter must contain single-line keys only" is ambiguous — it could mean all values must be single-line.

**How to test:** Install a skill with `>` block scalar description in OpenClaw and observe whether it loads correctly and displays the full description.

**Confirmation count:** 0 / 5 needed for promotion to rule.

---

## HYP-2: `install` key in metadata enables auto-install in OpenClaw

**Hypothesis:** The `install` array in `metadata.openclaw` triggers automatic dependency installation when a skill is loaded in OpenClaw (similar to how `requires.bins` gates the skill).

**Evidence for:** The metadata schema documents `install` with `kind: node` and `kind: uv` entries alongside package lists.

**Evidence against:** No direct confirmation that auto-install runs vs. just declaring intent.

**How to test:** Deploy `curatr` skill to a fresh OpenClaw instance without pre-installed deps and observe if `npm install` and `uv add` run automatically.

**Confirmation count:** 0 / 5 needed for promotion to rule.

---

## HYP-3: `disable-model-invocation: true` is appropriate for all four HealthClaw skills

**Hypothesis:** All four skills (`fhir-r6-guardrails`, `curatr`, `phi-redaction`, `fhir-upstream-proxy`) correctly use `disable-model-invocation: true`. However, `curatr` contains a conversational flow guide (the Messenger flow) that might benefit from model injection.

**Evidence for:** Skills with large technical reference bodies (guardrails, proxy config) should not be injected into every prompt — token overhead is wasted.

**Evidence against:** `curatr` has a Conversation Flow section that tells the model how to present issues to patients. This behavioral guidance might be more effective if injected.

**How to test:** Remove `disable-model-invocation` from `curatr` and compare agent behavior when evaluating patient records.

**Confirmation count:** 0 / 5 needed for promotion to rule.
