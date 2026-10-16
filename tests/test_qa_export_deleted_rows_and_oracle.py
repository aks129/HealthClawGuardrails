"""QA pins for the shared check-in/ktc row walk (r6/checkin.collect) and
the ktc gate ordering, which mutation testing showed nothing else pins.

- A soft-deleted row never leaves, on either profile. Dropping the
  `is_deleted == False` filter from r6/checkin.collect survived the whole
  suite (QA, PR #976).
- Without a token, ktc answers 401 before it looks at the patient, so a
  caller cannot learn whether a patient id exists from 422 vs 401.
  Removing the non-spending `require_grant` before `collect` survived
  tests/test_ktc_export.py.
"""

import pytest

from models import db
from r6.models import R6Resource
from tests import checkin_fixtures as f

ALL = ['AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
       'MedicationRequest', 'Patient']


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)
    monkeypatch.setenv('KTC_EXPORT_ENABLED', 'true')


def _ktc_token(client, types):
    resp = client.post(
        '/r6/fhir/internal/step-up-token',
        json={'tenant_id': f.TENANT, 'purpose': 'ktc', 'resource_types': types},
        headers={'X-Tenant-Id': f.TENANT, 'X-Internal-Secret': f.MINT_SECRET})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['token']


def _delete(app, rtype, rid):
    with app.app_context():
        row = R6Resource.query.filter_by(
            tenant_id=f.TENANT, resource_type=rtype, id=rid).one()
        row.is_deleted = True
        db.session.commit()


@pytest.mark.parametrize('profile', ['checkin', 'ktc'])
def test_a_deleted_condition_stays_home(app, client, profile):
    f.seed(app, f.patient(), f.condition(), f.condition('ci-cond-gone'))
    _delete(app, 'Condition', 'ci-cond-gone')
    types = ['Condition', 'Patient']
    tok = (_ktc_token(client, types) if profile == 'ktc'
           else f.token(client, types))
    resp = f.export(client, tok, types, profile=profile)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    ids = {r['id'] for r in f.resources(resp, 'Condition')}
    assert ids == {'ci-cond-1'}


def test_a_deleted_patient_is_not_exportable_on_ktc(app, client):
    f.seed(app, f.patient(), f.condition())
    _delete(app, 'Patient', f.PT)
    resp = f.export(client, _ktc_token(client, ALL), ALL, profile='ktc')
    assert resp.status_code == 422, resp.get_data(as_text=True)


@pytest.mark.parametrize('patient_id', [f.PT, 'no-such-patient'])
def test_no_token_gets_401_whether_or_not_the_patient_exists(
        app, client, patient_id):
    f.seed(app, f.patient(), f.condition())
    resp = f.export(client, None, ALL, patient_id=patient_id, profile='ktc')
    assert resp.status_code == 401, resp.get_data(as_text=True)
