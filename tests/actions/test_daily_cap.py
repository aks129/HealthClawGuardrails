"""The per-tenant daily cap on calls and texts (#216, human-gate spec 6.1).

Every phone call and SMS already waits for the person's Approve on a review
page that shows the recipient, number and script. The cap bounds what that
gate cannot: how many approvals one day can turn into provider requests. A
compromised approval surface, or a person tapping through a flood of
proposals, is stopped at the cap instead of at the provider's bill.

What is counted is claims (transitions into executing), not proposals, so an
agent cannot spend the cap without a human tap. The check runs at confirm,
after the claim and before any provider request.
"""
import pytest

from models import db
from r6.actions import errors
from r6.actions.models import ProposedAction
from tests.approval_helpers import approval_headers


PHONE = {'kind': 'phone-call',
         'payload': {'to': 'Pharmacy', 'phone': '617-555-0100',
                     'body': 'Calling about a refill.'}}
SMS = {'kind': 'sms',
       'payload': {'to': 'Pharmacy', 'phone': '617-555-0100',
                   'body': 'Refill ready?'}}


@pytest.fixture
def providers_configured(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'AC-test')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'test-token')
    monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15555550100')


def _approve(client, headers, body):
    """propose -> commit -> confirm; returns (action_id, confirm response)."""
    resp = client.post('/r6/actions/propose', json=body, headers=headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    action_id = resp.get_json()['id']
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 202, resp.get_data(as_text=True)
    resp = client.post('/r6/actions/%s/confirm' % action_id,
                       headers=approval_headers(client.application, headers,
                                                action_id),
                       json={})
    return action_id, resp


def test_a_call_over_the_cap_is_refused_before_the_provider(
        client, auth_headers, app, action_registry, fake_providers,
        providers_configured, monkeypatch):
    monkeypatch.setenv('ACTIONS_DAILY_CAP', '2')

    for _ in range(2):
        _, resp = _approve(client, auth_headers, PHONE)
        assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(fake_providers) == 2

    action_id, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 429, resp.get_data(as_text=True)
    assert resp.get_json()['error_code'] == errors.DAILY_CAP_REACHED
    # THE guarantee: the refused approval made no provider request.
    assert len(fake_providers) == 2
    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert row.status == 'failed'
        assert row.outcome_summary == errors.DAILY_CAP_REACHED
        assert row.provider_request_at is None


def test_the_cap_is_per_kind(client, auth_headers, action_registry,
                             fake_providers, providers_configured,
                             monkeypatch):
    monkeypatch.setenv('ACTIONS_DAILY_CAP', '1')
    _, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 200
    # A call does not spend the text budget.
    _, resp = _approve(client, auth_headers, SMS)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _, resp = _approve(client, auth_headers, SMS)
    assert resp.status_code == 429
    assert resp.get_json()['error_code'] == errors.DAILY_CAP_REACHED
    assert len(fake_providers) == 2


def test_the_cap_is_per_tenant(client, auth_headers, other_tenant_headers,
                               action_registry, fake_providers,
                               providers_configured, monkeypatch):
    monkeypatch.setenv('ACTIONS_DAILY_CAP', '1')
    _, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 200
    # Another person's calls are not counted against this one's cap.
    _, resp = _approve(client, other_tenant_headers, PHONE)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(fake_providers) == 2


def test_yesterdays_calls_do_not_count(client, auth_headers, app,
                                       action_registry, fake_providers,
                                       providers_configured, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv('ACTIONS_DAILY_CAP', '1')
    first, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 200
    with app.app_context():
        row = db.session.get(ProposedAction, first)
        row.claimed_at = row.claimed_at - timedelta(days=1)
        db.session.commit()
    _, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_the_capped_kinds_are_the_ones_that_dial_or_text():
    from r6.actions.routes import DAILY_CAPPED_KINDS
    assert set(DAILY_CAPPED_KINDS) == {'phone-call', 'insurance-call', 'sms'}


@pytest.mark.parametrize('raw', ['', 'ten', '-1', '2.5'])
def test_an_unreadable_cap_falls_back_to_the_default(raw, monkeypatch):
    from r6.actions.routes import DEFAULT_DAILY_CAP, daily_cap
    monkeypatch.setenv('ACTIONS_DAILY_CAP', raw)
    assert daily_cap() == DEFAULT_DAILY_CAP


def test_the_default_cap_applies_when_unset(monkeypatch):
    from r6.actions.routes import DEFAULT_DAILY_CAP, daily_cap
    monkeypatch.delenv('ACTIONS_DAILY_CAP', raising=False)
    assert daily_cap() == DEFAULT_DAILY_CAP
    assert 0 < DEFAULT_DAILY_CAP <= 20


def test_a_zero_cap_refuses_every_call(client, auth_headers, action_registry,
                                       fake_providers, providers_configured,
                                       monkeypatch):
    monkeypatch.setenv('ACTIONS_DAILY_CAP', '0')
    _, resp = _approve(client, auth_headers, PHONE)
    assert resp.status_code == 429
    assert fake_providers == []
