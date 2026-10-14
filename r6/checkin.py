"""The `checkin` export: SMART Health Check-in's closed tables (FHI-53).

A check-in hands a clinic only the items the patient ticked, once. The engine
side is a short-lived, single-use token minted for an exact set of resource
types, and a `$share-bundle` profile that exports only those types, for one
named patient, and only rows that carry an allow-listed US Core or C4DIC
profile claim (r6/us_core_profiles.py). What each released resource keeps is
r6/access.py `_checkin_profile`.

One function, `operation`, builds the token's `op` claim for both the mint
and the export, so the two cannot disagree on spelling.

The two request handlers live here rather than in r6/routes.py, which is
pinned to shrink (tests/test_ratchets.py): `mint` is the check-in branch of
/internal/step-up-token and `export` the check-in branch of $share-bundle.
"""

import json
import re
from datetime import datetime, timezone

from flask import Response, jsonify

from models import db
from r6 import access, stepup
from r6.audit import add_audit_event
from r6.models import R6Resource

#: The six items a check-in may ask for. Organization is deliberately absent:
#: payor Organizations are fetched by the ids the exported Coverage
#: references, never by type, which would export every one in the tenant.
CHECKIN_TYPES = frozenset({
    'Patient', 'Condition', 'AllergyIntolerance', 'MedicationRequest',
    'Immunization', 'Coverage',
})

#: The `aud` claim of a check-in token. No other mint sets it, so the cached
#: general CareAgents token (no `aud`) is refused by this profile.
AUDIENCE = 'checkin-export'

#: Fixed; the caller does not choose it.
TOKEN_TTL_SECONDS = 120

#: A serialised bundle above this is refused (413), never truncated: a short
#: list read as complete is the worse failure. Leaves headroom for transport
#: overhead under the wallet's 512 KB.
MAX_BUNDLE_BYTES = 400_000

#: The agent id the export audits under. Fixed words: nothing the requester
#: wrote reaches the audit row.
AUDIT_AGENT = 'careagents-checkin'

_US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'

#: Which claims make a row of each type exportable. A claim for another
#: type does not count: a Condition claiming us-core-patient is unprofiled.
CLAIMS_BY_TYPE = {
    'Patient': frozenset({_US_CORE + 'us-core-patient'}),
    'Condition': frozenset({
        _US_CORE + 'us-core-condition',
        _US_CORE + 'us-core-condition-problems-health-concerns',
        _US_CORE + 'us-core-condition-encounter-diagnosis',
    }),
    'AllergyIntolerance': frozenset({_US_CORE + 'us-core-allergyintolerance'}),
    'MedicationRequest': frozenset({_US_CORE + 'us-core-medicationrequest'}),
    'Immunization': frozenset({_US_CORE + 'us-core-immunization'}),
    'Coverage': frozenset({
        _US_CORE + 'us-core-coverage',
        'http://hl7.org/fhir/us/insurance-card/StructureDefinition/'
        'C4DIC-Coverage',
    }),
}


def parse_types(value):
    """The requested types as a sorted list, or None if the request is not
    a non-empty JSON array of CHECKIN_TYPES strings."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(t, str) and t in CHECKIN_TYPES for t in value):
        return None
    return sorted(set(value))


def operation(types):
    """The `op` claim for exactly this set of types."""
    return 'checkin:' + ','.join(sorted(set(types)))


def has_claim(resource):
    """True when the resource carries an allow-listed claim for its own type.

    Reads the profile list `to_fhir_json` already filtered; the base before
    any `|version` is what is compared."""
    wanted = CLAIMS_BY_TYPE.get(resource.get('resourceType'))
    meta = resource.get('meta')
    claims = meta.get('profile') if isinstance(meta, dict) else None
    if not wanted or not isinstance(claims, list):
        return False
    return any(isinstance(c, str) and c.partition('|')[0] in wanted
               for c in claims)


#: The patient a check-in names: the store's own id shape.
_PATIENT_ID = re.compile(r'[A-Za-z0-9\-.]{1,255}')


def mint(tenant_id, body):
    """The check-in branch of issue_step_up_token (r6/routes.py), behind its
    `_internal_mint_authorized` gate. A ttl, aud, op or scope in the body is
    refused, not ignored: a caller who asked for one would otherwise believe
    they had it."""
    if body.get('purpose') != 'checkin':
        return jsonify({'error': 'purpose must be "checkin" or absent'}), 400
    if set(body) - {'tenant_id', 'purpose', 'resource_types'}:
        return jsonify({'error': 'a checkin token takes only tenant_id, '
                                 'purpose and resource_types'}), 400
    types = parse_types(body.get('resource_types'))
    if types is None:
        return jsonify({'error': 'resource_types must be a non-empty array '
                                 'of: ' + ', '.join(sorted(CHECKIN_TYPES))}), 400
    try:
        token = stepup.generate_step_up_token(
            tenant_id, agent_id=AUDIT_AGENT, ttl_seconds=TOKEN_TTL_SECONDS,
            scope='read', audience=AUDIENCE, operation=operation(types))
    except ValueError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'token': token, 'tenant_id': tenant_id, 'scope': 'read',
                    'expires_in': TOKEN_TTL_SECONDS})


def export(tenant, body):
    """$share-bundle profile=checkin (FHI-53).

    - Gate: a token minted for exactly this type set (`aud` checkin-export,
      `op` from r6/checkin.operation), single use. The cached general token
      has no `aud` and is refused; a subset or superset is refused.
    - `patient_id` and a valid type set are required, checked before the
      token is spent; a 400 is audited. Only that patient's rows go out.
    - Only rows carrying an allow-listed claim for their own type go out.
    - Each row goes through r6/access.py _checkin_profile. Payor Organizations
      are fetched by the ids the exported Coverage references, in this
      tenant, never by type.
    - Over MAX_BUNDLE_BYTES: 413 and no bundle. Never truncated.
    - One AuditEvent, from fixed words.
    """
    # The request is checked BEFORE the grant, so a malformed one does not
    # spend the single-use token (#963 security review F4). Both checks are
    # about the request, not the credential, and answer the same to anyone.
    types = parse_types(body.get('resource_types'))
    patient_id = body.get('patient_id')
    if types is None or not (isinstance(patient_id, str)
                             and _PATIENT_ID.fullmatch(patient_id)):
        add_audit_event(
            'read', resource_type='Bundle', resource_id='share-bundle',
            agent_id=AUDIT_AGENT, tenant_id=tenant.id, outcome='failure',
            detail='share-bundle export (profile=checkin) refused: '
                   'resource_types or patient_id missing or malformed')
        db.session.commit()
        return jsonify({'resourceType': 'OperationOutcome', 'issue': [{
            'severity': 'error', 'code': 'required',
            'diagnostics': 'profile "checkin" requires resource_types from '
                           'the check-in set and a patient_id'}]}), 400

    access.require_grant(
        scope=access.Scope.TENANT_BOUND, tenant=tenant, audience=AUDIENCE,
        operation=operation(types), consume_nonce=True,
        absent_status=401, rejected_status=401)

    rows = R6Resource.query.filter(
        R6Resource.tenant_id == tenant.id,
        R6Resource.is_deleted == False,  # noqa: E712
        R6Resource.resource_type.in_(types),
    ).order_by(R6Resource.resource_type, R6Resource.id).all()

    own = f'Patient/{patient_id}'
    entries = []
    type_set = set()
    payor_ids = set()
    for row in rows:
        stored = row.to_fhir_json()
        if row.resource_type == 'Patient':
            if row.id != patient_id:
                continue
        else:
            ref = next((r.get('reference') for r in (
                stored.get('subject'), stored.get('patient'),
                stored.get('beneficiary')) if isinstance(r, dict)), None)
            if ref != own:
                continue
        if not has_claim(stored):
            continue
        resource = access._checkin_profile(stored, patient_id=patient_id)
        entries.append({'resource': resource})
        type_set.add(row.resource_type)
        for payor in resource.get('payor') or []:
            payor_ids.add(payor['reference'].split('/', 1)[1])

    if payor_ids:
        orgs = R6Resource.query.filter(
            R6Resource.tenant_id == tenant.id,
            R6Resource.is_deleted == False,  # noqa: E712
            R6Resource.resource_type == 'Organization',
            R6Resource.id.in_(sorted(payor_ids)),
        ).order_by(R6Resource.id).all()
        for org in orgs:
            entries.append({'resource': access._checkin_profile(
                org.to_fhir_json(), patient_id=patient_id)})
            type_set.add('Organization')

    bundle = {
        'resourceType': 'Bundle',
        'type': 'collection',
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        'entry': entries,
    }
    payload = json.dumps(bundle)
    too_large = len(payload.encode('utf-8')) > MAX_BUNDLE_BYTES

    detail = (f'share-bundle export (profile=checkin): {len(entries)} '
              f'resources across {len(type_set)} type(s); '
              f'types={",".join(types)}')
    if too_large:
        detail = ('share-bundle export (profile=checkin) refused: over the '
                  f'size limit; types={",".join(types)}')
    add_audit_event(
        'read', resource_type='Bundle', resource_id='share-bundle',
        agent_id=AUDIT_AGENT, tenant_id=tenant.id, detail=detail)
    db.session.commit()

    if too_large:
        return jsonify({'error': 'checkin_too_large'}), 413
    return Response(payload, status=200, mimetype='application/fhir+json')
