"""The approve page for every kind but form-fill (#215, #413).

Rendered from the sealed payload and nothing else: what the person sees is
what the approval digest binds and what the executor runs. Its POST records
the review-page confirmation; execution still happens only on the confirm
route's claim.
"""
import json

import pytest

from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction
from r6.models import AuditEventRecord, R6Resource
from tests.approval_helpers import approval_headers

CANARY = 'CANARY-DISPLAY-2210'
RID = 'c-approve-1'


def _propose(client, tenant_headers, body):
    r = client.post('/r6/actions/propose', json=body, headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()['id']


def _staged(client, tenant_headers, auth_headers, body):
    action_id = _propose(client, tenant_headers, body)
    c = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert c.status_code == 202, c.get_data(as_text=True)
    return action_id


def _get(client, headers, action_id):
    return client.get('/r6/actions/%s/review' % action_id, headers=headers)


def _post(client, headers, action_id):
    return client.post('/r6/actions/%s/review' % action_id, json={'ack': 'true'},
                       headers=headers)


CURATR = {'kind': 'curatr-fix',
          'payload': {'to': 'Condition/%s' % RID,
                      'body': 'Mark this condition as resolved.',
                      'curatr_fix': {
                          'resource_type': 'Condition', 'resource_id': RID,
                          'record_version': 1,
                          'patient_intent': 'it cleared up last spring',
                          'fixes': [
                              {'field_path': 'Condition.clinicalStatus.coding[0].code',
                               'new_value': 'resolved'},
                              {'field_path': 'Condition.onsetDateTime',
                               'new_value': '2024-03-01'}]}}}

SMS = {'kind': 'sms',
       'payload': {'to': 'Dr. Smith', 'phone': '617-555-0100',
                   'body': 'Reminder: appointment Tuesday at 9.'}}


@pytest.fixture
def seeded(app, tenant_id, action_registry, monkeypatch):
    monkeypatch.setenv('CURATR_FIX_RAIL_ENABLED', '1')
    record = {'resourceType': 'Condition', 'id': RID,
              'subject': {'reference': 'Patient/alice'},
              'code': {'coding': [{'system': 'http://snomed.info/sct',
                                   'code': '44054006', 'display': CANARY}]},
              'clinicalStatus': {'coding': [{'code': 'active'}]}}
    with app.app_context():
        db.session.add(R6Resource('Condition', json.dumps(record),
                                  resource_id=RID, tenant_id=tenant_id))
        db.session.commit()


def test_the_page_shows_every_change_and_nothing_from_the_record(
        client, app, tenant_headers, auth_headers, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    r = _get(client, auth_headers, action_id)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Correction to your health record' in html
    assert 'Condition/%s' % RID in html
    assert 'record version 1' in html
    for fix in CURATR['payload']['curatr_fix']['fixes']:
        assert fix['field_path'] in html
        assert json.dumps(fix['new_value']) in html
    assert 'Mark this condition as resolved.' in html
    assert CANARY not in html                     # the proposal, not the record
    assert '/r6/actions/%s/review' % action_id in html   # what the relay rewrites


def test_a_phone_or_sms_request_shows_recipient_number_and_verbatim_text(
        client, app, tenant_headers, auth_headers, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, SMS)
    html = _get(client, auth_headers, action_id).get_data(as_text=True)
    assert 'Text message' in html
    assert 'Dr. Smith' in html and '617-555-0100' in html
    assert 'Reminder: appointment Tuesday at 9.' in html


def test_the_page_requires_the_step_up_credential(
        client, app, tenant_headers, auth_headers, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _get(client, tenant_headers, action_id).status_code == 401


def test_the_page_is_only_for_an_action_awaiting_confirmation(
        client, app, tenant_headers, auth_headers, other_tenant_headers, seeded):
    proposed = _propose(client, tenant_headers, CURATR)
    assert _get(client, auth_headers, proposed).status_code == 404
    staged = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _get(client, other_tenant_headers, staged).status_code in (401, 404)


def test_the_page_carries_no_credential(
        client, app, tenant_headers, auth_headers, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    html = _get(client, auth_headers, action_id).get_data(as_text=True)
    assert auth_headers['X-Step-Up-Token'] not in html


def test_the_render_is_audited_without_record_text(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    _get(client, auth_headers, action_id)
    with app.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type='ProposedAction',
            resource_id=action_id, event_type='read').all()
    assert rows and any('approve page rendered; kind=curatr-fix' == r.detail
                        for r in rows)
    assert not any(CANARY in (r.detail or '') for r in rows)


def test_submit_records_one_confirmation_over_the_sealed_payload_and_executes_nothing(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    r = _post(client, auth_headers, action_id)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['approved_via'] == 'review-page'
    assert body['status'] == 'awaiting_confirmation'   # the page does not execute
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        assert action.status == 'awaiting_confirmation'
        rows = ActionConfirmation.query.filter_by(action_id=action_id).all()
        assert len(rows) == 1
        assert rows[0].payload_digest
    got = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers).get_json()
    assert got['meta']['versionId'] == '1'
    assert got['clinicalStatus']['coding'][0]['code'] == 'active'

    again = _post(client, auth_headers, action_id)
    assert again.status_code == 409
    with app.app_context():
        assert ActionConfirmation.query.filter_by(action_id=action_id).count() == 1


def test_then_the_approve_credential_carries_out_exactly_what_was_shown(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _post(client, auth_headers, action_id).status_code == 200
    r = client.post('/r6/actions/%s/confirm' % action_id, json={},
                    headers=approval_headers(app, auth_headers, action_id))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['status'] == 'completed'
    got = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers).get_json()
    assert got['clinicalStatus']['coding'][0]['code'] == 'resolved'
    assert got['onsetDateTime'] == '2024-03-01'
    assert got['meta']['versionId'] == '2'
    # The page is gone once the action has moved on.
    assert _get(client, auth_headers, action_id).status_code == 404


def test_the_form_fill_page_still_renders_the_form(
        client, app, tenant_headers, auth_headers, seeded):
    from tests.actions.test_review_flow import (MED_A, PATIENT, _seed,
                                                _staged_form_fill)
    _seed(app, tenant_headers['X-Tenant-Id'], [PATIENT, MED_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    html = _get(client, auth_headers, action_id).get_data(as_text=True)
    assert 'Review each item before we generate your form' in html
    assert 'Approve this request?' not in html
