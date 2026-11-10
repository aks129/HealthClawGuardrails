"""A free-text call can no longer be dialled.

`phone-call` and `insurance-call` take a script and a number from whoever
proposes them. They stay proposable and reviewable (the gate's own tests use
them as fixtures), but no rail dials them, the route that drafted a transfer
call from the medication list answers 410, and the provider callback that
trusts a query-string secret resolves texts and nothing else.
"""
import json

import pytest

from models import db
from r6.actions import errors
from r6.actions.events import ActionEvent
from r6.actions.models import ProposedAction, VALID_KINDS
from r6.actions.registry import all_kinds
from tests.approval_helpers import approval_headers

CALL_KINDS = ('phone-call', 'insurance-call')


@pytest.mark.parametrize('kind', CALL_KINDS)
def test_an_approved_call_fails_loud_and_reaches_no_provider(
        kind, client, auth_headers, app, action_registry, fake_providers,
        monkeypatch):
    """Propose, submit and approve still work; the approval ends `failed`.

    MUTATION: register an executor for the kind again (restore the phone
    rail module and add it back to register_all in
    r6/actions/rails/__init__.py) -> red: the provider is called and the
    action is `executing`.
    """
    # A configured provider key must not be what stands between an approval
    # and a dial.
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    monkeypatch.setenv('BLAND_API_KEY', 'test-key')
    assert kind not in all_kinds()

    resp = client.post('/r6/actions/propose', headers=auth_headers, json={
        'kind': kind, 'payload': {'to': 'Pharmacy', 'phone': '617-555-0100',
                                  'body': 'Calling about a refill.'}})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    action_id = resp.get_json()['id']
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 202, resp.get_data(as_text=True)

    resp = client.post('/r6/actions/%s/confirm' % action_id, json={},
                       headers=approval_headers(app, auth_headers, action_id))

    assert resp.status_code == 502, resp.get_data(as_text=True)
    assert resp.get_json()['error_code'] == errors.PROVIDER_NOT_CONFIGURED
    assert fake_providers == [], 'an approved %s reached a provider' % kind
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'failed'
        assert row.provider_request_at is None
        assert row.external_ref is None


def _in_flight(app, tenant_id, kind):
    with app.app_context():
        row = ProposedAction(tenant_id=tenant_id, kind=kind,
                             payload={'to': 'Pharmacy',
                                      'phone': '617-555-0100',
                                      'body': 'script'})
        row.status = 'executing'
        row.external_ref = 'ref-123'
        db.session.add(row)
        db.session.commit()
        return row.id


def _unchanged(app, action_id):
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        events = ActionEvent.query.filter_by(action_id=action_id).count()
        return (row.status, row.outcome_summary, events) == (
            'executing', None, 0)


@pytest.mark.parametrize('kind', sorted(set(VALID_KINDS) - {'sms'}))
def test_the_callback_refuses_every_kind_but_a_text(
        kind, client, app, tenant_id, monkeypatch):
    """The right secret, naming an in-flight action that is not a text: 404
    and no state change. Posted to the provider the route still serves, so
    the kind check is what refuses, not the provider gate above it.

    MUTATION: delete the `action.kind != 'sms'` check from action_callback
    in r6/actions/routes.py -> red: 200, and the action is `completed` with
    the caller's free text in outcome_summary.
    """
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _in_flight(app, tenant_id, kind)

    resp = client.post(
        '/r6/actions/callback/twilio?action_id=%s&secret=hook-secret'
        % action_id,
        json={'call_id': 'ref-123', 'status': 'completed',
              'summary': 'free text from whoever holds the secret'})

    assert resp.status_code == 404, resp.get_data(as_text=True)
    assert _unchanged(app, action_id)


def test_the_callback_no_longer_knows_the_call_provider(
        client, app, tenant_id, monkeypatch):
    """MUTATION: accept 'bland' again in action_callback's provider gate ->
    red: 200 and the text is resolved by a provider that never sent it."""
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _in_flight(app, tenant_id, 'sms')

    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret'
        % action_id, json={'call_id': 'ref-123', 'status': 'completed'})

    assert resp.status_code == 404
    assert _unchanged(app, action_id)


def test_the_transfer_route_is_gone_and_stages_nothing(
        client, app, auth_headers, tenant_id):
    """A write-capable caller with a transferable medication on file: 410,
    and no proposed action.

    MUTATION: restore the body of propose_rx_transfer in
    r6/actions/routes.py -> red: 201 and a staged phone-call.
    """
    med = {'resourceType': 'MedicationRequest', 'status': 'active',
           'intent': 'order',
           'medicationCodeableConcept': {'text': 'Metformin 500 mg tablet'},
           'subject': {'reference': 'Patient/rx-test-pt'}}
    seeded = client.post('/r6/fhir/MedicationRequest', data=json.dumps(med),
                         headers={**auth_headers, 'X-Human-Confirmed': 'true',
                                  'Content-Type': 'application/fhir+json'})
    assert seeded.status_code == 201, seeded.get_data(as_text=True)

    resp = client.post('/r6/actions/rx-transfer/propose', headers=auth_headers,
                       json={'to_pharmacy': {'name': 'Corner Pharmacy',
                                             'phone': '+15555550111'}})

    assert resp.status_code == 410, resp.get_data(as_text=True)
    with app.app_context():
        assert ProposedAction.query.filter_by(tenant_id=tenant_id).count() == 0


def test_the_transfer_route_answers_410_before_any_credential(client):
    resp = client.post('/r6/actions/rx-transfer/propose', json={})
    assert resp.status_code == 410
