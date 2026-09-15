"""A Curatr data-quality fix rides the action rail (#413).

propose -> commit -> the human's Approve (action-bound credential) ->
executor -> record changed once, with Provenance and audit. Every refusal
on that path leaves the record exactly as it was, and nothing in the
response, the stored outcome or the audit trail carries the record's
content.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from models import db
from r6.actions import errors
from r6.actions.models import ProposedAction
from r6.models import AuditEventRecord, R6Resource
from tests.approval_helpers import approval_headers

CANARY = 'CANARY-DISPLAY-8841'
RID = 'c-rail-1'


def _record():
    return {'resourceType': 'Condition', 'id': RID,
            'subject': {'reference': 'Patient/alice'},
            'code': {'coding': [{'system': 'http://snomed.info/sct',
                                 'code': '44054006', 'display': CANARY}]},
            'clinicalStatus': {'coding': [{'code': 'active'}]}}


def _seed(app, tenant_id):
    with app.app_context():
        db.session.add(R6Resource('Condition', json.dumps(_record()),
                                  resource_id=RID, tenant_id=tenant_id))
        db.session.commit()


def _version(client, tenant_headers):
    r = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return int(r.get_json()['meta']['versionId'])


def _body(version=1, fixes=None, to='Condition/%s' % RID):
    return {'kind': 'curatr-fix',
            'payload': {'to': to,
                        'body': 'Mark this condition as resolved.',
                        'curatr_fix': {
                            'resource_type': 'Condition', 'resource_id': RID,
                            'record_version': version,
                            'patient_intent': 'this was resolved last year',
                            'fixes': fixes if fixes is not None else [
                                {'field_path': 'Condition.clinicalStatus.coding[0].code',
                                 'new_value': 'resolved'}]}}}


def _propose(client, tenant_headers, body=None):
    return client.post('/r6/actions/propose', json=body or _body(),
                       headers=tenant_headers)


def _staged(client, tenant_headers, auth_headers, body=None):
    r = _propose(client, tenant_headers, body)
    assert r.status_code == 201, r.get_data(as_text=True)
    action_id = r.get_json()['id']
    c = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert c.status_code == 202, c.get_data(as_text=True)
    return action_id


def _confirm(client, app, auth_headers, action_id, headers=None):
    return client.post('/r6/actions/%s/confirm' % action_id, json={},
                       headers=headers or approval_headers(app, auth_headers,
                                                           action_id))


def _provenances(app, tenant_id):
    with app.app_context():
        rows = R6Resource.query.filter_by(resource_type='Provenance',
                                          tenant_id=tenant_id).all()
        return [json.loads(r.resource_json) for r in rows
                if any(t.get('reference') == 'Condition/%s' % RID
                       for t in json.loads(r.resource_json).get('target', []))]


def _status(app, action_id):
    with app.app_context():
        return ProposedAction.query.get(action_id).status


@pytest.fixture
def rail(action_registry, monkeypatch, app, tenant_id):
    monkeypatch.setenv('CURATR_FIX_RAIL_ENABLED', '1')
    _seed(app, tenant_id)


def test_the_approved_fix_changes_the_record_once_with_provenance(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    assert _version(client, tenant_headers) == 1   # staging touches nothing

    r = _confirm(client, app, auth_headers, action_id)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['status'] == 'completed'

    got = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers).get_json()
    assert got['clinicalStatus']['coding'][0]['code'] == 'resolved'
    assert got['meta']['versionId'] == '2'
    assert got['subject']['reference'] == 'Patient/alice'   # only the approved field

    provs = _provenances(app, tenant_id)
    assert len(provs) == 1
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        outcome = json.loads(action.outcome_summary)
        assert outcome['provenance_id'] == provs[0]['id'] == action.external_ref
        assert outcome['issues_fixed'] == 1
        assert outcome['fields'] == ['Condition.clinicalStatus.coding[0].code']
        assert (outcome['version_before'], outcome['version_after']) == (1, 2)
        details = [a.detail or '' for a in
                   AuditEventRecord.query.filter_by(tenant_id=tenant_id).all()]
        assert any('curatr-fix' in d for d in details)
    # The record's content is not in the response, the stored outcome or the audit trail.
    assert CANARY not in r.get_data(as_text=True)
    assert CANARY not in action.outcome_summary
    assert not any(CANARY in d for d in details)


def test_the_same_credential_cannot_execute_twice(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    headers = approval_headers(app, auth_headers, action_id)
    assert _confirm(client, app, auth_headers, action_id, headers).status_code == 200
    again = _confirm(client, app, auth_headers, action_id, headers)
    assert again.status_code == 401
    assert _version(client, tenant_headers) == 2
    assert len(_provenances(app, tenant_id)) == 1


def test_a_normal_write_token_cannot_approve(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    r = _confirm(client, app, auth_headers, action_id, headers=auth_headers)
    assert r.status_code == 401
    assert _version(client, tenant_headers) == 1
    assert _status(app, action_id) == 'awaiting_confirmation'


def test_a_credential_bound_to_another_action_cannot_approve(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    other = _staged(client, tenant_headers, auth_headers)
    r = _confirm(client, app, auth_headers, action_id,
                 headers=approval_headers(app, auth_headers, other))
    assert r.status_code == 401
    assert _version(client, tenant_headers) == 1


def test_a_payload_altered_after_the_credential_was_minted_is_not_executed(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    headers = approval_headers(app, auth_headers, action_id)
    with app.app_context():
        swapped = _body(fixes=[{'field_path': 'Condition.clinicalStatus.coding[0].code',
                                'new_value': 'inactive'}])['payload']
        ProposedAction.query.filter_by(id=action_id).update(
            {'payload_json': json.dumps(swapped)}, synchronize_session=False)
        db.session.commit()
    r = _confirm(client, app, auth_headers, action_id, headers)
    assert r.status_code in (401, 409), r.get_data(as_text=True)
    assert _version(client, tenant_headers) == 1
    assert not _provenances(app, tenant_id)


def test_decline_leaves_the_record_alone(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    r = client.post('/r6/actions/%s/decline' % action_id,
                    headers=approval_headers(app, auth_headers, action_id))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _status(app, action_id) == 'declined'
    assert _version(client, tenant_headers) == 1
    # A later Approve on the declined action does nothing either.
    r = _confirm(client, app, auth_headers, action_id)
    assert r.status_code != 200
    assert _version(client, tenant_headers) == 1


def test_an_expired_proposal_is_not_executed(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    r = _propose(client, tenant_headers)
    action_id = r.get_json()['id']
    with app.app_context():
        ProposedAction.query.filter_by(id=action_id).update(
            {'expires_at': datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(minutes=1)}, synchronize_session=False)
        db.session.commit()
    c = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert c.status_code == 410
    assert _version(client, tenant_headers) == 1


def test_a_record_that_moved_on_since_the_proposal_is_not_changed(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    """The stale-proposal policy: the approval was for the record the human
    saw. A version bump in between means no mutation, a loud failure, and
    the moved-on record left exactly as it is."""
    action_id = _staged(client, tenant_headers, auth_headers)
    moved = dict(_record())
    moved['clinicalStatus'] = {'coding': [{'code': 'remission'}]}
    with app.app_context():
        row = R6Resource.query.filter_by(resource_type='Condition', id=RID,
                                         tenant_id=tenant_id).first()
        row.update_resource(json.dumps(moved, separators=(',', ':'), sort_keys=True))
        db.session.commit()
    assert _version(client, tenant_headers) == 2

    r = _confirm(client, app, auth_headers, action_id)
    assert r.status_code == 502, r.get_data(as_text=True)
    assert r.get_json()['error_code'] == errors.STALE_SOURCE_DATA
    assert _status(app, action_id) == 'failed'
    got = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers).get_json()
    assert got['clinicalStatus']['coding'][0]['code'] == 'remission'
    assert got['meta']['versionId'] == '2'
    assert not _provenances(app, tenant_id)
    with app.app_context():
        outcome = ProposedAction.query.get(action_id).outcome_summary
    assert CANARY not in (outcome or '')


def test_the_rail_is_dark_until_the_operator_turns_it_on(
        client, app, tenant_headers, auth_headers, tenant_id, rail, monkeypatch):
    action_id = _staged(client, tenant_headers, auth_headers)
    monkeypatch.delenv('CURATR_FIX_RAIL_ENABLED')
    r = _confirm(client, app, auth_headers, action_id)
    assert r.status_code == 502
    assert r.get_json()['error_code'] == errors.PROVIDER_NOT_CONFIGURED
    assert _version(client, tenant_headers) == 1
    assert _status(app, action_id) == 'failed'


@pytest.mark.parametrize('body', [
    _body(fixes=[{'field_path': 'Condition.note', 'new_value': [{'text': 'x'}]}]),
    _body(fixes=[{'field_path': 'Patient.name', 'new_value': []}]),
    _body(to='Condition/someone-else'),
    _body(version=0),
    _body(version='1'),
    _body(fixes=[]),
], ids=['note', 'other-type-path', 'label-mismatch',
        'version-0', 'version-str', 'no-fixes'])
def test_a_fix_the_evaluator_could_not_have_proposed_is_refused_at_propose(
        client, app, tenant_headers, auth_headers, tenant_id, rail, body):
    r = _propose(client, tenant_headers, body)
    assert r.status_code == 422, r.get_data(as_text=True)
    assert r.get_json()['error_code'] == errors.PAYLOAD_INVALID
    assert _version(client, tenant_headers) == 1


def test_an_approved_relink_of_a_linked_record_is_refused_whole_at_execute(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    """`subject` is in the vocabulary only for a record that has none (#739);
    propose cannot see the record, so this one is refused where it can be —
    before any mutation, as a whole."""
    body = _body(fixes=[
        {'field_path': 'Condition.clinicalStatus.coding[0].code', 'new_value': 'resolved'},
        {'field_path': 'Condition.subject', 'new_value': {'reference': 'Patient/mallory'}}])
    action_id = _staged(client, tenant_headers, auth_headers, body)
    r = _confirm(client, app, auth_headers, action_id)
    assert r.status_code == 502, r.get_data(as_text=True)
    assert r.get_json()['error_code'] == errors.PAYLOAD_INVALID
    got = client.get('/r6/fhir/Condition/%s' % RID, headers=tenant_headers).get_json()
    assert got['subject']['reference'] == 'Patient/alice'
    assert got['clinicalStatus']['coding'][0]['code'] == 'active'
    assert got['meta']['versionId'] == '1'
    assert not _provenances(app, tenant_id)


def test_the_stored_outcome_names_only_ids_counts_and_paths(
        client, app, tenant_headers, auth_headers, tenant_id, rail):
    action_id = _staged(client, tenant_headers, auth_headers)
    _confirm(client, app, auth_headers, action_id)
    with app.app_context():
        outcome = json.loads(ProposedAction.query.get(action_id).outcome_summary)
    assert set(outcome) == {'resource', 'issues_fixed', 'provenance_id',
                            'version_before', 'version_after', 'fields'}
    assert 'resolved' not in json.dumps(outcome)   # the new value is not there either
