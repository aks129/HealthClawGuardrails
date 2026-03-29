# Knowledge Index

Domain knowledge captured during development of HealthClaw Guardrails.

## Domains

- [skills/](skills/) — Claude/OpenClaw skill file standards, compliance rules, and patterns
- [integrations/](integrations/) — Third-party data integrations: Fasten Connect, TEFCA IAS, webhook patterns

## How to Use

Each domain folder contains:
- `knowledge.md` — facts and observed patterns
- `rules.md` — confirmed rules (apply by default)
- `hypotheses.md` — unconfirmed patterns (need 5+ confirmations to promote to rule)

When a hypothesis is confirmed 5+ times, promote it to `rules.md` and remove from `hypotheses.md`.
When a rule is contradicted by new data, demote it back to `hypotheses.md`.
