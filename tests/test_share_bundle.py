"""
Tests for POST /r6/fhir/$share-bundle.

The share-bundle endpoint exports a patient-controlled FHIR collection Bundle
for SMART Health Link generation.  It requires:
  - X-Tenant-Id  (enforced by r6_blueprint.before_request)
  - X-Step-Up-Token  (step-up gate inside the route)

Redaction applied: apply_patient_controlled_redaction — strips name/telecom/
address/notes, preserves DOB and clinical codes, injects healthclaw canonical
identifier, stamps meta.tag.
"""

import json
import pytest

from models import db
from r6.models import R6Resource, AuditEventRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_resource(app, resource_type, resource_json_dict, tenant_id='test-tenant'):
    """Directly insert an R6Resource row into the test DB."""
    with app.app_context():
        rj = json.dumps(resource_json_dict)
        row = R6Resource(
            resource_type=resource_type,
            resource_json=rj,
            resource_id=resource_json_dict.get('id'),
            tenant_id=tenant_id,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


# Minimal FHIR resources with subject references

PATIENT_ID = 'share-pt-001'

PATIENT_RESOURCE = {
    'resourceType': 'Patient',
    'id': PATIENT_ID,
    'name': [{'family': 'Vestel', 'given': ['Eugene']}],
    'birthDate': '1985-06-12',
    'gender': 'male',
    'identifier': [
        {'system': 'http://example.org/mrn', 'value': 'MRN99887766'}
    ],
    'telecom': [{'system': 'phone', 'value': '555-123-4567'}],
    'address': [{'line': ['100 Main St'], 'city': 'Austin', 'state': 'TX'}],
}

CONDITION_RESOURCE = {
    'resourceType': 'Condition',
    'id': 'share-cond-001',
    'clinicalStatus': {
        'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                    'code': 'active'}]
    },
    'code': {
        'coding': [{'system': 'http://snomed.info/sct', 'code': '44054006',
                    'display': 'Diabetes mellitus type 2'}]
    },
    'subject': {'reference': f'Patient/{PATIENT_ID}'},
}

OTHER_TENANT_CONDITION = {
    'resourceType': 'Condition',
    'id': 'other-tenant-cond',
    'code': {'coding': [{'system': 'http://snomed.info/sct', 'code': '123456'}]},
    'subject': {'reference': f'Patient/{PATIENT_ID}'},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_share_bundle_requires_step_up(client, tenant_headers):
    """Request without X-Step-Up-Token must get 401."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=tenant_headers,
        json={},
    )
    assert resp.status_code == 401
    data = resp.get_json()
    assert data['resourceType'] == 'OperationOutcome'
    assert any('step-up' in (i.get('diagnostics', '').lower()) for i in data['issue'])


def test_share_bundle_returns_collection(client, auth_headers, app):
    """Seeded Patient + Condition must appear in the returned collection Bundle."""
    _seed_resource(app, 'Patient', PATIENT_RESOURCE)
    _seed_resource(app, 'Condition', CONDITION_RESOURCE)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient', 'Condition']},
    )
    assert resp.status_code == 200
    assert resp.content_type == 'application/fhir+json'

    bundle = json.loads(resp.data)
    assert bundle['resourceType'] == 'Bundle'
    assert bundle['type'] == 'collection'
    assert 'timestamp' in bundle

    types_returned = {e['resource']['resourceType'] for e in bundle['entry']}
    assert 'Patient' in types_returned
    assert 'Condition' in types_returned
    assert len(bundle['entry']) == 2


def test_share_bundle_works_for_non_public_tenant_with_token(client, app):
    """The SHL feed must keep working for a real (non-public) tenant under the
    read-auth gate, as long as a valid step-up token is supplied. This is the
    path shl_generate drives: forward the patient's step-up token to
    $share-bundle. Both the read-auth gate and the route's own step-up check
    must accept it."""
    from r6.stepup import generate_step_up_token
    tenant = 'shl-private-clinic'  # NOT in PUBLIC_TENANTS
    _seed_resource(app, 'Patient', PATIENT_RESOURCE, tenant_id=tenant)
    _seed_resource(app, 'Condition', CONDITION_RESOURCE, tenant_id=tenant)

    headers = {
        'X-Tenant-Id': tenant,
        'X-Step-Up-Token': generate_step_up_token(tenant),
    }
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient', 'Condition']},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert {e['resource']['resourceType'] for e in bundle['entry']} == {'Patient', 'Condition'}


def test_share_bundle_non_public_tenant_rejected_without_token(client, app):
    """A non-public tenant with NO token is stopped by the read-auth gate (401)
    before the route runs — the SHL feed never leaks identified data to an
    unauthenticated caller."""
    tenant = 'shl-private-clinic'
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers={'X-Tenant-Id': tenant},
        json={},
    )
    assert resp.status_code == 401


def test_share_bundle_patient_controlled_redaction_applied(client, auth_headers, app):
    """Deidentified profile: name/telecom/address removed, birthDate preserved."""
    _seed_resource(app, 'Patient', PATIENT_RESOURCE)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient'], 'profile': 'deidentified'},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert len(bundle['entry']) == 1

    patient = bundle['entry'][0]['resource']
    # name, telecom, address should be stripped
    assert 'name' not in patient
    assert 'telecom' not in patient
    assert 'address' not in patient
    # birthDate preserved
    assert patient.get('birthDate') == '1985-06-12'
    # healthclaw canonical identifier injected
    systems = [i['system'] for i in patient.get('identifier', [])]
    assert 'https://healthclaw.io/patient-id' in systems
    # meta.tag stamped
    tags = patient.get('meta', {}).get('tag', [])
    codes = {t.get('code') for t in tags}
    assert 'patient-controlled' in codes


def test_share_bundle_intake_profile_keeps_demographics(client, auth_headers, app):
    """Default (intake) profile: Patient.name survives and meta.tag contains intake-identified."""
    _seed_resource(app, 'Patient', PATIENT_RESOURCE)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient']},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert len(bundle['entry']) == 1

    patient = bundle['entry'][0]['resource']
    # Demographics must be preserved
    assert 'name' in patient
    assert patient['name'][0]['family'] == 'Vestel'
    # meta.tag must flag identified share
    tags = patient.get('meta', {}).get('tag', [])
    tagged_systems = {t.get('system') for t in tags}
    tagged_codes = {t.get('code') for t in tags}
    assert 'https://healthclaw.io/share-profile' in tagged_systems
    assert 'intake-identified' in tagged_codes


def test_share_bundle_bogus_profile_400(client, auth_headers):
    """Unknown profile value must return 400 OperationOutcome."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'resource_types': ['Patient'], 'profile': 'bogus'},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['resourceType'] == 'OperationOutcome'
    assert 'bogus' in data['issue'][0]['diagnostics']


def test_share_bundle_rejects_unknown_type(client, auth_headers):
    """resource_types containing an unsupported type must return 400."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'resource_types': ['Teleport']},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['resourceType'] == 'OperationOutcome'
    diag = data['issue'][0]['diagnostics']
    assert 'Teleport' in diag


def test_share_bundle_tenant_isolation(client, auth_headers, app):
    """Resource belonging to a different tenant must not appear in the bundle."""
    # Seed a condition under a different tenant
    _seed_resource(app, 'Condition', OTHER_TENANT_CONDITION, tenant_id='other-tenant')

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,  # scoped to 'test-tenant'
        json={'resource_types': ['Condition']},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    ids = [e['resource'].get('id') for e in bundle['entry']]
    assert 'other-tenant-cond' not in ids


def test_share_bundle_emits_audit(client, auth_headers, app):
    """An AuditEventRecord must be written; detail has counts, no patient name."""
    _seed_resource(app, 'Patient', PATIENT_RESOURCE)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient']},
    )
    assert resp.status_code == 200

    with app.app_context():
        events = AuditEventRecord.query.filter_by(
            tenant_id='test-tenant',
            resource_id='share-bundle',
        ).all()
        assert len(events) >= 1
        detail = events[-1].detail or ''
        # Detail must contain count info
        assert 'resource' in detail
        # Detail must NOT contain the patient's name
        assert 'Vestel' not in detail
        assert 'Eugene' not in detail


def test_share_bundle_empty_ok(client, auth_headers):
    """When no resources match, return an empty collection Bundle (200)."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'resource_types': ['Coverage']},  # nothing seeded
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert bundle['resourceType'] == 'Bundle'
    assert bundle['type'] == 'collection'
    assert bundle['entry'] == []


def test_share_bundle_default_types(client, auth_headers, app):
    """Omitting resource_types uses the default SHL intake set (no 400)."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert bundle['resourceType'] == 'Bundle'
    assert bundle['type'] == 'collection'


def test_share_bundle_invalid_resource_types_not_list(client, auth_headers):
    """resource_types must be a list, not a string."""
    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'resource_types': 'Patient'},  # string, not list
    )
    assert resp.status_code == 400


def test_intake_profile_strips_ssn_note_text_keeps_name_and_mrn(client, auth_headers, app):
    """Intake profile must strip SSN-class identifiers, note, and text but keep
    name/demographics and non-SSN identifiers (e.g. MRN)."""
    patient_with_ssn = {
        'resourceType': 'Patient',
        'id': 'ssn-strip-pt-001',
        'name': [{'family': 'Smith', 'given': ['Alice']}],
        'birthDate': '1990-03-15',
        'identifier': [
            {'system': 'http://example.org/mrn', 'value': 'MRN-12345'},
            {'system': 'http://hl7.org/fhir/sid/us-ssn', 'value': '123-45-6789'},
        ],
        'note': [{'text': 'Patient is anxious about procedures.'}],
        'text': {'status': 'generated', 'div': '<div>Alice Smith</div>'},
    }
    _seed_resource(app, 'Patient', patient_with_ssn)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': 'ssn-strip-pt-001', 'resource_types': ['Patient']},
        # profile defaults to 'intake'
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)
    assert len(bundle['entry']) == 1

    patient = bundle['entry'][0]['resource']

    # Name must be preserved (intake = identified)
    assert 'name' in patient
    assert patient['name'][0]['family'] == 'Smith'

    # birthDate must be preserved
    assert patient.get('birthDate') == '1990-03-15'

    # SSN identifier must be stripped
    identifiers = patient.get('identifier', [])
    systems = [i.get('system') for i in identifiers]
    assert 'http://hl7.org/fhir/sid/us-ssn' not in systems

    # Non-SSN (MRN) identifier must be kept
    assert 'http://example.org/mrn' in systems

    # note and text must be stripped
    assert 'note' not in patient
    assert 'text' not in patient

    # meta.tag must be stamped
    tags = patient.get('meta', {}).get('tag', [])
    assert any(t.get('code') == 'intake-identified' for t in tags)


def test_coverage_beneficiary_survives_patient_filter(client, auth_headers, app):
    """Coverage resources that reference the patient via beneficiary.reference
    must survive the patient_id filter (not just subject/patient references)."""
    coverage = {
        'resourceType': 'Coverage',
        'id': 'cov-beneficiary-001',
        'status': 'active',
        'beneficiary': {'reference': f'Patient/{PATIENT_ID}'},
        'payor': [{'display': 'ACME Insurance'}],
    }
    _seed_resource(app, 'Patient', PATIENT_RESOURCE)
    _seed_resource(app, 'Coverage', coverage)

    resp = client.post(
        '/r6/fhir/$share-bundle',
        headers=auth_headers,
        json={'patient_id': PATIENT_ID, 'resource_types': ['Patient', 'Coverage']},
    )
    assert resp.status_code == 200
    bundle = json.loads(resp.data)

    ids = [e['resource'].get('id') for e in bundle['entry']]
    assert 'cov-beneficiary-001' in ids
