---
name: phi-redaction
description: >
  PHI redaction patterns for FHIR resources following HIPAA Safe Harbor. Use when:
  (1) Redacting patient health information from FHIR resources before AI agent access,
  (2) Implementing de-identification for clinical data pipelines,
  (3) Understanding what fields are stripped, masked, or truncated in FHIR resources,
  (4) Building read paths that automatically protect patient privacy.
---

# PHI Redaction for FHIR Resources

Standard redaction profile for Protected Health Information (PHI) applied on all
read paths in the FHIR MCP guardrail stack. Based on HIPAA Safe Harbor method.

## When to Use This Skill

- You need to understand how PHI is stripped from FHIR resources
- You are building a pipeline that must redact clinical data before AI access
- You need to implement or audit de-identification logic for FHIR

## Redaction Rules

All redaction is applied at read time, not at storage time.

### Names (`HumanName`)
- **Family name**: Kept as-is
- **Given names**: Truncated to first initial + period (e.g., "John" -> "J.")
- **Text**: Removed

### Identifiers
- **Value**: Masked to last 4 characters (e.g., "123-45-6789" -> "***6789")
- **System and type**: Kept for reference

### Addresses
- **Line and text**: Removed
- **City, state, country**: Kept (for demographic analysis)

### Telecom (Phone, Email)
- **Value**: Replaced with `[Redacted]`
- **System and use**: Kept

### Birth Date
- **Truncated to year only** (e.g., "1985-03-15" -> "1985")

### Photos
- **Removed entirely** from the resource

### Text Narratives
- **Replaced** with: `<div xmlns="http://www.w3.org/1999/xhtml">[Redacted]</div>`
- **Status** set to `empty`

### Notes and Comments
- **Replaced** with `[Redacted]`

### Contained Resources
- All contained resources are redacted recursively

## Implementation Pattern (Python)

```python
import json

def apply_redaction(resource):
    """Deep-copy the resource and redact PHI fields."""
    redacted = json.loads(json.dumps(resource))
    _redact_fields(redacted)
    for contained in redacted.get('contained', []):
        if isinstance(contained, dict):
            _redact_fields(contained)
    return redacted
```

Key implementation notes:
- Always deep-copy before redacting (never modify the stored resource)
- Redact contained resources recursively
- Apply on ALL read paths: direct reads, search results, context envelopes,
  upstream proxy responses

## HIPAA Safe Harbor Coverage

This redaction profile covers these Safe Harbor identifiers:
- Names (partial)
- Geographic data smaller than state (addresses stripped)
- Dates (birth date truncated to year)
- Telephone/fax numbers (redacted)
- Email addresses (redacted)
- Medical record numbers (masked to last 4)

**Not covered by this profile** (would need additional implementation):
- Social Security numbers (not typically in FHIR resources)
- IP addresses, device identifiers (not redacted from extension fields)
- Biometric identifiers
- Full-face photographs (removed if in `photo` field, not scanned in attachments)

## Integration with MCP Tools

The redaction is applied automatically by the guardrail stack. MCP tools like
`fhir.read`, `fhir.search`, `fhir.lastn`, and `context.get` all return
redacted data. No additional action is needed by the agent.

The `$deidentify` operation provides explicit HIPAA Safe Harbor de-identification
on demand, useful for export or analysis workflows.
