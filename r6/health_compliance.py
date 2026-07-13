"""
Health Compliance Module for FHIR R6 MCP.

Implements OpenAI Health App and marketplace requirements:
- Medical disclaimer injection on all clinical responses
- Human-in-the-loop enforcement for write operations
- Conservative de-identification preview (expert review required)
- Audit trail NDJSON export for compliance review
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from flask import request, jsonify

logger = logging.getLogger(__name__)

# --- Medical Disclaimer ---

MEDICAL_DISCLAIMER = (
    'DISCLAIMER: This system provides informational data only and does not '
    'constitute medical advice, diagnosis, or treatment. All clinical data '
    'should be reviewed by a licensed healthcare professional before any '
    'clinical decision-making. This tool is not FDA-approved as a medical '
    'device. See privacy policy for full terms.'
)

CLINICAL_RESOURCE_TYPES = {
    'Observation', 'Condition', 'MedicationRequest', 'DiagnosticReport',
    'AllergyIntolerance', 'Procedure', 'CarePlan', 'Immunization',
    # Phase 2 — R6 clinical types
    'NutritionIntake', 'DeviceAlert',
}


def add_disclaimer(response_data, resource_type=None):
    """
    Add medical disclaimer to FHIR response data when the content
    is clinical in nature.

    Args:
        response_data: dict — the FHIR resource or bundle
        resource_type: optional explicit resource type

    Returns:
        dict with _disclaimer extension added if clinical
    """
    rt = resource_type or response_data.get('resourceType', '')

    # Check if response contains clinical data
    is_clinical = False
    if rt in CLINICAL_RESOURCE_TYPES:
        is_clinical = True
    elif rt == 'Bundle':
        entries = response_data.get('entry', [])
        for entry in entries:
            entry_rt = entry.get('resource', {}).get('resourceType', '')
            if entry_rt in CLINICAL_RESOURCE_TYPES:
                is_clinical = True
                break

    if is_clinical:
        response_data['_disclaimer'] = {
            'text': MEDICAL_DISCLAIMER,
            'url': '/r6/fhir/docs/privacy-policy',
        }

    return response_data


# --- Human-in-the-Loop Enforcement ---

def require_human_confirmation(resource):
    """
    Check if a write operation requires explicit human confirmation
    beyond the step-up token.

    Returns True for high-risk clinical writes that require a
    separate confirmation header (X-Human-Confirmed: true).
    """
    rt = resource.get('resourceType', '')
    if rt in CLINICAL_RESOURCE_TYPES:
        return True
    # Consent changes always require human confirmation
    if rt == 'Consent':
        return True
    return False


def enforce_human_in_loop():
    """
    Flask before_request check for human-in-the-loop on clinical writes.
    Only applies to POST/PUT with clinical resource types.
    Exempts $validate (read-only) and other operation endpoints.
    """
    if request.method not in ('POST', 'PUT'):
        return None

    # Exempt validation, operations, and internal demo endpoints.
    # $interpret is read-shaped (lab reference-range interpreter) — it never
    # writes the posted Observation/Bundle to the store, so the clinical-write
    # human-confirmation gate does not apply.
    if ('$validate' in request.path or '$import-stub' in request.path
            or '$interpret' in request.path):
        return None
    if '$ingest-context' in request.path:
        return None
    if '/demo/' in request.path or '/internal/' in request.path:
        return None

    body = request.get_json(silent=True)
    if not body:
        return None

    if require_human_confirmation(body):
        confirmed = request.headers.get('X-Human-Confirmed', '').lower()
        if confirmed != 'true':
            return jsonify({
                'resourceType': 'OperationOutcome',
                'issue': [{
                    'severity': 'error',
                    'code': 'business-rule',
                    'diagnostics': (
                        'This clinical write requires human confirmation. '
                        'Set X-Human-Confirmed: true header to proceed. '
                        'A licensed healthcare professional must review '
                        'and approve clinical data modifications.'
                    )
                }]
            }), 428  # Precondition Required


# --- De-identification preview (not a certified HIPAA determination) ---

# Direct identifier-bearing FHIR fields removed by the preview policy.
DEIDENTIFICATION_FIELDS = {
    'name', 'address', 'telecom',
    'identifier', 'photo', 'contact',
}

# Date fields to generalize (year only)
DATE_FIELDS = {'birthDate', 'deceasedDateTime'}


def deidentify_resource(resource):
    """
    Apply the conservative HealthClaw de-identification preview.

    This is a technical data-minimization aid, not a legal determination that
    a dataset satisfies the HIPAA Safe Harbor method. Deployment-specific
    expert review remains required before external disclosure.

    Returns a deep copy with PHI removed.
    """
    deidentified = json.loads(json.dumps(resource))
    _strip_deidentification_preview(deidentified)

    # Add de-identification tag
    meta = deidentified.setdefault('meta', {})
    security = meta.setdefault('security', [])
    security.append({
        'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationValue',
        'code': 'ANONYED',
        'display': 'anonymized',
    })

    return deidentified


def _strip_deidentification_preview(resource):
    """Recursively remove common identifying FHIR values in-place."""
    if not isinstance(resource, dict):
        return

    # Remove direct identifiers
    for field in DEIDENTIFICATION_FIELDS:
        resource.pop(field, None)

    # Remove text narratives (may contain PHI)
    resource.pop('text', None)

    # Generalize dates to year only
    for field in list(resource):
        value = resource.get(field)
        if (field in DATE_FIELDS or field.endswith('Date')
                or field.endswith('DateTime') or field.endswith('Instant')):
            if isinstance(value, str):
                resource[field] = value[:4]

    # Remove age if > 89 (Safe Harbor requirement)
    age_fields = ['_age', 'age']
    for af in age_fields:
        if af in resource:
            age_val = resource[af]
            if isinstance(age_val, dict):
                val = age_val.get('value', 0)
                if isinstance(val, (int, float)) and val > 89:
                    resource[af] = {'value': 90, 'comparator': '>='}

    # Remove notes, comments, descriptions (free text may contain PHI)
    for field in [
        'note', 'comment', 'description', 'display', 'reference',
        'valueString', 'valueMarkdown', 'valueUrl', 'valueUri',
        'valueCanonical', 'valueBase64Binary',
    ]:
        resource.pop(field, None)

    # Strip CodeableConcept.text fields (may contain regional identifiers)
    _strip_codeable_concept_text(resource)

    # Attachment payloads and signed URLs can carry PHI directly.
    if ('contentType' in resource
            and any(k in resource for k in ('data', 'url', 'title'))):
        resource.pop('data', None)
        resource.pop('url', None)
        resource.pop('title', None)

    # Generate a replacement pseudonymous ID
    if 'id' in resource:
        original_id = resource['id']
        resource['id'] = hashlib.sha256(
            f'deidentified:{original_id}'.encode()
        ).hexdigest()[:16]

    for value in list(resource.values()):
        if isinstance(value, dict):
            _strip_deidentification_preview(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strip_deidentification_preview(item)


# Backward-compatible private alias for integrations that imported it despite
# the leading underscore. The public claim is deliberately no longer Safe Harbor.
_strip_safe_harbor = _strip_deidentification_preview


def _strip_codeable_concept_text(obj):
    """Recursively strip 'text' from CodeableConcept-like dicts."""
    if not isinstance(obj, dict):
        return
    # A CodeableConcept has 'coding' array and optional 'text'
    if 'coding' in obj and 'text' in obj:
        obj.pop('text', None)
    for val in obj.values():
        if isinstance(val, dict):
            _strip_codeable_concept_text(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _strip_codeable_concept_text(item)


def _is_identifying_extension(ext):
    """Check if an extension likely contains identifying information."""
    if not isinstance(ext, dict):
        return False
    url = ext.get('url', '')
    # Remove extensions related to race, ethnicity, birth-related info
    identifying_patterns = ['birthPlace', 'birthSex', 'nationality', 'tribe']
    return any(p in url for p in identifying_patterns)


# --- Audit Trail Export ---

def export_audit_trail(audit_records, format='ndjson'):
    """
    Export audit trail records in NDJSON format for compliance review.

    Args:
        audit_records: list of AuditEventRecord model instances
        format: 'ndjson' (default) or 'fhir-bundle'

    Returns:
        str: formatted audit trail
    """
    if format == 'fhir-bundle':
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'total': len(audit_records),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'entry': [
                {
                    'fullUrl': f'urn:uuid:{record.id}',
                    'resource': record.to_fhir_json()
                }
                for record in audit_records
            ]
        }
        return json.dumps(bundle, indent=2)

    # Default: NDJSON (one JSON object per line)
    lines = []
    for record in audit_records:
        lines.append(json.dumps(record.to_fhir_json(), separators=(',', ':')))
    return '\n'.join(lines)
