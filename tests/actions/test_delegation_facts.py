"""Attested facts: what a person said about themselves for a call.

Name, the name to use on a call, date of birth, callback number and
insurance details. Only the server-to-server secret writes or reads them;
a tenant token, which an agent holds, gets neither. Synthetic data only.

MUTATIONS, each caught by the test named beside it:
  drop the secret check                    -> ..._a_tenant_token_neither_writes_nor_reads
  open the gate when the secret is unset   -> ..._an_unset_secret_refuses
  let the read skip the gate               -> ..._a_tenant_token_neither_writes_nor_reads
  drop tenant_id from the facts query      -> ..._facts_are_the_tenants_own
  log a value in audit detail              -> ..._no_value_in_audit
  drop the preferred-name rule             -> ..._a_legal_name_needs_a_preferred_name
  accept a truthy attestation              -> ..._a_write_that_fails_a_rule_names_the_field_only
  accept an undefined key                  -> ..._a_write_that_fails_a_rule_names_the_field_only
  accept any date fromisoformat reads      -> ..._a_write_that_fails_a_rule_names_the_field_only
  check names with str.isalpha only        -> ..._names_in_other_scripts_and_forms_are_accepted
  drop the table from the purge list       -> ..._purge_removes_facts
  drop both boot imports                   -> ..._a_fresh_process_serves_the_routes
"""

import os
import pathlib
import subprocess
import sys

import unicodedata
from datetime import timedelta

import pytest

from models import db
from r6.actions import facts as facts_module
from r6.actions.models import _utcnow
from r6.actions.facts import FACT_RULES, DelegationFact
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

SECRET = 'facts-secret-not-real'
TENANT = 'facts-tenant-a'
OTHER = 'facts-tenant-b'
DOB = '1961-07-23'
FACTS = {'legal_name': 'Sentinel Q Sample', 'preferred_name': 'Sunny',
         'date_of_birth': DOB, 'callback_phone': '+14125550177',
         'payer_name': 'Example Mutual', 'member_id': 'ZX-99817-A',
         'group_number': 'G-4410'}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)


def _internal(tenant=TENANT):
    return {'X-Tenant-Id': tenant, 'X-Internal-Secret': SECRET}


def _agent(tenant=TENANT):
    return {'X-Tenant-Id': tenant,
            'X-Step-Up-Token': generate_step_up_token(tenant)}


def _attest(client, facts=None, tenant=TENANT, attested=True):
    return client.put('/r6/actions/facts', headers=_internal(tenant),
                      json={'facts': FACTS if facts is None else facts,
                            'attested': attested})


def _audit_rows(tenant=TENANT):
    return AuditEventRecord.query.filter_by(tenant_id=tenant).all()


def test_every_fact_has_a_passing_example():
    assert set(FACTS) == set(FACT_RULES)


# --- who may write and read ------------------------------------------------

def test_a_tenant_token_neither_writes_nor_reads(client, app):
    # No audit row for the refusals. A bounded refusal row is #1131; an
    # unbounded one would let any caller write rows for a tenant id it names.
    assert _attest(client).status_code == 200
    with app.app_context():
        before = len(_audit_rows())
    wrote = client.put('/r6/actions/facts', headers=_agent(),
                       json={'facts': {'member_id': 'AGENT-1'},
                             'attested': True})
    read = client.get('/r6/actions/facts', headers=_agent())
    assert (wrote.status_code, read.status_code) == (403, 403)
    assert DOB not in read.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(DelegationFact,
                              (TENANT, 'member_id')).value == 'ZX-99817-A'
        assert len(_audit_rows()) == before


def test_an_unset_secret_refuses(client, app, monkeypatch):
    monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET')
    headers = dict(_agent(), **{'X-Internal-Secret': ''})
    wrote = client.put('/r6/actions/facts', headers=headers,
                       json={'facts': FACTS, 'attested': True})
    read = client.get('/r6/actions/facts', headers=headers)
    assert (wrote.status_code, read.status_code) == (403, 403)
    with app.app_context():
        assert DelegationFact.query.count() == 0


def test_a_missing_tenant_is_a_400(client):
    resp = client.put('/r6/actions/facts',
                      headers={'X-Internal-Secret': SECRET},
                      json={'facts': FACTS, 'attested': True})
    assert resp.status_code == 400


# --- attesting -------------------------------------------------------------

def test_attested_facts_are_stored_one_row_per_key(client, app):
    first = _attest(client)
    assert first.status_code == 200
    assert {k: v['value'] for k, v in first.get_json()['facts'].items()} == (
        FACTS)
    again = _attest(client, {'preferred_name': 'Sam'})
    assert again.status_code == 200
    with app.app_context():
        assert DelegationFact.query.filter_by(tenant_id=TENANT).count() == (
            len(FACTS))
        assert db.session.get(
            DelegationFact, (TENANT, 'preferred_name')).value == 'Sam'
    read = client.get('/r6/actions/facts', headers=_internal()).get_json()
    assert read['facts']['preferred_name']['value'] == 'Sam'
    assert read['facts']['date_of_birth']['value'] == DOB
    assert (read['facts']['preferred_name']['attested_at']
            >= read['facts']['date_of_birth']['attested_at'])


def test_the_audit_row_names_the_keys(client, app):
    _attest(client, {'legal_name': 'Sentinel Q Sample',
                     'preferred_name': 'Sunny', 'date_of_birth': DOB})
    with app.app_context():
        assert [a.detail for a in _audit_rows()] == [
            'facts attested: date_of_birth,legal_name,preferred_name']


@pytest.mark.parametrize('facts,attested,errors', [
    ({'legal_name': 'R2-D2', 'preferred_name': 'Sunny'}, True,
     ['legal_name']),
    ({'legal_name': '', 'preferred_name': 'Sunny'}, True, ['legal_name']),
    ({'preferred_name': 'x' * 81}, True, ['preferred_name']),
    ({'preferred_name': '<b>Sunny</b>'}, True, ['preferred_name']),
    ({'date_of_birth': '1961-02-30'}, True, ['date_of_birth']),
    ({'date_of_birth': '2999-01-01'}, True, ['date_of_birth']),
    ({'date_of_birth': '07/23/1961'}, True, ['date_of_birth']),
    ({'date_of_birth': '1861-07-23'}, True, ['date_of_birth']),
    ({'date_of_birth': 19610723}, True, ['date_of_birth']),
    ({'date_of_birth': '1961-W30-7'}, True, ['date_of_birth']),
    ({'date_of_birth': '196\u0661-07-23'}, True, ['date_of_birth']),
    ({'callback_phone': '+19\u0661\u06615550177'}, True, ['callback_phone']),
    ({'callback_phone': '911'}, True, ['callback_phone']),
    ({'callback_phone': '+19005550177'}, True, ['callback_phone']),
    ({'callback_phone': '+442079460000'}, True, ['callback_phone']),
    ({'payer_name': 'Example\nMutual'}, True, ['payer_name']),
    ({'member_id': 'ZX 99817'}, True, ['member_id']),
    ({'group_number': 'G' * 31}, True, ['group_number']),
    ({'member_id': 'BAD ID', 'date_of_birth': 'nope'}, True,
     ['date_of_birth', 'member_id']),
    ({'shoe_size': '<sentinel-key>'}, True, ['facts']),
    ({}, True, ['facts']),
    (['member_id'], True, ['facts']),
    ({'member_id': 'ZX-1'}, False, ['attested']),
    ({'member_id': 'ZX-1'}, 'yes', ['attested']),
    ({'member_id': 'ZX-1'}, 1, ['attested']),
])
def test_a_write_that_fails_a_rule_names_the_field_only(
        client, app, facts, attested, errors):
    resp = _attest(client, facts, attested=attested)
    assert resp.status_code == 422
    assert resp.get_json()['errors'] == errors
    # Nothing the caller sent comes back: no value, and no key this module
    # does not define.
    text = resp.get_data(as_text=True)
    sent = facts.items() if isinstance(facts, dict) else []
    for key, value in sent:
        if key not in FACT_RULES:
            assert key not in text
        if isinstance(value, str) and value.strip():
            assert value not in text
    with app.app_context():
        assert DelegationFact.query.count() == 0
        assert _audit_rows() == []


@pytest.mark.parametrize('name', [
    'Jos\u00e9 Sample',                       # composed
    'Jose\u0301 Sample',                      # the same name, decomposed
    '\u0905\u0928\u093f\u0924\u093e \u0936\u0930\u094d\u092e\u093e',   # Devanagari, with vowel signs
    "Mary-Anne O'Sample Jr.",
])
def test_names_in_other_scripts_and_forms_are_accepted(client, app, name):
    resp = _attest(client, {'legal_name': name, 'preferred_name': name})
    assert resp.status_code == 200, resp.get_json()
    stored = resp.get_json()['facts']['legal_name']['value']
    assert stored == unicodedata.normalize('NFC', name)


@pytest.mark.parametrize('name', [
    '\u0301Jose',          # starts with a combining mark
    'Sam \U0001F600',      # a symbol
    'Sam\u200b',           # a zero-width space
    'Sam_1',
])
def test_names_that_are_not_names_are_refused(client, name):
    resp = _attest(client, {'preferred_name': name})
    assert resp.status_code == 422
    assert resp.get_json()['errors'] == ['preferred_name']


def test_the_date_of_birth_edges(client):
    today = _utcnow().date()
    for value, status in ((today.isoformat(), 200),
                          ((today + timedelta(days=1)).isoformat(), 422),
                          ('1900-01-01', 200), ('1899-12-31', 422)):
        resp = _attest(client, {'date_of_birth': value})
        assert resp.status_code == status, value


def test_a_callback_number_outside_north_america_is_refused(client):
    resp = _attest(client, {'callback_phone': '+442079460000'})
    assert resp.get_json()['errors'] == ['callback_phone']


def test_two_first_saves_of_one_key_do_not_crash(client, app, monkeypatch):
    """The second of two racing first saves finds the row already there.
    It is told to send again; nothing is half-written."""
    assert _attest(client, {'member_id': 'FIRST-1'}).status_code == 200
    monkeypatch.setattr(facts_module, '_tenant_facts', lambda tenant_id: {})
    raced = _attest(client, {'member_id': 'SECOND-2'})
    assert raced.status_code == 409
    assert 'SECOND-2' not in raced.get_data(as_text=True)
    monkeypatch.undo()
    with app.app_context():
        assert db.session.get(DelegationFact,
                              (TENANT, 'member_id')).value == 'FIRST-1'
        assert len(_audit_rows()) == 1


def test_a_legal_name_needs_a_preferred_name(client, app):
    alone = _attest(client, {'legal_name': 'Sentinel Q Sample'})
    assert alone.status_code == 422
    assert alone.get_json()['errors'] == ['preferred_name']
    # Insurance details on their own do not need one.
    assert _attest(client, {'member_id': 'ZX-1'}).status_code == 200
    # Once it is held, the legal name can be corrected on its own.
    assert _attest(client, {'legal_name': 'Sentinel Q Sample',
                            'preferred_name': 'Sunny'}).status_code == 200
    assert _attest(client, {'legal_name': 'Sentinel Sample'}).status_code == (
        200)


def test_a_body_that_is_not_an_object_is_refused(client):
    resp = client.put('/r6/actions/facts', headers=_internal(),
                      json=['not', 'an', 'object'])
    assert resp.status_code == 400


# --- tenant isolation ------------------------------------------------------

def test_facts_are_the_tenants_own(client, app):
    _attest(client, tenant=OTHER)
    assert client.get('/r6/actions/facts',
                      headers=_internal()).get_json() == {'facts': {}}
    mine = _attest(client, {'member_id': 'MINE-1'})
    assert mine.status_code == 200
    assert set(mine.get_json()['facts']) == {'member_id'}
    theirs = client.get('/r6/actions/facts',
                        headers=_internal(OTHER)).get_json()['facts']
    assert theirs['member_id']['value'] == 'ZX-99817-A'
    with app.app_context():
        assert DelegationFact.query.filter_by(tenant_id=TENANT).count() == 1


# --- audit and purge -------------------------------------------------------

def test_no_value_in_audit(client, app):
    _attest(client)
    client.get('/r6/actions/facts', headers=_internal())
    _attest(client, {'date_of_birth': '1961-07-24'})
    _attest(client, {'date_of_birth': 'not-a-date'})
    with app.app_context():
        rows = _audit_rows()
        # two writes and a read; the refused write stores and audits nothing.
        assert len(rows) == 3
        values = list(FACTS.values()) + ['1961-07-24', '4125550177', '1961']
        for row in rows:
            text = ' '.join(str(v) for v in (
                row.detail, row.resource_id, row.agent_id,
                row.outcome_detail_code))
            for value in values:
                assert value not in text


def test_purge_removes_facts(client, app):
    _attest(client)
    _attest(client, tenant=OTHER)
    with app.app_context():
        from r6.purge import purge_tenant
        deleted = purge_tenant(TENANT)
        db.session.commit()
        assert deleted['delegation_facts'] == len(FACTS)
        assert {f.tenant_id for f in DelegationFact.query.all()} == {OTHER}


# --- boot ------------------------------------------------------------------

_BOOT_PROBE = """
from main import create_app
app = create_app({"TESTING": True, "LEGACY_BOOT_ON_CREATE": False,
                  "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
print(sorted(e for e in app.view_functions if "facts" in e))
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
        ['actions.attest_facts', 'actions.read_facts'])
