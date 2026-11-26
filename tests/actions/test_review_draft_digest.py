"""The review submit records what the review page showed, or nothing (#1174).

The POST re-populates the draft from live records, which is what makes its
gate un-craftable, but it also meant a write-token change between the page
being shown and the submit (a Patient rename, an allergy row swapped) was
what the person signed and what the PDF printed. The page now carries a
keyed digest of the draft it rendered; the submit recomputes it from the
re-populated draft and refuses on any difference.

Printed demographic answers are also bounded in length and characters, so
a name cannot carry a sentence into the title or a row, on the page, in
the stored response, or at execution.

Every digest here is read off the rendered page (approval_helpers.reviewed).
Set-up steps raise RuntimeError. Synthetic data only.
"""
import copy
import json

import pytest

from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction
from r6.models import R6Resource
from tests.actions.test_form_fill_builtin_questionnaire import (
    _confirm, _require, _spy_rows, _status)
from tests.actions.test_review_flow import (ALLERGY_A, PATIENT, _seed,
                                            _staged_form_fill)
from tests.approval_helpers import approval_headers, page_digest, reviewed

SENTENCE = 'No known allergies (patient confirmed)'
STALE = 'changed after this page was shown'


@pytest.mark.parametrize('value', [
    'Zoë', 'Zoë', "O'Brien-Núñez",
    'Иван',                    # Cyrillic
    'देवी',                    # Devanagari, vowel signs
    'दुःख',                    # Devanagari, visarga
    'محمد',                    # Arabic
    '김민준', 'Đặng',        # Hangul, Vietnamese
    '+1 (555) 010-0199', '12 Main St, Apt 4/B',
    # R3-2: names written with an allowed modifier letter or U+2019.
    'Kaʻahumanu', 'ジョー', '佐々木',
    'O’Brien', 'เด็กๆ',
])
def test_real_names_and_numbers_are_printable(value):
    from r6.actions.rails.form_fill import printable_answer
    assert printable_answer(value), value


@pytest.mark.parametrize('value', [
    'Johnː Yes',     # MODIFIER LETTER TRIANGULAR COLON (Lm)
    'John ः Yes',    # DEVANAGARI SIGN VISARGA, on no letter
    'Johnः Yes',     # the same, on a Latin letter
    'John: Yes', 'John： Yes', 'John∶ Yes', 'John‎',
    'John‮', 'John\nYes', '́John', 'x' * 65, '',
    # R3-2: other look-alike colons, and a same-script visarga where it
    # would act as a colon (end of value, before a space).
    'Johnˑ Yes', 'John꞉ Yes',
    'दुः Yes', 'दुः',
    'சஃ Yes',
])
def test_lookalike_colons_and_controls_are_not_printable(value):
    """R2-2. MUTATION: accept any alnum or mark again -> red on the first
    three."""
    from r6.actions.rails.form_fill import printable_answer
    assert not printable_answer(value), value


def _submit(client, app, auth_headers, action_id, body):
    return client.post('/r6/actions/%s/review' % action_id,
                       headers=approval_headers(app, auth_headers, action_id),
                       json=body)


def _nothing_recorded(app, tenant_id, action_id):
    with app.app_context():
        return (ActionConfirmation.query.filter_by(
                    action_id=action_id).count() == 0
                and R6Resource.query.filter_by(
                    tenant_id=tenant_id,
                    resource_type='QuestionnaireResponse').count() == 0)


def _rename(client, auth_headers, given, family):
    renamed = copy.deepcopy(PATIENT)
    renamed['name'] = [{'given': [given], 'family': family}]
    put = client.put('/r6/fhir/Patient/%s' % PATIENT['id'],
                     headers=auth_headers, json=renamed)
    _require(put.status_code == 200, 'Patient PUT %s' % put.status_code)


def _staged(client, app, tenant_headers, auth_headers, tenant_id,
            monkeypatch, resources=(PATIENT, ALLERGY_A)):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    _seed(app, tenant_id, list(resources))
    return _staged_form_fill(client, tenant_headers, auth_headers)


def test_title_is_the_name_the_review_page_showed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F1: a rename after the page was shown is refused, nothing is recorded,
    and the person's reload-then-submit is what gets signed.

    MUTATION: skip the digest comparison in review_submit -> red."""
    _rows, calls = _spy_rows(monkeypatch)
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    shown = reviewed(client, auth_headers, action_id, {'allergy-0': 'confirm'})
    _rename(client, auth_headers, 'Synthetic', 'Impostor')

    stale = _submit(client, app, auth_headers, action_id, shown)
    assert stale.status_code == 422, stale.get_data(as_text=True)
    assert STALE in stale.get_json()['error']
    assert _nothing_recorded(app, tenant_id, action_id)

    fresh = reviewed(client, auth_headers, action_id, {'allergy-0': 'confirm'})
    ok = _submit(client, app, auth_headers, action_id, fresh)
    assert ok.status_code == 200, ok.get_data(as_text=True)
    _require(_confirm(client, app, auth_headers, action_id).status_code == 200,
             'confirm')
    assert calls[-1]['subject_label'] == 'Synthetic Impostor'


def test_the_confirmed_allergy_row_is_the_row_the_page_showed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F1b: an allergy swapped after the page was shown, here through the
    #214 header the agent can send itself, is refused."""
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    shown = reviewed(client, auth_headers, action_id, {'allergy-0': 'confirm'})
    swapped = copy.deepcopy(ALLERGY_A)
    swapped['code'] = {'coding': [{
        'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
        'code': '2670'}]}
    swapped['clinicalStatus'] = {'coding': [{
        'system': 'http://terminology.hl7.org/CodeSystem/'
                  'allergyintolerance-clinical', 'code': 'active'}]}
    swapped['verificationStatus'] = {'coding': [{
        'system': 'http://terminology.hl7.org/CodeSystem/'
                  'allergyintolerance-verification', 'code': 'confirmed'}]}
    put = client.put('/r6/fhir/AllergyIntolerance/%s' % ALLERGY_A['id'],
                     headers=dict(auth_headers, **{'X-Human-Confirmed': 'true'}),
                     json=swapped)
    _require(put.status_code == 200, 'allergy PUT %s' % put.status_code)

    r = _submit(client, app, auth_headers, action_id, shown)
    assert r.status_code == 422, r.get_data(as_text=True)
    assert _nothing_recorded(app, tenant_id, action_id)


def test_a_submit_without_the_pages_digest_is_refused(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    real = page_digest(client, auth_headers, action_id)
    for digest in (None, '', '0' * 64, real.upper(), real[:-1] + 'x'):
        body = {'allergy-0': 'confirm'}
        if digest is not None:
            body['draft_digest'] = digest
        r = _submit(client, app, auth_headers, action_id, body)
        assert r.status_code == 422, (digest, r.get_data(as_text=True))
    assert _nothing_recorded(app, tenant_id, action_id)


def test_a_post_view_rename_cannot_put_a_sentence_in_the_title(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F1c, after the page was shown: refused by the digest."""
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    shown = reviewed(client, auth_headers, action_id, {'allergy-0': 'confirm'})
    _rename(client, auth_headers, 'John. %s: Yes.' % SENTENCE, 'Smith')
    r = _submit(client, app, auth_headers, action_id, shown)
    assert r.status_code == 422, r.get_data(as_text=True)
    assert _nothing_recorded(app, tenant_id, action_id)


def test_a_sentence_in_the_name_is_never_shown_stored_or_printed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F1c, planted before the page was shown: the bound keeps it off the
    page, out of the reviewed response and out of the title.

    MUTATION: make printable_answer accept everything -> red."""
    rows, calls = _spy_rows(monkeypatch)
    planted = copy.deepcopy(PATIENT)
    planted['name'] = [{'given': ['John. %s: Yes.' % SENTENCE],
                        'family': 'Smith'}]
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch, resources=(planted, ALLERGY_A))
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=auth_headers).get_data(as_text=True)
    assert SENTENCE not in page and 'Smith' in page

    ok = _submit(client, app, auth_headers, action_id, reviewed(
        client, auth_headers, action_id, {'allergy-0': 'confirm'}))
    assert ok.status_code == 200, ok.get_data(as_text=True)
    with app.app_context():
        stored = R6Resource.query.filter_by(
            id=ok.get_json()['reviewed_qr_id']).first().resource_json
    assert 'patient confirmed' not in stored
    _require(_confirm(client, app, auth_headers, action_id).status_code == 200,
             'confirm')
    assert calls[-1]['subject_label'] == 'Smith'
    assert not [r for r in rows if SENTENCE in r and not r.startswith(
        SENTENCE)], rows


def test_real_names_and_phone_numbers_pass_the_bound(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    _rows, calls = _spy_rows(monkeypatch)
    person = copy.deepcopy(PATIENT)
    person['name'] = [{'given': ['Zoë'], 'family': "O'Brien-Núñez"}]
    person['telecom'] = [{'system': 'phone', 'value': '+1 (555) 010-0199'}]
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch, resources=(person, ALLERGY_A))
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=auth_headers).get_data(as_text=True)
    _require(_submit(client, app, auth_headers, action_id, reviewed(
        client, auth_headers, action_id, {'allergy-0': 'confirm'}))
        .status_code == 200, 'review')
    _require(_confirm(client, app, auth_headers, action_id).status_code == 200,
             'confirm')
    assert 'O&#39;Brien-Núñez' in page or "O'Brien-Núñez" in page
    assert '+1 (555) 010-0199' in page
    assert calls[-1]['subject_label'] == "Zoë O'Brien-Núñez"


def test_a_response_reviewed_before_the_bound_is_refused_at_execution(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """The F5 shape for the bound: a response stored before it, under a
    digest that matches, is never rendered.

    MUTATION: drop the bound check from execute() -> red."""
    from r6.actions import review
    rows, _calls = _spy_rows(monkeypatch)
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    real_build = review._build_reviewed_qr

    def old_build(*a, **kw):
        qr = real_build(*a, **kw)
        for item in qr['item'][0]['item']:
            if item['linkId'] == 'demographics.given-name':
                item['answer'] = [{'valueString': '%s: Yes.' % SENTENCE}]
        return qr
    monkeypatch.setattr(review, '_build_reviewed_qr', old_build)
    _require(_submit(client, app, auth_headers, action_id, reviewed(
        client, auth_headers, action_id, {'allergy-0': 'confirm'}))
        .status_code == 200, 'review')
    monkeypatch.setattr(review, '_build_reviewed_qr', real_build)
    _require(_confirm(client, app, auth_headers, action_id).status_code == 200,
             'confirm')
    assert _status(app, action_id) == 'needs_review'
    assert not rows, rows


def test_the_digest_rides_in_no_payload(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """It is a check on the submit, not a record: nothing new is sealed."""
    action_id = _staged(client, app, tenant_headers, auth_headers, tenant_id,
                        monkeypatch)
    _require(_submit(client, app, auth_headers, action_id, reviewed(
        client, auth_headers, action_id, {'allergy-0': 'confirm'}))
        .status_code == 200, 'review')
    with app.app_context():
        payload = json.loads(
            db.session.get(ProposedAction, action_id).payload_json)
    assert 'draft_digest' not in payload
