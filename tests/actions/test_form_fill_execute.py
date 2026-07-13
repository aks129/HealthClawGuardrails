"""form-fill execute() end-to-end (Task 8).

execute() is now the real orchestration: reviewed QuestionnaireResponse ->
rendered PDF -> FHIR DocumentReference -> signed download link. These tests
pin the safety contract branch-by-branch (fail loud, never fabricate a
completed form) plus one full propose -> review -> confirm -> download
integration path on synthetic data with the allergy attestation satisfied.
"""
import json

from models import db
from r6.actions.models import ProposedAction
from r6.actions.rails.form_fill import FormFillExecutor
from r6.actions import errors
from r6.models import R6Resource
from r6.sdc.documents import get_document_pdf_bytes

# Reuse the review-flow fixtures/helpers/constants verbatim.
from tests.actions.test_review_flow import (
    PATIENT, MED_A, ALLERGY_A, _seed, _staged_form_fill,
)

BASE_URL = 'https://example.test'


def _reviewed_qr(subject_id='test-patient-1', nka=True):
    """A completed QuestionnaireResponse of the shape the review page persists:
    a subject, one medication, and the explicit NKA attestation boolean."""
    qr = {
        'resourceType': 'QuestionnaireResponse',
        'status': 'completed',
        'questionnaire': 'healthclaw-intake',
        'authored': '2026-07-11T12:00:00Z',
        'subject': {'reference': 'Patient/%s' % subject_id},
        'item': [
            {'linkId': 'medications', 'item': [
                {'linkId': 'medications.med', 'text': 'Metformin 500 mg tablet',
                 'answer': [{'valueString': 'Metformin 500 mg tablet'}]},
            ]},
            {'linkId': 'allergies', 'item': [
                {'linkId': 'allergies.no-known-allergies',
                 'answer': [{'valueBoolean': nka}]},
            ]},
        ],
    }
    return qr


def _persist_qr(tenant, qr):
    row = R6Resource(resource_type='QuestionnaireResponse',
                     resource_json=json.dumps(qr), tenant_id=tenant)
    db.session.add(row)
    db.session.commit()
    return row.id


def _action(tenant, payload):
    return ProposedAction(tenant_id=tenant, kind='form-fill', payload=payload)


# ---------------------------------------------------------------------------
# Unit-level: the four safety branches of execute()
# ---------------------------------------------------------------------------

def test_execute_without_public_base_url_fails_loud(app, tenant_id, monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    with app.app_context():
        action = _action(tenant_id, {'questionnaire': 'healthclaw-intake',
                                     'body': 'x', 'reviewed_qr_id': 'whatever'})
        result = FormFillExecutor().execute(action)
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_execute_without_reviewed_qr_is_needs_review(app, tenant_id, monkeypatch):
    """No reviewed QR -> the form was never human-reviewed. Never 'completed'."""
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE_URL)
    with app.app_context():
        action = _action(tenant_id, {'questionnaire': 'healthclaw-intake',
                                     'body': 'x'})
        result = FormFillExecutor().execute(action)
    assert result.status == 'needs_review'
    assert result.status != 'completed'


def test_execute_missing_qr_row_is_stale_source_data(app, tenant_id, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE_URL)
    with app.app_context():
        action = _action(tenant_id, {'questionnaire': 'healthclaw-intake',
                                     'body': 'x',
                                     'reviewed_qr_id': 'does-not-exist'})
        result = FormFillExecutor().execute(action)
    assert result.status == 'failed'
    assert result.error == errors.STALE_SOURCE_DATA


def test_execute_happy_path_renders_persists_and_links(app, tenant_id,
                                                       monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE_URL)
    with app.app_context():
        qr_id = _persist_qr(tenant_id, _reviewed_qr())
        action = _action(tenant_id, {'questionnaire': 'healthclaw-intake',
                                     'body': 'x', 'reviewed_qr_id': qr_id})
        result = FormFillExecutor().execute(action)

        assert result.status == 'completed', result.outcome
        docref_id = result.provider_ref
        assert docref_id
        assert result.outcome['document_reference_id'] == docref_id
        assert result.outcome['questionnaire_response_id'] == qr_id
        link = result.outcome['delivery_link']
        assert '/r6/sdc/documents/%s' % docref_id in link
        assert link.startswith(BASE_URL)

        # The DocumentReference exists for the tenant and carries real PDF bytes.
        pdf = get_document_pdf_bytes(tenant_id, docref_id)
        assert pdf and pdf.startswith(b'%PDF')


# ---------------------------------------------------------------------------
# End-to-end: propose -> review (confirm the allergy) -> confirm -> download
# ---------------------------------------------------------------------------

def test_end_to_end_propose_review_confirm_download(client, app, tenant_headers,
                                                    auth_headers, action_registry,
                                                    monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', BASE_URL)
    tenant = tenant_headers['X-Tenant-Id']
    # Real allergy (Penicillin) so the attestation is satisfied by CONFIRMING it
    # (not by NKA) — the human affirmatively confirms the allergy row.
    _seed(app, tenant, [PATIENT, MED_A, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    # Review: act on the med and CONFIRM the real allergy (no NKA needed).
    review = client.post('/r6/actions/%s/review' % action_id,
                         headers=auth_headers,
                         json={'med-0': 'yes', 'allergy-0': 'confirm'})
    assert review.status_code == 200, review.get_data(as_text=True)
    assert review.get_json()['reviewed_qr_id']

    # Out-of-band confirm — mirrors tests/actions/test_confirm_is_commit.py::
    # _confirm (auth_headers carry a valid step-up token; commit/review are
    # multi-use, confirm consumes the nonce).
    confirm = client.post('/r6/actions/%s/confirm' % action_id,
                         headers=auth_headers, json={})
    assert confirm.status_code == 200, confirm.get_data(as_text=True)
    assert confirm.get_json()['status'] == 'completed'

    # Pull the delivery link out of the persisted outcome.
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'completed'
        outcome = json.loads(row.outcome_summary)
        link = outcome['delivery_link']
        docref_id = outcome['document_reference_id']
        assert row.external_ref == docref_id

    # GET the signed link (path + query) — the signature IS the credential, so
    # no tenant/step-up headers are supplied.
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(link)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    resp = client.get(parsed.path, query_string=query)
    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data.startswith(b'%PDF')
