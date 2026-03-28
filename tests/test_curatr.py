"""
Tests for Curatr data quality evaluation and fix application.

Covers:
- CuratrEngine.evaluate() for Condition resources (offline — no network calls)
- Flask $curatr-evaluate endpoint
- Flask $curatr-apply-fix endpoint
- Provenance resource creation on fix
- _apply_field_fix helper
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from r6.curatr import (
    CuratrEngine,
    CuratrIssue,
    CuratrResult,
    apply_fix,
    _apply_field_fix,
    DEPRECATED_SYSTEMS,
)


# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture
def engine():
    return CuratrEngine(timeout=1)


@pytest.fixture
def icd9_condition():
    """Condition with a deprecated ICD-9 code — no network needed."""
    return {
        "resourceType": "Condition",
        "id": "cond-icd9",
        "code": {
            "coding": [{
                "system": "http://hl7.org/fhir/sid/icd-9-cm",
                "code": "250.00",
                "display": "Diabetes mellitus without complication",
            }]
        },
        "subject": {"reference": "Patient/pt-1"},
    }


@pytest.fixture
def good_condition():
    """Condition with a valid ICD-10-CM code and all recommended fields."""
    return {
        "resourceType": "Condition",
        "id": "cond-good",
        "clinicalStatus": {
            "coding": [{
                "system": (
                    "http://terminology.hl7.org/CodeSystem/"
                    "condition-clinical"
                ),
                "code": "active",
            }]
        },
        "verificationStatus": {
            "coding": [{
                "system": (
                    "http://terminology.hl7.org/CodeSystem/"
                    "condition-ver-status"
                ),
                "code": "confirmed",
            }]
        },
        "code": {
            "coding": [{
                "system": "http://hl7.org/fhir/sid/icd-10-cm",
                "code": "E11.9",
                "display": (
                    "Type 2 diabetes mellitus without complications"
                ),
            }]
        },
        "subject": {"reference": "Patient/pt-1"},
    }


@pytest.fixture
def minimal_condition():
    """Condition missing clinicalStatus and verificationStatus."""
    return {
        "resourceType": "Condition",
        "id": "cond-min",
        "code": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "73211009",
                "display": "Diabetes mellitus",
            }]
        },
        "subject": {"reference": "Patient/pt-1"},
    }


@pytest.fixture
def sample_condition_json(good_condition):
    return json.dumps(good_condition, separators=(',', ':'), sort_keys=True)


# ------------------------------------------------------------------ #
# CuratrEngine unit tests (no network)                               #
# ------------------------------------------------------------------ #

class TestCuratrEngineOffline:
    """Tests that use mocked or no terminology lookups."""

    def test_icd9_flagged_as_critical(self, engine, icd9_condition):
        # Patch lookup so the deprecated-system check is the only issue
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(icd9_condition)

        assert isinstance(result, CuratrResult)
        critical = [i for i in result.issues if i.severity == "critical"]
        assert len(critical) >= 1
        assert any("ICD-9" in i.plain_language for i in critical)
        assert result.overall_quality == "critical"

    def test_missing_clinical_status_is_warning(self, engine, minimal_condition):
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(minimal_condition)

        paths = [i.field_path for i in result.issues]
        assert any("clinicalStatus" in p for p in paths)
        warn = [i for i in result.issues if "clinicalStatus" in i.field_path]
        assert warn[0].severity == "warning"

    def test_missing_verification_status_is_info(self, engine, minimal_condition):
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(minimal_condition)

        info = [
            i for i in result.issues
            if "verificationStatus" in i.field_path
        ]
        assert info[0].severity == "info"

    def test_good_condition_no_deprecated_issues(self, engine, good_condition):
        # Mock lookup returning valid to avoid network
        with patch.object(
            engine, '_lookup_code',
            return_value={"valid": True, "display": None, "message": None}
        ):
            result = engine.evaluate(good_condition)

        deprecated = [
            i for i in result.issues if i.title == "Outdated code system"
        ]
        assert len(deprecated) == 0

    def test_invalid_clinical_status_code(self, engine):
        cond = {
            "resourceType": "Condition",
            "id": "cond-bad-cs",
            "clinicalStatus": {
                "coding": [{"system": "http://example.org", "code": "ongoing"}]
            },
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "73211009"}]},
            "subject": {"reference": "Patient/pt-1"},
        }
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(cond)

        bad_cs = [
            i for i in result.issues
            if "clinicalStatus" in i.field_path and i.severity == "warning"
        ]
        assert len(bad_cs) == 1
        assert "ongoing" in bad_cs[0].plain_language

    def test_display_mismatch_is_suggestion(self, engine):
        cond = {
            "resourceType": "Condition",
            "id": "cond-disp",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "verificationStatus": {"coding": [{"code": "confirmed"}]},
            "code": {
                "coding": [{
                    "system": "http://hl7.org/fhir/sid/icd-10-cm",
                    "code": "E11.9",
                    "display": "Diabetes Type 2",  # wrong display
                }]
            },
            "subject": {"reference": "Patient/pt-1"},
        }
        with patch.object(
            engine, '_lookup_code',
            return_value={
                "valid": True,
                "display": (
                    "Type 2 diabetes mellitus without complications"
                ),
                "message": None,
            }
        ):
            result = engine.evaluate(cond)

        suggestions = [i for i in result.issues if i.severity == "suggestion"]
        assert len(suggestions) >= 1
        assert "Type 2 diabetes mellitus" in suggestions[0].suggestion

    def test_no_code_element_produces_critical(self, engine):
        cond = {
            "resourceType": "Condition",
            "id": "cond-nocode",
            "subject": {"reference": "Patient/pt-1"},
        }
        result = engine.evaluate(cond)
        critical = [i for i in result.issues if i.severity == "critical"]
        assert any("code" in i.field_path for i in critical)

    def test_summary_reflects_issue_count(self, engine, icd9_condition):
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(icd9_condition)
        assert str(len(result.issues)) in result.summary or "critical" in result.summary

    def test_result_to_dict(self, engine, icd9_condition):
        with patch.object(engine, '_lookup_code', return_value=None):
            result = engine.evaluate(icd9_condition)
        d = result.to_dict()
        assert d["resource_type"] == "Condition"
        assert d["resource_id"] == "cond-icd9"
        assert "issues" in d
        assert "overall_quality" in d
        assert "checked_at" in d


# ------------------------------------------------------------------ #
# _apply_field_fix helper                                             #
# ------------------------------------------------------------------ #

class TestApplyFieldFix:

    def test_simple_top_level_field(self):
        resource = {"status": "old"}
        assert _apply_field_fix(resource, "status", "new") is True
        assert resource["status"] == "new"

    def test_nested_field(self):
        resource = {"code": {"text": "old text"}}
        assert _apply_field_fix(resource, "code.text", "new text") is True
        assert resource["code"]["text"] == "new text"

    def test_array_index(self):
        resource = {"coding": [{"code": "old"}]}
        assert _apply_field_fix(resource, "coding[0].code", "new") is True
        assert resource["coding"][0]["code"] == "new"

    def test_strips_resource_type_prefix(self):
        resource = {"code": {"text": "old"}}
        assert _apply_field_fix(resource, "Condition.code.text", "new") is True
        assert resource["code"]["text"] == "new"

    def test_out_of_bounds_array_returns_false(self):
        resource = {"coding": []}
        assert _apply_field_fix(resource, "coding[5].code", "new") is False

    def test_missing_parent_creates_dict(self):
        resource = {}
        assert _apply_field_fix(resource, "code.text", "hello") is True
        assert resource["code"]["text"] == "hello"


# ------------------------------------------------------------------ #
# Flask endpoint tests                                                #
# ------------------------------------------------------------------ #

class TestCuratrEvaluateEndpoint:

    def test_evaluate_returns_200(
        self, client, tenant_headers, step_up_token, auth_headers,
        good_condition, sample_condition_json
    ):
        # Create a Condition resource first
        good_condition['id'] = 'cond-eval-1'
        resp = client.post(
            '/r6/fhir/Condition',
            json=good_condition,
            headers={
                **auth_headers,
                'X-Human-Confirmed': 'true',
            },
        )
        assert resp.status_code == 201, resp.get_json()
        resource_id = resp.get_json()['id']

        with patch('r6.curatr.CuratrEngine._lookup_code',
                   return_value={"valid": True, "display": None, "message": None}):
            resp = client.get(
                f'/r6/fhir/Condition/{resource_id}/$curatr-evaluate',
                headers=tenant_headers,
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['resource_type'] == 'Condition'
        assert 'issues' in body
        assert 'overall_quality' in body

    def test_evaluate_404_for_missing_resource(self, client, tenant_headers):
        resp = client.get(
            '/r6/fhir/Condition/does-not-exist/$curatr-evaluate',
            headers=tenant_headers,
        )
        assert resp.status_code == 404

    def test_evaluate_requires_tenant_header(self, client):
        resp = client.get('/r6/fhir/Condition/x/$curatr-evaluate')
        assert resp.status_code == 400

    def test_evaluate_unsupported_type_returns_400(self, client, tenant_headers):
        resp = client.get(
            '/r6/fhir/FakeResource/x/$curatr-evaluate',
            headers=tenant_headers,
        )
        assert resp.status_code == 400


class TestCuratrApplyFixEndpoint:

    def _create_icd9_condition(self, client, auth_headers):
        condition = {
            "resourceType": "Condition",
            "code": {
                "coding": [{
                    "system": "http://hl7.org/fhir/sid/icd-9-cm",
                    "code": "250.00",
                    "display": "Diabetes mellitus without complication",
                }]
            },
            "subject": {"reference": "Patient/pt-1"},
        }
        resp = client.post(
            '/r6/fhir/Condition',
            json=condition,
            headers={**auth_headers, 'X-Human-Confirmed': 'true'},
        )
        assert resp.status_code == 201, resp.get_json()
        return resp.get_json()['id']

    def test_apply_fix_succeeds(self, client, auth_headers, tenant_headers):
        resource_id = self._create_icd9_condition(client, auth_headers)

        fixes = [
            {
                "field_path": "Condition.code.coding[0].system",
                "new_value": "http://hl7.org/fhir/sid/icd-10-cm",
            },
            {
                "field_path": "Condition.code.coding[0].code",
                "new_value": "E11.9",
            },
        ]
        resp = client.post(
            f'/r6/fhir/Condition/{resource_id}/$curatr-apply-fix',
            json={"fixes": fixes, "patient_intent": "Updating ICD-9 to ICD-10"},
            headers={
                **auth_headers,
                'X-Human-Confirmed': 'true',
            },
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body['issues_fixed'] == 2
        assert 'provenance' in body
        assert body['provenance']['resourceType'] == 'Provenance'
        # Check provenance target points to the fixed resource
        target_ref = body['provenance']['target'][0]['reference']
        assert resource_id in target_ref

    def test_apply_fix_requires_step_up(self, client, tenant_headers):
        resp = client.post(
            '/r6/fhir/Condition/some-id/$curatr-apply-fix',
            json={"fixes": [], "patient_intent": "test"},
            headers={**tenant_headers, 'X-Human-Confirmed': 'true'},
        )
        assert resp.status_code == 403

    def test_apply_fix_empty_fixes_returns_400(
        self, client, auth_headers
    ):
        resp = client.post(
            '/r6/fhir/Condition/some-id/$curatr-apply-fix',
            json={"fixes": [], "patient_intent": "test"},
            headers={
                **auth_headers,
                'X-Human-Confirmed': 'true',
            },
        )
        assert resp.status_code == 400

    def test_apply_fix_creates_provenance_in_store(
        self, client, auth_headers, tenant_headers
    ):
        resource_id = self._create_icd9_condition(client, auth_headers)

        fixes = [{
            "field_path": "Condition.code.coding[0].code",
            "new_value": "E11.9",
        }]
        resp = client.post(
            f'/r6/fhir/Condition/{resource_id}/$curatr-apply-fix',
            json={"fixes": fixes, "patient_intent": "Fix ICD code"},
            headers={
                **auth_headers,
                'X-Human-Confirmed': 'true',
            },
        )
        assert resp.status_code == 200
        prov_id = resp.get_json()['provenance']['id']

        # Verify Provenance is readable
        prov_resp = client.get(
            f'/r6/fhir/Provenance/{prov_id}',
            headers=tenant_headers,
        )
        assert prov_resp.status_code == 200
        prov = prov_resp.get_json()
        assert prov['resourceType'] == 'Provenance'


# ------------------------------------------------------------------ #
# Condition CRUD tests                                                #
# ------------------------------------------------------------------ #

class TestConditionCRUD:

    def test_create_condition(self, client, auth_headers):
        condition = {
            "resourceType": "Condition",
            "code": {
                "coding": [{
                    "system": "http://snomed.info/sct",
                    "code": "73211009",
                }]
            },
            "subject": {"reference": "Patient/pt-1"},
        }
        resp = client.post(
            '/r6/fhir/Condition',
            json=condition,
            headers={**auth_headers, 'X-Human-Confirmed': 'true'},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body['resourceType'] == 'Condition'
        assert 'id' in body

    def test_read_condition(self, client, auth_headers, tenant_headers):
        condition = {
            "resourceType": "Condition",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "73211009"}]},
            "subject": {"reference": "Patient/pt-1"},
        }
        create = client.post(
            '/r6/fhir/Condition',
            json=condition,
            headers={**auth_headers, 'X-Human-Confirmed': 'true'},
        )
        assert create.status_code == 201
        resource_id = create.get_json()['id']

        read = client.get(
            f'/r6/fhir/Condition/{resource_id}',
            headers=tenant_headers,
        )
        assert read.status_code == 200
        assert read.get_json()['resourceType'] == 'Condition'

    def test_condition_requires_code_field(self, client, auth_headers):
        condition = {
            "resourceType": "Condition",
            "subject": {"reference": "Patient/pt-1"},
        }
        resp = client.post(
            '/r6/fhir/Condition',
            json=condition,
            headers={**auth_headers, 'X-Human-Confirmed': 'true'},
        )
        # Should fail validation (code is required)
        assert resp.status_code == 422
