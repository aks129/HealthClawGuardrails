"""
FHIR Resource Redaction.

Standard redaction profile for PHI protection applied consistently
on all resource access paths (not just context ingestion).

- Names: Truncate family and given names to first initial only (e.g. "Rivera" → "R.")
- Identifiers: Remove the value from every Identifier under a key in
  `_IDENTIFIER_KEYS` (`identifier`, `accessionIdentifier`, `requisition`,
  `valueIdentifier`, ...); keep `system` and `type`. Safe Harbor
  §164.514(b)(2)(i)(G)/(H)/(I)/(J) list SSNs, medical record numbers,
  health plan and account numbers among the identifiers to REMOVE — a
  last-four suffix is a re-identification vector, not a redaction.
  Coverage's plain-string member numbers (`subscriberId`, `dependent`,
  R4 `class.value`) are removed too (#282). This names the keys the
  supported types use; an identifier-like value under any other key is
  not covered, so do not read the Safe Harbor citation as the codebase's
  total Safe Harbor posture.
- Addresses: Remove line/text/city/district/postalCode, keep state/country,
  whether the element is a list or a single Address
- Telecom: Replace values with [Redacted]
- Birth dates: Truncate to year only
- Photos: Remove entirely
- Narratives: Replace with redacted div
- Notes/comments: Replace with [Redacted]
"""

import json

from r6.terminology import label_codings


def apply_redaction(resource):
    """
    Apply standard redaction profile to a FHIR resource.
    Returns a deep copy with PHI fields redacted.

    Redaction strips every `display` and `text`, because upstream systems write
    patient names into them. That left records with no readable name at all —
    measured at 0 of 65 labelled on a live tenant — so afterwards we put back
    labels for codes the SERVER recognises (r6/terminology.py). The order
    matters: strip whatever the upstream said, then add only what we know
    ourselves, keyed by code. Unrecognised codes stay unlabelled on purpose.
    """
    redacted = json.loads(json.dumps(resource))  # Deep copy
    _redact_recursive(redacted)
    label_codings(redacted)

    return redacted


def _redact_fields(resource, narrative=True):
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
    if 'text' in resource and narrative:
        resource['text'] = {
            'status': 'empty',
            'div': '<div xmlns="http://www.w3.org/1999/xhtml">[Redacted]</div>'
        }
    elif 'text' in resource:
        resource.pop('text', None)

    # Remove identifier values. Until 2026-09 this kept the last four
    # characters, and SECURITY.md said so (docs/2026-08-16-hard-truths.md
    # §4). `system` and `type` stay so a reader can see which kind of
    # identifier existed without learning it. FHIR puts the Identifier
    # datatype under other keys too (_IDENTIFIER_KEYS); those leaked whole
    # until #282. A plain string under one of them (R4's
    # Coverage.subscriberId, AuditEvent.agent.altId) is removed outright.
    for key in _IDENTIFIER_KEYS:
        identifiers = resource.get(key)
        if isinstance(identifiers, str):
            resource.pop(key)
            continue
        if isinstance(identifiers, dict):
            identifiers = [identifiers]
        if isinstance(identifiers, list):
            for ident in identifiers:
                if isinstance(ident, dict):
                    ident.pop('value', None)

    # Remove full addresses. Most resources carry a list; Location.address
    # is a single Address, and leaked whole until #282.
    addresses = resource.get('address')
    if isinstance(addresses, dict):
        addresses = [addresses]
    if isinstance(addresses, list):
        for addr in addresses:
            if not isinstance(addr, dict):
                continue
            addr.pop('line', None)
            addr.pop('text', None)
            addr.pop('city', None)
            addr.pop('district', None)
            addr.pop('postalCode', None)
            # State/country remain useful coarse demographics.

    # Redact telecom (phone numbers, emails)
    if 'telecom' in resource and isinstance(resource['telecom'], list):
        for telecom in resource['telecom']:
            if 'value' in telecom and isinstance(telecom['value'], str):
                telecom['value'] = '[Redacted]'

    # Redact Patient.contact[] — emergency-contact name / phone / address is
    # PHI and must not pass through on reads (contact.name is a single
    # HumanName, telecom a list, address a single Address).
    if 'contact' in resource and isinstance(resource['contact'], list):
        for c in resource['contact']:
            if not isinstance(c, dict):
                continue
            cn = c.get('name')
            if isinstance(cn, dict):
                if isinstance(cn.get('family'), str) and cn['family']:
                    cn['family'] = cn['family'][0] + '.'
                if isinstance(cn.get('given'), list):
                    cn['given'] = [
                        g[0] + '.' if isinstance(g, str) and len(g) > 0 else g
                        for g in cn['given']
                    ]
                cn.pop('text', None)
            for tc in (c.get('telecom') or []):
                if isinstance(tc, dict) and isinstance(tc.get('value'), str):
                    tc['value'] = '[Redacted]'
            ca = c.get('address')
            if isinstance(ca, dict):
                ca.pop('line', None)
                ca.pop('text', None)
                ca.pop('city', None)
                ca.pop('district', None)
                ca.pop('postalCode', None)
            # Subscription.contact[] is a ContactPoint itself, not a
            # Patient.contact entry, so its value sits right here (#282).
            if isinstance(c.get('value'), str):
                c['value'] = '[Redacted]'

    # Coverage.dependent is a plain string; Coverage.class.value is a string
    # in R4 and an Identifier from R5 on. Both are plan-membership numbers
    # (Safe Harbor §164.514(b)(2)(i)(I)), and `value`/`dependent` mean other
    # things elsewhere, so this is scoped by resource type (#282).
    if resource.get('resourceType') == 'Coverage':
        if isinstance(resource.get('dependent'), str):
            resource.pop('dependent')
        for cls in (resource.get('class') or []):
            if not isinstance(cls, dict):
                continue
            if isinstance(cls.get('value'), str):
                cls.pop('value')
            elif isinstance(cls.get('value'), dict):
                cls['value'].pop('value', None)

    # A stored AuditEvent's agent carries a network address (an IP or host
    # name, Safe Harbor (O)). `altId` is handled with the identifiers above.
    if resource.get('resourceType') == 'AuditEvent':
        for agent in (resource.get('agent') or []):
            if not isinstance(agent, dict):
                continue
            if isinstance(agent.get('network'), dict):
                agent['network'].pop('address', None)
            agent.pop('networkString', None)
            agent.pop('networkUri', None)

    # CarePlan.title is written per patient by whoever made the plan. `title`
    # elsewhere (Questionnaire, Requirements) is a definition's own label and
    # stays, so this one is scoped by resource type (#282).
    if resource.get('resourceType') == 'CarePlan' and \
            isinstance(resource.get('title'), str):
        resource.pop('title')

    # Goal.statusReason is free text. On other resources the same key is a
    # CodeableConcept or a list of them, which must keep its codes (#282).
    if isinstance(resource.get('statusReason'), str):
        resource.pop('statusReason')

    # Remove notes/comments. DiagnosticReport.conclusion is the same kind of
    # clinician free text and leaked on the standard read path until #282.
    for field in ['note', 'comment', 'conclusion']:
        if field in resource:
            if isinstance(resource[field], list):
                resource[field] = [{'text': '[Redacted]'}]
            elif isinstance(resource[field], str):
                resource[field] = '[Redacted]'


_FREE_TEXT_KEYS = {
    'display', 'description', 'valueString', 'valueMarkdown', 'valueUrl',
    'valueUri', 'valueCanonical', 'valueBase64Binary',
    # Free-text strings that survived a sweep of every string/markdown
    # element of the supported types (#282). Each key is a string wherever
    # FHIR uses it. authorString is Annotation's author NAME.
    'patientInstruction', 'onsetString', 'abatementString',
    'occurrenceString', 'performedString', 'scheduledString',
    'authorString', 'detailString', 'ageString', 'bornString',
    'deceasedString',
}
_DATE_KEYS = {
    'birthDate', 'deceasedDateTime', 'valueDate', 'valueDateTime',
}
# Keys that hold the Identifier datatype in the supported types, whatever the
# resource. `subscriberId` is Coverage's member id (a string in R4) and
# `altId` an AuditEvent agent's alternate user id (a string).
_IDENTIFIER_KEYS = (
    'identifier', 'accessionIdentifier', 'masterIdentifier',
    'groupIdentifier', 'requisition', 'preAdmissionIdentifier',
    'valueIdentifier', 'subscriberId', 'altId',
)
# Elements only an Attachment has. `url` and `title` alone are not enough to
# know one: an Extension has a `url`, a definition has both.
_ATTACHMENT_ONLY_KEYS = (
    'contentType', 'size', 'hash', 'creation', 'pages', 'frames',
    'duration', 'height', 'width',
)


def _redact_recursive(obj):
    """Recursively minimize common PHI-bearing FHIR datatypes in-place."""
    if isinstance(obj, list):
        for item in obj:
            _redact_recursive(item)
        return
    if not isinstance(obj, dict):
        return

    _redact_fields(obj, narrative=bool(obj.get('resourceType')))

    # Attachment content and signed URLs can directly contain or reveal PHI.
    # Every Attachment element is optional, so it is known by shape: an
    # Attachment-only key, or `data` (SampledData also has `data`, with
    # `dimensions`), or `url` with `title` on a datatype (a Questionnaire is
    # a resource with both). Until #282 only `contentType` counted, so an
    # attachment without one kept its body.
    is_attachment = (
        any(k in obj for k in _ATTACHMENT_ONLY_KEYS)
        or ('data' in obj and 'dimensions' not in obj)
        or ('url' in obj and 'title' in obj and 'resourceType' not in obj))
    if is_attachment and any(k in obj for k in ('data', 'url', 'title')):
        obj.pop('data', None)
        obj.pop('url', None)
        obj.pop('title', None)

    # Organization/Practitioner names are often represented as a scalar.
    if isinstance(obj.get('name'), str):
        value = obj['name']
        obj['name'] = value[0] + '.' if value else value

    for key in list(obj):
        value = obj.get(key)
        if key in _FREE_TEXT_KEYS:
            obj.pop(key, None)
            continue
        if key in _DATE_KEYS and isinstance(value, str):
            obj[key] = value[:4]
            continue
        _redact_recursive(value)


def apply_patient_controlled_redaction(resource, patient_id):
    """
    Patient-controlled deidentification mode.

    The patient owns this store — they want their own data, minus
    institutional identifiers that could re-identify them to third parties.

    Rules (differ from the stricter de-identification preview):
    - name[], telecom[], address[], photo[] — removed entirely at the top
      level (a stronger guarantee than the standard profile's redact-to-
      initial, since this output feeds external sharing: SHL / $share-bundle)
    - birthDate — PRESERVED at the top level (patient wants their own DOB)
    - Institutional identifiers (MRN, facility patient IDs) — removed
    - The healthclaw patient_id is injected as the sole canonical top-level
      identifier
    - Clinical codes (SNOMED, ICD-10, LOINC, CVX, RxNorm) — pass through
    - meta.tag stamped with 'deidentified' + 'patient-controlled'
    - notes/comments — removed

    #617: this function used to stop at the top level. `contained[]`,
    `subject.display`, `generalPractitioner[].display` and any other nested
    person-bearing field survived untouched inside a Bundle stamped
    'ANONYED'. Fixed by running the same recursive walker `apply_redaction`
    uses over the WHOLE tree first — every `display`/free-text key, every
    nested name/telecom/address/photo/text/note, at any depth, gets the
    standard profile's treatment — and then applying this function's own
    STRONGER top-level-only policy (full removal instead of redaction,
    verbatim birthDate, the single canonical identifier) on top. This is a
    policy layered on the one walker, not a second one: nested resources
    (a contained RelatedPerson, a referenced Practitioner's display) get the
    standard profile's guarantee; the top-level resource — the one this
    function exists to protect — gets the stronger one on top of it.

    Args:
        resource: FHIR resource dict (not modified in place)
        patient_id: The healthclaw.io canonical patient ID to inject

    Returns:
        Deep copy with patient-controlled deidentification applied
    """
    import copy
    result = copy.deepcopy(resource)
    original_birth_date = result.get('birthDate')

    # Base pass: the same walker apply_redaction uses, over the whole tree.
    # Closes #617 — nothing nested (contained[], subject.display,
    # generalPractitioner[].display, any reference's .display) survives this
    # untouched, because the walker does not stop at the top level.
    _redact_recursive(result)

    # Everything below is this function's OWN, stronger, top-level-only
    # policy layered on top of the base pass above.

    # Remove direct identifiers entirely
    result.pop('name', None)
    result.pop('telecom', None)
    result.pop('address', None)
    result.pop('photo', None)
    # Patient.contact[] carries emergency-contact name/phone/address — remove it
    # wholesale (this output feeds SHL / $share-bundle external sharing).
    result.pop('contact', None)

    # birthDate is PRESERVED verbatim at the top level — patient wants their
    # own DOB in their store. The base pass above truncated it to a year
    # like every other date in the tree; restore the original here, only at
    # the top level, only for this function.
    if original_birth_date is not None:
        result['birthDate'] = original_birth_date

    # The healthclaw canonical id is the SOLE identifier — built, not
    # filtered. The keyword denylist this replaced ('mrn', 'facility', ...)
    # passed any upstream identifier whose system used other words through
    # with its value intact, which is the same last-four-class gap the
    # standard profile had (Safe Harbor §164.514(b)(2)(i)(H)).
    result['identifier'] = [{
        'system': 'https://healthclaw.io/patient-id',
        'value': patient_id,
    }]

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
