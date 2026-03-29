# Skill Standards — Facts and Patterns

Last updated: 2026-03-29

## Frontmatter Fields Reference

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | Yes | string | kebab-case, must match folder name |
| `description` | Yes | string | Trigger language; may use YAML `>` block scalar |
| `metadata` | No | **single-line JSON** | NOT multi-line YAML |
| `disable-model-invocation` | No | boolean (default false) | Set true for runtime/infra skills |
| `user-invocable` | No | boolean (default true) | False hides from slash-command list |
| `homepage` | No | URL | Shown as "Website" in macOS Skills UI |
| `command-dispatch` | No | `"tool"` | Bypasses model, routes directly to a tool |
| `command-tool` | No | string | Tool name when `command-dispatch: tool` |
| `license` | No | string | Used by Anthropic reference skills |

## Metadata Schema (OpenClaw)

```json
{
  "openclaw": {
    "requires": {
      "env": ["ENV_VAR_NAME"],
      "bins": ["node", "python3"],
      "anyBins": ["..."],
      "config": ["browser.enabled"]
    },
    "install": [
      {"kind": "node", "packages": ["pkg1", "pkg2"]},
      {"kind": "uv", "packages": ["pkg1"]}
    ],
    "primaryEnv": "ENV_VAR_NAME",
    "always": false,
    "emoji": "🔒",
    "os": ["darwin", "linux"],
    "skillKey": "override-key"
  }
}
```

## Skill Load Precedence (highest to lowest)

1. Workspace skills (`/path/to/workspace/.claude/skills/`)
2. Project agent skills
3. Personal agent skills
4. Managed/local skills
5. Bundled skills
6. Extra directories

## Anthropic Reference Skill Patterns (observed)

- **pdf**: `name` + `description` + `license` only. No metadata. Body is implementation guide.
- **mcp-builder**: `name` + `description` + `license`. Body is phased development guide.
- **claude-api**: `name` + `description`. Body is language-specific API usage guide.
- **webapp-testing**: `name` + `description`. Body is Playwright patterns.

Pattern: Anthropic skills are minimal in frontmatter and dense in body content.

## HealthClaw Skills Audit (2026-03-29)

| Skill | Compliance Issues Found | Fixed |
|---|---|---|
| `curatr` | `metadata` was multi-line YAML — non-compliant | Yes — converted to single-line JSON |
| `fhir-r6-guardrails` | None | N/A |
| `phi-redaction` | None | N/A |
| `fhir-upstream-proxy` | Stale version in body (`0.9.0`) | Yes — updated to `1.0.0` |

Also updated `r6/fhir_proxy.py` User-Agent from `MCP-FHIR-Guardrails/0.9.0` to `HealthClaw-Guardrails/1.0.0`.
