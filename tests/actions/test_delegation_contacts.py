"""Saved contacts: the numbers a person has attested an assistant may call.

A contact is a destination, so who may write one decides who may choose
where a call goes. Only the server-to-server secret writes; a tenant write
token, which an agent holds, is refused before anything is stored or
audited. The agent's read lists contacts without their numbers. Synthetic
data only: 555-01xx numbers and made-up labels.

MUTATIONS, each caught by the test named beside it:
  drop the secret check on the write        -> ..._a_write_token_saves_nothing
  open the gate when the secret is unset    -> ..._an_unset_secret_refuses_every_write
  drop tenant_id from the re-save lookup    -> ..._another_tenants_contact_is_not_found
  drop tenant_id from the suspend lookup    -> ..._another_tenants_contact_is_not_found
  drop tenant_id from the list query        -> ..._the_list_is_the_tenants_own
  remove the N11 rule                       -> ..._numbers_that_are_not_a_business_line
  remove the premium-rate rule              -> ..._numbers_that_are_not_a_business_line
  match digits with \\d                     -> ..._numbers_that_are_not_a_business_line
  add phone_e164 to the agent's serializer  -> ..._the_agents_list_carries_no_number
  give the agent the form view              -> ..._the_agents_list_carries_no_number
  drop the grant on the agent's list        -> ..._the_agents_list_needs_a_tenant_token
  put the label in audit detail             -> ..._no_label_or_number_in_audit
  accept a truthy business-line tick        -> ..._a_save_that_fails_a_rule_names_the_field_only
  drop the table from the purge list        -> ..._purge_removes_contacts
  drop both boot imports (main.py and
  register_model_metadata; either one
  alone still attaches the routes)          -> ..._a_fresh_process_serves_the_routes
"""

import os
import pathlib
import subprocess
import sys

import pytest

from models import db
from r6.actions.contacts import DelegationContact, suspend_contact
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

SECRET = 'contacts-secret-not-real'
TENANT = 'contacts-tenant-a'
OTHER = 'contacts-tenant-b'
LABEL = 'Sentinel Corner Pharmacy'
NUMBER = '+14125550142'
BODY = {'label': LABEL, 'phone_e164': NUMBER, 'callee_kind': 'pharmacy',
        'tz': 'America/New_York', 'business_line': True}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)


def _internal(tenant=TENANT):
    return {'X-Tenant-Id': tenant, 'X-Internal-Secret': SECRET}


def _agent(tenant=TENANT):
    return {'X-Tenant-Id': tenant,
            'X-Step-Up-Token': generate_step_up_token(tenant)}


def _save(client, tenant=TENANT, **changes):
    resp = client.post('/r6/actions/contacts', headers=_internal(tenant),
                       json=dict(BODY, **changes))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _audit_rows(tenant=TENANT):
    return AuditEventRecord.query.filter_by(tenant_id=tenant).all()


# --- who may write ---------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('post', '/r6/actions/contacts'),
    ('put', '/r6/actions/contacts/c_0000'),
])
def test_a_write_token_saves_nothing(client, app, method, path):
    resp = getattr(client, method)(path, headers=_agent(), json=BODY)
    assert resp.status_code == 403
    with app.app_context():
        assert DelegationContact.query.count() == 0
        assert _audit_rows() == []


def test_an_unset_secret_refuses_every_write(client, app, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET')
    headers = dict(_agent(), **{'X-Internal-Secret': ''})
    resp = client.post('/r6/actions/contacts', headers=headers, json=BODY)
    assert resp.status_code == 403
    with app.app_context():
        assert DelegationContact.query.count() == 0


def test_a_save_stores_the_contact_and_audits_it(client, app):
    saved = _save(client)
    assert saved['id'].startswith('c_')
    assert saved['status'] == 'active'
    assert saved['phone_e164'] == NUMBER
    assert saved['attested_at']
    with app.app_context():
        row = db.session.get(DelegationContact, saved['id'])
        assert (row.tenant_id, row.label, row.phone_e164) == (
            TENANT, LABEL, NUMBER)
        details = [a.detail for a in _audit_rows()]
        assert details == ['contact saved; contact=%s; kind=pharmacy'
                           % saved['id']]


# --- tenant isolation ------------------------------------------------------

def test_another_tenants_contact_is_not_found(client, app):
    theirs = _save(client, tenant=OTHER)
    resp = client.put('/r6/actions/contacts/%s' % theirs['id'],
                      headers=_internal(), json=dict(BODY, label='Mine Now'))
    assert resp.status_code == 404
    with app.app_context():
        assert suspend_contact(TENANT, theirs['id']) is False
        db.session.commit()
        row = db.session.get(DelegationContact, theirs['id'])
        assert (row.label, row.status) == (LABEL, 'active')


def test_the_list_is_the_tenants_own(client):
    mine = _save(client)
    _save(client, tenant=OTHER, label='Their Dentist')
    for headers in (_agent(), _internal()):
        listed = client.get('/r6/actions/contacts', headers=headers)
        assert listed.status_code == 200
        assert [c['id'] for c in listed.get_json()['contacts']] == [mine['id']]


# --- what may be saved -----------------------------------------------------

@pytest.mark.parametrize('number', [
    '911', '988', '+1911', '+19115550142',   # N11 as the area code
    '+14129110142',                          # N11 as the exchange
    '+14122110142',
    '+19005550142',                          # premium rate
    '+14129760142',
    '+442079460000',                         # not NANP
    '+11125550142', '+14120550142',          # not a dialable shape
    '4125550142', '+1 412 555 0142', '', None, 14125550142,
    '+19\u0661\u06615550142',                # 911 in another script's digits
    '+19\u0660\u06605550142',                # 900 likewise
    '+1412555014\u0662',
])
def test_numbers_that_are_not_a_business_line_are_refused(client, app, number):
    resp = client.post('/r6/actions/contacts', headers=_internal(),
                       json=dict(BODY, phone_e164=number))
    assert resp.status_code == 422, number
    assert resp.get_json()['errors'] == ['phone_e164']
    with app.app_context():
        assert DelegationContact.query.count() == 0


def test_a_toll_free_number_is_accepted(client):
    assert _save(client, phone_e164='+18005550142')['phone_e164'] == (
        '+18005550142')


@pytest.mark.parametrize('field,value', [
    ('business_line', False), ('business_line', 'yes'), ('business_line', 1),
    ('label', ''), ('label', '   '), ('label', 'x' * 61),
    ('label', 'Pharmacy <script>'), ('label', 'Line\nbreak'), ('label', 7),
    ('callee_kind', 'person'), ('callee_kind', None),
    ('tz', 'Mars/Olympus'), ('tz', ''), ('tz', '../etc/passwd'), ('tz', 5),
])
def test_a_save_that_fails_a_rule_names_the_field_only(client, app, field,
                                                       value):
    resp = client.post('/r6/actions/contacts', headers=_internal(),
                       json=dict(BODY, **{field: value}))
    assert resp.status_code == 422
    assert resp.get_json()['errors'] == [field]
    if isinstance(value, str) and value.strip():
        assert value not in resp.get_data(as_text=True)
    with app.app_context():
        assert DelegationContact.query.count() == 0


def test_a_body_that_is_not_an_object_is_refused(client):
    resp = client.post('/r6/actions/contacts', headers=_internal(),
                       json=['not', 'an', 'object'])
    assert resp.status_code == 400


# --- what the agent may read -----------------------------------------------

def test_the_agents_list_carries_no_number(client):
    saved = _save(client)
    resp = client.get('/r6/actions/contacts', headers=_agent())
    assert resp.status_code == 200
    assert resp.get_json()['contacts'] == [
        {'id': saved['id'], 'label': LABEL, 'callee_kind': 'pharmacy',
         'status': 'active'}]
    assert NUMBER not in resp.get_data(as_text=True)
    assert NUMBER[2:] not in resp.get_data(as_text=True)


def test_the_form_read_returns_the_number(client):
    _save(client)
    resp = client.get('/r6/actions/contacts', headers=_internal())
    assert resp.get_json()['contacts'][0]['phone_e164'] == NUMBER


def test_the_agents_list_needs_a_tenant_token(client):
    _save(client)
    bare = client.get('/r6/actions/contacts', headers={'X-Tenant-Id': TENANT})
    assert bare.status_code == 401
    foreign = client.get('/r6/actions/contacts', headers={
        'X-Tenant-Id': TENANT,
        'X-Step-Up-Token': generate_step_up_token(OTHER)})
    assert foreign.status_code == 401
    wrong = client.get('/r6/actions/contacts', headers={
        'X-Tenant-Id': TENANT, 'X-Internal-Secret': 'not-the-secret'})
    assert wrong.status_code == 401
    for resp in (bare, foreign, wrong):
        assert LABEL not in resp.get_data(as_text=True)


# --- suspend and re-save ---------------------------------------------------

def test_a_suspended_contact_stays_suspended_until_it_is_saved_again(
        client, app):
    saved = _save(client)
    with app.app_context():
        assert suspend_contact(TENANT, saved['id']) is True
        db.session.commit()
    listed = client.get('/r6/actions/contacts', headers=_agent()).get_json()
    assert listed['contacts'][0]['status'] == 'suspended'

    again = client.put('/r6/actions/contacts/%s' % saved['id'],
                       headers=_internal(),
                       json=dict(BODY, phone_e164='+14125550199'))
    assert again.status_code == 200
    assert again.get_json()['status'] == 'active'
    assert again.get_json()['phone_e164'] == '+14125550199'
    assert again.get_json()['attested_at'] >= saved['attested_at']
    with app.app_context():
        details = [a.detail for a in _audit_rows()]
        assert 'contact suspended; contact=%s' % saved['id'] in details
        assert details.count('contact saved; contact=%s; kind=pharmacy'
                             % saved['id']) == 2


def test_a_re_save_runs_the_same_rules(client, app):
    saved = _save(client)
    resp = client.put('/r6/actions/contacts/%s' % saved['id'],
                      headers=_internal(),
                      json=dict(BODY, phone_e164='+19005550142'))
    assert resp.status_code == 422
    with app.app_context():
        assert db.session.get(DelegationContact, saved['id']).phone_e164 == (
            NUMBER)


# --- audit and purge -------------------------------------------------------

def test_no_label_or_number_in_audit(client, app):
    saved = _save(client)
    client.put('/r6/actions/contacts/%s' % saved['id'], headers=_internal(),
               json=BODY)
    client.get('/r6/actions/contacts', headers=_agent())
    client.get('/r6/actions/contacts', headers=_internal())
    client.post('/r6/actions/contacts', headers=_internal(),
                json=dict(BODY, tz='Mars/Olympus'))
    with app.app_context():
        suspend_contact(TENANT, saved['id'])
        db.session.commit()
        rows = _audit_rows()
        # save, re-save, two reads, suspend: each leaves a row. The save
        # that failed a rule stored nothing and leaves none.
        assert len(rows) == 5
        for row in rows:
            text = ' '.join(str(v) for v in (
                row.detail, row.resource_id, row.agent_id,
                row.outcome_detail_code))
            assert 'Sentinel' not in text
            assert '4125550142' not in text
            assert 'New_York' not in text


def test_purge_removes_contacts(client, app):
    _save(client)
    kept = _save(client, tenant=OTHER)
    with app.app_context():
        from r6.purge import purge_tenant
        deleted = purge_tenant(TENANT)
        db.session.commit()
        assert deleted['delegation_contacts'] == 1
        assert [c.id for c in DelegationContact.query.all()] == [kept['id']]


# --- boot ------------------------------------------------------------------

_BOOT_PROBE = """
from main import create_app
app = create_app({"TESTING": True, "LEGACY_BOOT_ON_CREATE": False,
                  "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
print(sorted(e for e in app.view_functions if "contact" in e))
"""


def test_a_fresh_process_serves_the_routes():
    """This module imports the store itself, which is enough to attach its
    routes in this process. A server only has them if boot imports it."""
    root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', _BOOT_PROBE], cwd=root,
                            env=env, text=True, capture_output=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == str(
        ['actions.list_contacts', 'actions.resave_contact',
         'actions.save_contact'])
