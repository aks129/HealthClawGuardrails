# Skill Standards — Confirmed Rules

Rules confirmed by cross-referencing OpenClaw docs (https://docs.openclaw.ai/tools/skills)
and Anthropic reference skills (https://github.com/anthropics/skills).

---

## RULE-1: `metadata` must be a single-line JSON object

**Confirmed:** OpenClaw parser requires `metadata` as a single-line JSON string, not nested YAML.

**Correct:**
```yaml
metadata: {"openclaw":{"requires":{"env":["MY_SECRET"],"bins":["node"]},"primaryEnv":"MY_SECRET"}}
```

**Wrong (multi-line YAML):**
```yaml
metadata:
  openclaw:
    requires:
      env:
        - MY_SECRET
```

**Source:** docs.openclaw.ai — "the parser used by the embedded agent supports single-line frontmatter keys only" and "metadata should be a single-line JSON object."

---

## RULE-2: Required frontmatter fields are `name` and `description` only

**Confirmed:** All other frontmatter fields are optional. Skills with only `name` + `description` are valid.

**Source:** OpenClaw docs + Anthropic pdf/mcp-builder skills (minimal frontmatter observed in production).

---

## RULE-3: Skill names use kebab-case

**Confirmed:** All Anthropic reference skills use kebab-case (`pdf`, `mcp-builder`, `claude-api`, `webapp-testing`). OpenClaw docs confirm kebab-case convention.

---

## RULE-4: `disable-model-invocation: true` is the correct flag for runtime/guardrail skills

**Confirmed:** Skills that provide reference documentation (not a conversational flow) should set this. Prevents skill body from being injected into every model prompt, reducing token overhead.

---

## RULE-5: Description should lead with strong trigger language

**Confirmed:** Anthropic pdf skill uses "Use this skill whenever the user wants to do anything with PDF files." Our pattern "Use when: (1)..." is acceptable but second-person trigger ("Use this skill whenever...") is the canonical Anthropic style.

---

## RULE-6: Skill folder name should match the `name` frontmatter field

**Confirmed:** OpenClaw warns when folder name and `name` field diverge. The optional `skillKey` in metadata can override, but matching is the default expectation.
