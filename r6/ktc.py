"""The `ktc` export: Kill the Clipboard, a SMART Health Link to a clinic's EHR.

The patient shares a chosen set of their records with a clinic as a SMART
Health Link (https://ktc-spec.github.io/, v0.10.2). This module is the
engine's first piece of that: a short-lived, single-use token minted for an
exact set of resource types, and a `$share-bundle` profile that exports only
those types, for one named patient, through r6/access.py `_ktc_profile`.

It is the check-in export with three differences:

- its own grant (`aud` ktc-export, `op` from `operation`), so a check-in
  token cannot reach it and a ktc token cannot reach check-in;
- no `meta.profile` on what leaves, and Patient is always in the set, so
  the bundle carries the one Patient the spec requires;
- its own size ceiling, for the PDF a later PR adds.

The row walk, the claim gate, the redaction, our labels and the HTEST
marker are the check-in's own (r6/checkin.collect, r6/access.py
_checkin_profile), not copies of them.

Until the link store lands (design PR 3) the export would answer with the
bundle itself, and the design says plaintext never leaves the engine. So it
is OFF unless KTC_EXPORT_ENABLED is set (`export_enabled`): the tests turn
it on, no deployment does. PR 3 replaces the body with
`{link_id, shlink, exp}` and can then remove the flag.

`"deliver": "link"` is that body, added beside the bundle answer rather
than replacing it: the bundle is encrypted and stored in r6/shl_links.py and
the answer is `{link_id, shlink, exp}`, so the plaintext stays here. It has
its own switch, KTC_LINK_ENABLED, so a deployment can answer links while the
bundle answer stays off. Two more gates apply to it, both checked before the
token is spent:

- Test data only. Every exported entry must carry the HTEST marker, unless
  KTC_LINK_REAL_RECORDS is set. CareAgents' text line (Sendblue) is not yet
  under a BAA, and a link is made from a text conversation, so until the
  owner says otherwise a link carries made-up records only (403).
- LINK_MAX_BUNDLE_BYTES, lower than MAX_BUNDLE_BYTES, so the encrypted file
  stays under the 1 MB a receiver such as RX Genie accepts (413).

Both handlers live here rather than in r6/routes.py, which is pinned to
shrink: `mint` is the ktc branch of /internal/step-up-token and `export`
the ktc branch of $share-bundle.
"""

import json
import os

from flask import Response, jsonify

from models import db
from r6 import access, checkin, shl_links, stepup
from r6.audit import add_audit_event
from r6.runtime_config import TRUE_VALUES

#: The six items a link may carry; payor Organizations follow the exported
#: Coverage by reference, never by type. The check-in set, by owner ruling
#: 2026-10-16.
KTC_TYPES = checkin.CHECKIN_TYPES

#: The `aud` claim of a ktc token. No other mint sets it.
AUDIENCE = 'ktc-export'

#: Fixed; the caller does not choose it.
TOKEN_TTL_SECONDS = 120

#: A serialised bundle above this is refused (413), never truncated. Larger
#: than check-in's: the rendered PDF (design PR 2) rides inside the bundle
#: as base64.
MAX_BUNDLE_BYTES = 2_000_000

#: The agent id the export audits under. Fixed words.
AUDIT_AGENT = 'careagents-ktc'

#: A bundle delivered as a link stays under this, so its JWE (base64url,
#: 4/3 the size, plus a short header) stays under a receiver's 1 MB.
LINK_MAX_BUNDLE_BYTES = 700_000

#: The label every link carries. Fixed words: a receiver may log it, so it
#: names no person.
LINK_LABEL = 'Shared health records'

_TEST_MARKER = ('http://terminology.hl7.org/CodeSystem/v3-ActReason', 'HTEST')


def export_enabled():
    """Whether this engine answers the ktc export. Off unless
    KTC_EXPORT_ENABLED says so: until PR 3 the answer is the plaintext
    bundle, which the design keeps inside the engine."""
    return os.environ.get('KTC_EXPORT_ENABLED', '').strip().lower() \
        in TRUE_VALUES


def link_enabled():
    """Whether this engine answers `deliver: link` with the bundle answer
    off. Off unless KTC_LINK_ENABLED says so. The ciphertext-only answer is
    what the design lets out of the engine, so this is the switch a
    deployment turns on; KTC_EXPORT_ENABLED also opens it."""
    return os.environ.get('KTC_LINK_ENABLED', '').strip().lower() \
        in TRUE_VALUES


def link_real_records():
    """Whether a link may carry records without the HTEST marker. Off
    unless KTC_LINK_REAL_RECORDS says so; the owner sets it once the text
    line is under a BAA."""
    return os.environ.get('KTC_LINK_REAL_RECORDS', '').strip().lower() \
        in TRUE_VALUES


def is_test_data(resource):
    """True when the exported resource carries the HTEST marker on
    meta.tag or meta.security (the profile keeps it there, r6/access.py)."""
    meta = resource.get('meta') if isinstance(resource, dict) else None
    if not isinstance(meta, dict):
        return False
    system, code = _TEST_MARKER
    return any(
        isinstance(c, dict) and c.get('system') == system
        and c.get('code') == code
        for slot in ('tag', 'security')
        for c in (meta.get(slot) if isinstance(meta.get(slot), list) else []))


def parse_types(value):
    """The requested types as a sorted list, or None unless the request is
    a non-empty JSON array of KTC_TYPES strings that names Patient."""
    types = checkin.parse_types(value)
    return types if types and 'Patient' in types else None


def operation(types):
    """The `op` claim for exactly this set of types."""
    return 'ktc:' + ','.join(sorted(set(types)))


def mint(tenant_id, body):
    """The ktc branch of issue_step_up_token (r6/routes.py), behind its
    `_internal_mint_authorized` gate. A ttl, aud, op or scope in the body is
    refused, not ignored."""
    if body.get('purpose') != 'ktc':
        return jsonify({'error': 'purpose must be "ktc"'}), 400
    if set(body) - {'tenant_id', 'purpose', 'resource_types'}:
        return jsonify({'error': 'a ktc token takes only tenant_id, '
                                 'purpose and resource_types'}), 400
    types = parse_types(body.get('resource_types'))
    if types is None:
        return jsonify({'error': 'resource_types must be a non-empty array '
                                 'that includes Patient, of: '
                                 + ', '.join(sorted(KTC_TYPES))}), 400
    try:
        token = stepup.generate_step_up_token(
            tenant_id, agent_id=AUDIT_AGENT, ttl_seconds=TOKEN_TTL_SECONDS,
            scope='read', audience=AUDIENCE, operation=operation(types))
    except ValueError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'token': token, 'tenant_id': tenant_id, 'scope': 'read',
                    'expires_in': TOKEN_TTL_SECONDS})


def _refuse(tenant, status, reason, body):
    add_audit_event(
        'read', resource_type='Bundle', resource_id='share-bundle',
        agent_id=AUDIT_AGENT, tenant_id=tenant.id, outcome='failure',
        detail=f'share-bundle export (profile=ktc) refused: {reason}')
    db.session.commit()
    return jsonify(body), status


def export(tenant, body):
    """$share-bundle profile=ktc.

    - Off (export_enabled false, the default): 404, audited, before
      anything else is read, so no token is spent and the answer is the
      same for any caller.
    - Gate: a token minted for exactly this type set (`aud` ktc-export,
      `op` from r6/ktc.operation), single use. A check-in token, the cached
      general token (no `aud`), a subset or a superset is refused.
    - `patient_id` and a valid type set naming Patient are required, checked
      before the token is spent; a 400 is audited.
    - Rows: r6/checkin.collect, through r6/access.py _ktc_profile.
    - No exportable Patient (absent, deleted, unclaimed): 422, audited, token
      not spent. The spec requires the bundle to carry the Patient.
    - Over MAX_BUNDLE_BYTES: 413, no bundle, audited, token not spent.
    - One AuditEvent, from fixed words.
    """
    # The link answer has its own switch, so a deployment can offer links
    # without the plaintext bundle answer.
    if not (export_enabled()
            or (body.get('deliver') == 'link' and link_enabled())):
        return _refuse(tenant, 404, 'export disabled',
                       {'error': 'ktc_export_disabled'})

    # Request checks first, so a malformed one does not spend the token
    # (#963 security review F4).
    types = parse_types(body.get('resource_types'))
    patient_id = body.get('patient_id')
    deliver = body.get('deliver')
    if deliver not in (None, 'link'):
        return _refuse(
            tenant, 400, 'deliver malformed',
            {'resourceType': 'OperationOutcome', 'issue': [{
                'severity': 'error', 'code': 'value',
                'diagnostics': 'deliver must be "link" or absent'}]})
    if types is None or not (isinstance(patient_id, str)
                             and checkin._PATIENT_ID.fullmatch(patient_id)):
        return _refuse(
            tenant, 400, 'resource_types or patient_id missing or malformed',
            {'resourceType': 'OperationOutcome', 'issue': [{
                'severity': 'error', 'code': 'required',
                'diagnostics': 'profile "ktc" requires resource_types from '
                               'the ktc set, including Patient, and a '
                               'patient_id'}]})

    # Checked without spending; spent only once the bundle is known to be
    # sendable (r6/checkin.py export, the same ordering).
    grant = dict(scope=access.Scope.TENANT_BOUND, tenant=tenant,
                 audience=AUDIENCE, operation=operation(types),
                 absent_status=401, rejected_status=401)
    access.require_grant(**grant)

    entries, type_set = checkin.collect(tenant, types, patient_id,
                                        access._ktc_profile)
    if 'Patient' not in type_set:
        return _refuse(
            tenant, 422, 'no exportable Patient',
            {'resourceType': 'OperationOutcome', 'issue': [{
                'severity': 'error', 'code': 'not-found',
                'diagnostics': 'the named patient has no exportable '
                               'Patient record'}]})

    payload = json.dumps(checkin.collection(entries))
    limit = LINK_MAX_BUNDLE_BYTES if deliver == 'link' else MAX_BUNDLE_BYTES
    if len(payload.encode('utf-8')) > limit:
        return _refuse(tenant, 413,
                       f'over the size limit; types={",".join(types)}',
                       {'error': 'ktc_too_large'})

    if deliver == 'link':
        refused = _link_refusal(tenant, entries)
        if refused is not None:
            return refused

    access.require_grant(**grant, consume_nonce=True)
    if deliver == 'link':
        return _deliver_link(tenant, entries, type_set, types, payload)
    add_audit_event(
        'read', resource_type='Bundle', resource_id='share-bundle',
        agent_id=AUDIT_AGENT, tenant_id=tenant.id,
        detail=(f'share-bundle export (profile=ktc): {len(entries)} '
                f'resources across {len(type_set)} type(s); '
                f'types={",".join(types)}'))
    db.session.commit()
    return Response(payload, status=200, mimetype='application/fhir+json')


def _link_refusal(tenant, entries):
    """The refusal for `deliver: link`, or None. Checked before the token
    is spent: test data only unless KTC_LINK_REAL_RECORDS, and a public
    https base to build the file URL on."""
    if not link_real_records() and not all(
            is_test_data(e.get('resource')) for e in entries):
        return _refuse(tenant, 403, 'link refused: not test data',
                       {'error': 'ktc_not_test_data'})
    if shl_links.public_base() is None:
        return _refuse(tenant, 503, 'link refused: no public base url',
                       {'error': 'ktc_link_unavailable'})
    return None


def _deliver_link(tenant, entries, type_set, types, payload):
    """`deliver: link`, token spent: the bundle encrypted and stored, the
    answer the shlink."""
    link = shl_links.issue(tenant.id, payload.encode('utf-8'), LINK_LABEL)
    add_audit_event(
        'read', resource_type='Bundle', resource_id='share-bundle',
        agent_id=AUDIT_AGENT, tenant_id=tenant.id,
        detail=(f'share-bundle export (profile=ktc, deliver=link): '
                f'{len(entries)} resources across {len(type_set)} type(s); '
                f'types={",".join(types)}'))
    db.session.commit()
    resp = jsonify(link)
    resp.headers['Cache-Control'] = 'no-store'
    return resp
