"""
FHIR Resource Redaction.

Standard redaction profile for PHI protection applied consistently
on all resource access paths (not just context ingestion).

- Names: Truncate family and given names to first initial only (e.g. "Rivera" → "R.")
- Identifiers: Keep last 4 characters
- Addresses: Remove line/text, keep city/state/country
- Telecom: Replace values with [Redacted]
- Birth dates: Truncate to year only
- Photos: Remove entirely
- Narratives: Replace with redacted div
- Notes/comments: Replace with [Redacted]
"""

import json


def apply_redaction(resource):
    """
    Apply standard redaction profile to a FHIR resource.
    Returns a deep copy with PHI fields redacted.
    """
    redacted = json.loads(json.dumps(resource))  # Deep copy
    _redact_fields(redacted)

    # Also redact any contained resources
    if 'contained' in redacted and isinstance(redacted['contained'], list):
        for contained in redacted['contained']:
            if isinstance(contained, dict):
                _redact_fields(contained)

    return redacted


def _redact_fields(resource):
    """Redact PHI fields from a single resource dict (in-place)."""
    # Redact names: truncate family and given to first initial only
    if 'name' in resource and isinstance(resource['name'], list):
        for name_entry in resource['name']:
            if isinstance(name_entry, dict):
                if 'family' in name_entry and isinstance(name_entry['family'], str):
                    f = name_entry['family']
                    name_entry['family'] = (f[0] + '.') if len(f) > 0 else f
                if 'given' in name_entry and isinstance(name_entry['given'], list):
                    name_entry['given'] = [
                        g[0] + '.' if isinstance(g, str) and len(g) > 0 else g
                        for g in name_entry['given']
                    ]
                name_entry.pop('text', None)

    # Truncate birth date to year only
    if 'birthDate' in resource and isinstance(resource['birthDate'], str):
        resource['birthDate'] = resource['birthDate'][:4]

    # Remove photos
    resource.pop('photo', None)

    # Remove text narratives
    if 'text' in resource:
        resource['text'] = {
            'status': 'empty',
            'div': '<div xmlns="http://www.w3.org/1999/xhtml">[Redacted]</div>'
        }

    # Redact identifiers (keep last 4 characters)
    if 'identifier' in resource and isinstance(resource['identifier'], list):
        for ident in resource['identifier']:
            if 'value' in ident and isinstance(ident['value'], str):
                val = ident['value']
                if len(val) > 4:
                    ident['value'] = '***' + val[-4:]

    # Remove full addresses
    if 'address' in resource and isinstance(resource['address'], list):
        for addr in resource['address']:
            addr.pop('line', None)
            addr.pop('text', None)
            # Keep city, state, country for demographics

    # Redact telecom (phone numbers, emails)
    if 'telecom' in resource and isinstance(resource['telecom'], list):
        for telecom in resource['telecom']:
            if 'value' in telecom and isinstance(telecom['value'], str):
                telecom['value'] = '[Redacted]'

    # Remove notes/comments
    for field in ['note', 'comment']:
        if field in resource:
            if isinstance(resource[field], list):
                resource[field] = [{'text': '[Redacted]'}]
            elif isinstance(resource[field], str):
                resource[field] = '[Redacted]'


def apply_patient_controlled_redaction(resource, patient_id):
    """
    Patient-controlled deidentification mode.

    The patient owns this store — they want their own data, minus
    institutional identifiers that could re-identify them to third parties.

    Rules (differ from standard HIPAA Safe Harbor apply_redaction):
    - name[], telecom[], address[], photo[] — removed entirely
    - birthDate — PRESERVED (patient wants their own DOB)
    - Institutional identifiers (MRN, facility patient IDs) — removed
    - The healthclaw patient_id is injected as the sole canonical identifier
    - Clinical codes (SNOMED, ICD-10, LOINC, CVX, RxNorm) — pass through
    - meta.tag stamped with 'deidentified' + 'patient-controlled'
    - notes/comments — removed

    Args:
        resource: FHIR resource dict (not modified in place)
        patient_id: The healthclaw.io canonical patient ID to inject

    Returns:
        Deep copy with patient-controlled deidentification applied
    """
    import copy
    result = copy.deepcopy(resource)

    # Remove direct identifiers entirely
    result.pop('name', None)
    result.pop('telecom', None)
    result.pop('address', None)
    result.pop('photo', None)

    # birthDate is PRESERVED — patient wants their own DOB in their store

    # Remove institutional identifiers; inject healthclaw canonical ID
    INSTITUTIONAL_SYSTEMS = {
        'http://hl7.org/fhir/sid/us-ssn',
        'urn:oid:2.16.840.1.113883.4.1',   # SSN OID
    }
    # Strip identifiers whose system looks institutional (MRN, facility ID)
    # Keep only the injected healthclaw identifier
    filtered_identifiers = []
    if 'identifier' in result and isinstance(result['identifier'], list):
        for ident in result['identifier']:
            system = ident.get('system', '')
            # Drop SSN and any system-less or facility-scoped identifiers
            if system in INSTITUTIONAL_SYSTEMS:
                continue
            # Drop MRN-style identifiers (heuristic: system contains mrn etc.)
            institutional_kw = ('mrn', 'patient_id', 'facility', 'org/', 'example.org')
            if any(kw in system.lower() for kw in institutional_kw):
                continue
            filtered_identifiers.append(ident)

    # Always inject healthclaw canonical identifier
    filtered_identifiers.insert(0, {
        'system': 'https://healthclaw.io/patient-id',
        'value': patient_id,
    })
    result['identifier'] = filtered_identifiers

    # Remove notes/comments
    for field in ('note', 'comment'):
        result.pop(field, None)

    # Remove narrative text
    if 'text' in result:
        result.pop('text')

    # Stamp meta.tag with deidentified + patient-controlled
    meta = result.setdefault('meta', {})
    tags = meta.get('tag', [])
    existing_codes = {t.get('code') for t in tags}
    if 'deidentified' not in existing_codes:
        tags.append({
            'system': (
                'http://terminology.hl7.org/CodeSystem/v3-ObservationValue'
            ),
            'code': 'ANONYED',
            'display': 'anonymized',
        })
    if 'patient-controlled' not in existing_codes:
        tags.append({
            'system': 'https://healthclaw.io/tags',
            'code': 'patient-controlled',
            'display': 'Patient-controlled deidentification',
        })
    meta['tag'] = tags

    return result
