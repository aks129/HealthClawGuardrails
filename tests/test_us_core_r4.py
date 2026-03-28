"""
Tests for US Core v9 R4 resource support.

Covers CRUD, structural validation, and Curatr evaluation for the
Phase 4 US Core v9 resource types added alongside FHIR R6 ballot3.
"""

import json
import pytest


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def post_resource(client, resource, headers):
    """POST a resource; adds X-Human-Confirmed for clinical writes."""
    rt = resource.get('resourceType', 'Unknown')
    merged = dict(headers)
    merged['X-Human-Confirmed'] = 'true'
    resp = client.post(
        f'/r6/fhir/{rt}',
        data=json.dumps(resource),
        content_type='application/fhir+json',
        headers=merged,
    )
    return resp, resp.get_json()


def validate_resource(client, resource, headers):
    """POST to $validate; returns (response, data)."""
    rt = resource.get('resourceType', 'Unknown')
    resp = client.post(
        f'/r6/fhir/{rt}/$validate',
        data=json.dumps(resource),
        content_type='application/fhir+json',
        headers=headers,
    )
    return resp, resp.get_json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_allergy(tenant_id):
    cs_system = (
        'http://terminology.hl7.org/CodeSystem/'
        'allergyintolerance-clinical'
    )
    vs_system = (
        'http://terminology.hl7.org/CodeSystem/'
        'allergyintolerance-verification'
    )
    rxnorm = 'http://www.nlm.nih.gov/research/umls/rxnorm'
    return {
        'resourceType': 'AllergyIntolerance',
        'clinicalStatus': {
            'coding': [{'system': cs_system, 'code': 'active'}]
        },
        'verificationStatus': {
            'coding': [{'system': vs_system, 'code': 'confirmed'}]
        },
        'patient': {'reference': f'Patient/pt-{tenant_id}'},
        'code': {
            'coding': [
                {
                    'system': rxnorm,
                    'code': '7980',
                    'display': 'Penicillin',
                }
            ]
        },
    }


@pytest.fixture
def sample_immunization(tenant_id):
    cvx = 'http://hl7.org/fhir/sid/cvx'
    return {
        'resourceType': 'Immunization',
        'status': 'completed',
        'vaccineCode': {
            'coding': [
                {'system': cvx, 'code': '140',
                 'display': 'Influenza, seasonal'}
            ]
        },
        'patient': {'reference': f'Patient/pt-{tenant_id}'},
        'occurrenceDateTime': '2024-10-01',
    }


@pytest.fixture
def sample_medication_request(tenant_id):
    rxnorm = 'http://www.nlm.nih.gov/research/umls/rxnorm'
    return {
        'resourceType': 'MedicationRequest',
        'status': 'active',
        'intent': 'order',
        'medicationCodeableConcept': {
            'coding': [
                {
                    'system': rxnorm,
                    'code': '308460',
                    'display': 'Amoxicillin 500 MG Oral Capsule',
                }
            ]
        },
        'subject': {'reference': f'Patient/pt-{tenant_id}'},
    }


@pytest.fixture
def sample_procedure(tenant_id):
    snomed = 'http://snomed.info/sct'
    return {
        'resourceType': 'Procedure',
        'status': 'completed',
        'code': {
            'coding': [
                {'system': snomed, 'code': '80146002',
                 'display': 'Appendectomy'}
            ]
        },
        'subject': {'reference': f'Patient/pt-{tenant_id}'},
    }


@pytest.fixture
def sample_diagnostic_report(tenant_id):
    return {
        'resourceType': 'DiagnosticReport',
        'status': 'final',
        'code': {
            'coding': [
                {'system': 'http://loinc.org',
                 'code': '58410-2', 'display': 'CBC panel'}
            ]
        },
        'subject': {'reference': f'Patient/pt-{tenant_id}'},
    }


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

class TestAllergyIntoleranceCRUD:
    def test_create_allergy(self, client, auth_headers, sample_allergy):
        resp, data = post_resource(client, sample_allergy, auth_headers)
        assert resp.status_code == 201
        assert data['resourceType'] == 'AllergyIntolerance'
        assert 'id' in data

    def test_read_allergy(
        self, client, auth_headers, tenant_headers, sample_allergy
    ):
        _, created = post_resource(client, sample_allergy, auth_headers)
        rid = created['id']
        resp = client.get(
            f'/r6/fhir/AllergyIntolerance/{rid}',
            headers=tenant_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()['resourceType'] == 'AllergyIntolerance'

    def test_validate_allergy_passes(
        self, client, auth_headers, sample_allergy
    ):
        resp, data = validate_resource(
            client, sample_allergy, auth_headers
        )
        # valid resource: 200 OK
        assert resp.status_code == 200
        assert data['resourceType'] == 'OperationOutcome'
        errors = [
            i for i in data.get('issue', [])
            if i['severity'] in ('error', 'fatal')
        ]
        assert errors == []


class TestImmunizationCRUD:
    def test_create_immunization(
        self, client, auth_headers, sample_immunization
    ):
        resp, data = post_resource(
            client, sample_immunization, auth_headers
        )
        assert resp.status_code == 201
        assert data['resourceType'] == 'Immunization'

    def test_validate_immunization_missing_status(
        self, client, auth_headers, tenant_id
    ):
        cvx = 'http://hl7.org/fhir/sid/cvx'
        bad = {
            'resourceType': 'Immunization',
            'vaccineCode': {
                'coding': [{'system': cvx, 'code': '140'}]
            },
            'patient': {'reference': f'Patient/pt-{tenant_id}'},
            'occurrenceDateTime': '2024-10-01',
        }
        resp, data = validate_resource(client, bad, auth_headers)
        # invalid resource: 422 Unprocessable Entity
        assert resp.status_code == 422
        errors = [
            i for i in data.get('issue', [])
            if i['severity'] in ('error', 'fatal')
        ]
        assert any('status' in e['diagnostics'] for e in errors)


class TestMedicationRequestCRUD:
    def test_create_medication_request(
        self, client, auth_headers, sample_medication_request
    ):
        resp, data = post_resource(
            client, sample_medication_request, auth_headers
        )
        assert resp.status_code == 201
        assert data['resourceType'] == 'MedicationRequest'

    def test_validate_missing_intent(
        self, client, auth_headers, tenant_id
    ):
        rxnorm = 'http://www.nlm.nih.gov/research/umls/rxnorm'
        bad = {
            'resourceType': 'MedicationRequest',
            'status': 'active',
            'medicationCodeableConcept': {
                'coding': [{'system': rxnorm, 'code': '308460'}]
            },
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        }
        resp, data = validate_resource(client, bad, auth_headers)
        assert resp.status_code == 422
        errors = [
            i for i in data.get('issue', [])
            if i['severity'] in ('error', 'fatal')
        ]
        assert any('intent' in e['diagnostics'] for e in errors)

    def test_search_medication_requests(
        self, client, auth_headers, tenant_headers,
        sample_medication_request
    ):
        post_resource(client, sample_medication_request, auth_headers)
        resp = client.get(
            '/r6/fhir/MedicationRequest', headers=tenant_headers
        )
        assert resp.status_code == 200
        assert resp.get_json()['resourceType'] == 'Bundle'


class TestProcedureCRUD:
    def test_create_procedure(
        self, client, auth_headers, sample_procedure
    ):
        resp, data = post_resource(client, sample_procedure, auth_headers)
        assert resp.status_code == 201
        assert data['resourceType'] == 'Procedure'

    def test_validate_missing_code(
        self, client, auth_headers, tenant_id
    ):
        bad = {
            'resourceType': 'Procedure',
            'status': 'completed',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        }
        resp, data = validate_resource(client, bad, auth_headers)
        assert resp.status_code == 422
        errors = [
            i for i in data.get('issue', [])
            if i['severity'] in ('error', 'fatal')
        ]
        assert any('code' in e['diagnostics'] for e in errors)


class TestDiagnosticReportCRUD:
    def test_create_diagnostic_report(
        self, client, auth_headers, sample_diagnostic_report
    ):
        resp, data = post_resource(
            client, sample_diagnostic_report, auth_headers
        )
        assert resp.status_code == 201
        assert data['resourceType'] == 'DiagnosticReport'


# ---------------------------------------------------------------------------
# US Core resource type smoke tests
# ---------------------------------------------------------------------------

class TestUSCoreSmoke:
    """Quick create/read smoke tests for Phase 4 resource types."""

    def _smoke(self, client, auth_headers, tenant_headers, resource):
        resp, data = post_resource(client, resource, auth_headers)
        rt = resource['resourceType']
        assert resp.status_code == 201, (
            f"Create failed for {rt}: {data}"
        )
        rid = data['id']
        get_resp = client.get(
            f"/r6/fhir/{rt}/{rid}", headers=tenant_headers,
        )
        assert get_resp.status_code == 200

    def test_goal(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Goal',
            'lifecycleStatus': 'active',
            'description': {'text': 'Reduce HbA1c below 7%'},
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_care_plan(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'CarePlan',
            'status': 'active',
            'intent': 'plan',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_care_team(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'CareTeam',
            'status': 'active',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_document_reference(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'DocumentReference',
            'status': 'current',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
            'content': [
                {'attachment': {
                    'contentType': 'text/plain',
                    'data': 'SGVsbG8=',
                }}
            ],
        })

    def test_coverage(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Coverage',
            'status': 'active',
            'beneficiary': {
                'reference': f'Patient/pt-{tenant_id}'
            },
            'payor': [{'display': 'BlueCross'}],
        })

    def test_service_request(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'ServiceRequest',
            'status': 'active',
            'intent': 'order',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_location(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Location',
            'name': 'Main Clinic',
            'status': 'active',
        })

    def test_organization(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Organization',
            'name': 'Health Partners',
        })

    def test_practitioner(
        self, client, auth_headers, tenant_headers
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Practitioner',
            'name': [{'family': 'Smith', 'given': ['Jane']}],
        })

    def test_related_person(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'RelatedPerson',
            'patient': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_specimen(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'Specimen',
            'subject': {'reference': f'Patient/pt-{tenant_id}'},
        })

    def test_family_member_history(
        self, client, auth_headers, tenant_headers, tenant_id
    ):
        self._smoke(client, auth_headers, tenant_headers, {
            'resourceType': 'FamilyMemberHistory',
            'status': 'complete',
            'patient': {'reference': f'Patient/pt-{tenant_id}'},
            'relationship': {
                'coding': [{'code': 'FTH', 'display': 'Father'}]
            },
        })


# ---------------------------------------------------------------------------
# Curatr evaluation — US Core R4 resources (offline)
# ---------------------------------------------------------------------------

class TestCuratrUSCoreR4:
    """Curatr evaluation on US Core R4 resources — no network calls."""

    def test_allergy_with_deprecated_code_system(self):
        from r6.curatr import CuratrEngine
        engine = CuratrEngine()
        resource = {
            'resourceType': 'AllergyIntolerance',
            'id': 'ai-1',
            'clinicalStatus': {
                'coding': [{
                    'system': (
                        'http://terminology.hl7.org/CodeSystem/'
                        'allergyintolerance-clinical'
                    ),
                    'code': 'active',
                }]
            },
            'patient': {'reference': 'Patient/p1'},
            'code': {
                'coding': [{
                    'system': 'http://hl7.org/fhir/sid/icd-9-cm',
                    'code': 'V14.0',
                    'display': 'Penicillin allergy',
                }]
            },
        }
        result = engine.evaluate(resource)
        assert result.resource_type == 'AllergyIntolerance'
        critical = [i for i in result.issues if i.severity == 'critical']
        assert any(
            'deprecated' in i.title.lower()
            or 'icd-9' in i.plain_language.lower()
            for i in critical
        )

    def test_allergy_missing_required_fields(self):
        from r6.curatr import CuratrEngine
        engine = CuratrEngine()
        resource = {'resourceType': 'AllergyIntolerance', 'id': 'ai-2'}
        result = engine.evaluate(resource)
        assert result.overall_quality in ('critical', 'needs-review')
        paths = {i.field_path for i in result.issues}
        assert 'AllergyIntolerance.clinicalStatus' in paths
        assert 'AllergyIntolerance.patient' in paths

    def test_medication_request_missing_fields(self):
        from r6.curatr import CuratrEngine
        engine = CuratrEngine()
        resource = {'resourceType': 'MedicationRequest', 'id': 'mr-1'}
        result = engine.evaluate(resource)
        assert result.overall_quality in ('critical', 'needs-review')
        paths = {i.field_path for i in result.issues}
        assert 'MedicationRequest.status' in paths
        assert 'MedicationRequest.intent' in paths

    def test_immunization_missing_vaccine_code(self):
        from r6.curatr import CuratrEngine
        engine = CuratrEngine()
        resource = {
            'resourceType': 'Immunization',
            'id': 'imm-1',
            'status': 'completed',
            'patient': {'reference': 'Patient/p1'},
            'occurrenceDateTime': '2024-10-01',
        }
        result = engine.evaluate(resource)
        paths = {i.field_path for i in result.issues}
        assert 'Immunization.vaccineCode' in paths

    def test_procedure_good_quality(self):
        from r6.curatr import CuratrEngine
        from unittest.mock import patch
        engine = CuratrEngine()
        resource = {
            'resourceType': 'Procedure',
            'id': 'proc-1',
            'status': 'completed',
            'code': {
                'coding': [{
                    'system': 'http://snomed.info/sct',
                    'code': '80146002',
                    'display': 'Appendectomy',
                }]
            },
            'subject': {'reference': 'Patient/p1'},
        }
        with patch.object(engine, '_validate_tx_fhir', return_value=None), \
             patch.object(engine, '_validate_icd10_nlm', return_value=None):
            result = engine.evaluate(resource)
        assert result.resource_type == 'Procedure'

    def test_diagnostic_report_missing_status(self):
        from r6.curatr import CuratrEngine
        engine = CuratrEngine()
        resource = {
            'resourceType': 'DiagnosticReport',
            'id': 'dr-1',
            'code': {
                'coding': [{
                    'system': 'http://loinc.org',
                    'code': '58410-2',
                }]
            },
            'subject': {'reference': 'Patient/p1'},
        }
        result = engine.evaluate(resource)
        paths = {i.field_path for i in result.issues}
        assert 'DiagnosticReport.status' in paths


# ---------------------------------------------------------------------------
# Capability statement checks
# ---------------------------------------------------------------------------

class TestCapabilityStatement:
    def test_includes_us_core_resources(self, client):
        resp = client.get('/r6/fhir/metadata')
        assert resp.status_code == 200
        data = resp.get_json()
        resource_types = [
            r['type'] for r in data['rest'][0]['resource']
        ]
        for expected in [
            'AllergyIntolerance', 'Immunization',
            'MedicationRequest', 'Procedure',
            'DiagnosticReport', 'Coverage',
            'Goal', 'CarePlan',
        ]:
            assert expected in resource_types, (
                f'{expected} missing from CapabilityStatement'
            )

    def test_implementation_description_mentions_r4(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        desc = data['implementation']['description']
        assert 'US Core' in desc or 'R4' in desc

    def test_software_name(self, client):
        resp = client.get('/r6/fhir/metadata')
        data = resp.get_json()
        assert data['software']['name'] == 'HealthClaw Guardrails'
