"""A recorded "no known allergy" entry is not an allergy (FHI-58).

The intake gate accepts a submit that confirms at least one allergy OR ticks
"I have no known allergies". A record coded only with an absence concept
reaches the page as a row reading "Recorded entry: 'no known allergy'. ...",
and confirming that row used to count as a confirmed allergy. With every
real allergy removed, Approve then went through with no tick at all and the
form stated no known allergies that nobody attested. "No known allergies" is
only ever the person's own explicit statement.

The record shapes are the ones real feeds send: upstream `display` and
`text` (stripped by redaction), the SNOMED OID spelling, and a substance
coded beside an absence concept.
"""
import json
from html import unescape as html_unescape

import pytest

from models import db
from r6.actions.review import _is_absence_entry
from r6.models import R6Resource

TENANT = 'test-tenant'
PATIENT_ID = 'fhi58-patient'
PATIENT_REF = 'Patient/%s' % PATIENT_ID
SNOMED = 'http://snomed.info/sct'
SNOMED_OID = 'urn:oid:2.16.840.1.113883.6.96'
NKA = '716186003'           # No known allergy
NOT_ASKED = '1631000175102'  # Patient not asked
PENICILLIN = '91936005'     # Allergy to penicillin


def _store(resource, tenant_id):
    db.session.add(R6Resource(resource_type=resource['resourceType'],
                              resource_json=json.dumps(resource),
                              resource_id=resource.get('id'),
                              tenant_id=tenant_id))
    db.session.commit()


def _allergy(rid, *codings, text=None):
    code = {'coding': list(codings)}
    if text:
        code['text'] = text
    return {'resourceType': 'AllergyIntolerance', 'id': rid,
            'code': code, 'patient': {'reference': PATIENT_REF},
            'recordedDate': '2021-09-14'}


def _penicillin(rid='fhi58-pen'):
    return _allergy(rid, {'system': SNOMED, 'code': PENICILLIN,
                          'display': 'Penicillin'},
                    text='Penicillin (wheal)')


def _nka(rid='fhi58-nka', system=SNOMED, code=NKA):
    # The upstream display and text a real feed sends; redaction strips both.
    return _allergy(rid, {'system': system, 'code': code,
                          'display': 'No known allergy'},
                    text='NKA per patient')


def _seed(app, tenant_id, *allergies):
    with app.app_context():
        _store({'resourceType': 'Patient', 'id': PATIENT_ID,
                'name': [{'family': 'Gate', 'given': ['Fhi']}],
                'gender': 'female', 'birthDate': '1970-01-02'}, tenant_id)
        for allergy in allergies:
            _store(allergy, tenant_id)


def _committed_action(client, tenant_headers, auth_headers):
    proposed = client.post('/r6/actions/propose', headers=tenant_headers,
                           json={'kind': 'form-fill',
                                 'payload': {'to': 'Intake portal',
                                             'questionnaire': 'healthclaw-intake',
                                             'body': 'new patient intake form'}})
    assert proposed.status_code == 201, proposed.get_data(as_text=True)
    action_id = proposed.get_json()['id']
    committed = client.post('/r6/actions/%s/commit' % action_id,
                            headers=auth_headers)
    assert committed.status_code == 202, committed.get_data(as_text=True)
    return action_id


def _review(client, auth_headers, action_id, **answers):
    return client.post('/r6/actions/%s/review' % action_id,
                       headers=auth_headers, json=answers)


def _page(client, auth_headers, action_id):
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=auth_headers)
    assert page.status_code == 200, page.get_data(as_text=True)
    return page.get_data(as_text=True)


def _rows(html):
    """(allergen line, is absence entry) per allergy row, in page order."""
    out = []
    for chunk in html.split('class="allergy-row ')[1:]:
        head, _, rest = chunk.partition('>')
        name = rest.split('<div class="fw-bold">', 1)[1].split('</div>', 1)[0]
        out.append((html_unescape(name), 'data-absence-entry="true"' in head))
    return out


def _qr_count(app):
    with app.app_context():
        return R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', tenant_id=TENANT).count()


def _row_index(html, startswith):
    for i, (name, _) in enumerate(_rows(html)):
        if name.startswith(startswith):
            return i
    raise AssertionError('no row starting %r in %r' % (startswith, _rows(html)))


@pytest.mark.parametrize('system,code', [
    (SNOMED, NKA), (SNOMED_OID, NKA), (SNOMED, '409137002'),
    (SNOMED, NOT_ASKED)])
def test_forged_post_confirming_only_an_absence_entry_is_refused(
        client, app, tenant_headers, auth_headers, action_registry,
        system, code):
    """The FHI-58 repro as a forged POST: penicillin removed, the recorded
    "no known allergy" entry confirmed, no tick. MUTATION: count every
    confirmed row in review_submit's gate -> 200 here."""
    _seed(app, TENANT, _penicillin(), _nka(system=system, code=code))
    action_id = _committed_action(client, tenant_headers, auth_headers)
    html = _page(client, auth_headers, action_id)
    pen = _row_index(html, 'Allergy to penicillin')
    entry = _row_index(html, 'Recorded entry')
    before = _qr_count(app)

    refused = _review(client, auth_headers, action_id,
                      **{'allergy-%d' % pen: 'remove',
                         'allergy-%d' % entry: 'confirm'})

    assert refused.status_code == 422, refused.get_data(as_text=True)
    assert 'never assumed' in refused.get_json()['error']
    assert _qr_count(app) == before, 'a refused review stored a response'


def test_an_absence_entry_alone_needs_the_tick(
        client, app, tenant_headers, auth_headers, action_registry):
    _seed(app, TENANT, _nka())
    action_id = _committed_action(client, tenant_headers, auth_headers)

    refused = _review(client, auth_headers, action_id,
                      **{'allergy-0': 'confirm'})
    assert refused.status_code == 422, refused.get_data(as_text=True)

    accepted = _review(client, auth_headers, action_id,
                       **{'allergy-0': 'confirm', 'nka': 'on'})
    assert accepted.status_code == 200, accepted.get_data(as_text=True)


def test_the_tick_beside_a_confirmed_absence_entry_is_no_contradiction(
        client, app, tenant_headers, auth_headers, action_registry):
    """The entry and the attestation agree, so (3a) does not refuse it, and
    the stored answer is the person's tick, never the entry."""
    _seed(app, TENANT, _nka())
    action_id = _committed_action(client, tenant_headers, auth_headers)

    accepted = _review(client, auth_headers, action_id,
                       **{'allergy-0': 'confirm', 'nka': 'on'})

    assert accepted.status_code == 200, accepted.get_data(as_text=True)
    with app.app_context():
        body = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', tenant_id=TENANT,
            id=accepted.get_json()['reviewed_qr_id']).one().to_fhir_json()
    [allergies] = [g for g in body['item'] if g['linkId'] == 'allergies']
    [nka] = [i for i in allergies['item']
             if i['linkId'] == 'allergies.no-known-allergies']
    assert nka['answer'] == [{'valueBoolean': True}]


def test_a_real_allergy_confirmed_still_passes_without_the_tick(
        client, app, tenant_headers, auth_headers, action_registry):
    _seed(app, TENANT, _penicillin(), _nka())
    action_id = _committed_action(client, tenant_headers, auth_headers)
    html = _page(client, auth_headers, action_id)

    accepted = _review(client, auth_headers, action_id,
                       **{'allergy-%d' % _row_index(html, 'Allergy to pen'):
                          'confirm',
                          'allergy-%d' % _row_index(html, 'Recorded entry'):
                          'remove'})

    assert accepted.status_code == 200, accepted.get_data(as_text=True)


def test_a_real_allergy_beside_the_tick_is_still_a_contradiction(
        client, app, tenant_headers, auth_headers, action_registry):
    _seed(app, TENANT, _penicillin(), _nka())
    action_id = _committed_action(client, tenant_headers, auth_headers)
    html = _page(client, auth_headers, action_id)

    refused = _review(client, auth_headers, action_id,
                      **{'allergy-%d' % _row_index(html, 'Allergy to pen'):
                         'confirm',
                         'allergy-%d' % _row_index(html, 'Recorded entry'):
                         'confirm', 'nka': 'on'})

    assert refused.status_code == 422, refused.get_data(as_text=True)
    assert 'confirmed an allergy' in refused.get_json()['error']


@pytest.mark.parametrize('order', ['substance-first', 'absence-first'])
def test_a_substance_beside_an_absence_code_is_a_real_allergy(
        client, app, tenant_headers, auth_headers, action_registry, order):
    """Penicillin coded next to 716186003 is a penicillin allergy (FHI-48):
    confirming it is a confirmed allergy, and the page names it as one."""
    pen = {'system': SNOMED, 'code': PENICILLIN, 'display': 'Penicillin'}
    absent = {'system': SNOMED_OID, 'code': NKA,
              'display': 'No known allergy'}
    codings = (pen, absent) if order == 'substance-first' else (absent, pen)
    _seed(app, TENANT, _allergy('fhi58-mixed', *codings, text='NKA'))
    action_id = _committed_action(client, tenant_headers, auth_headers)

    assert _rows(_page(client, auth_headers, action_id)) == [
        ('Allergy to penicillin', False)]
    accepted = _review(client, auth_headers, action_id,
                       **{'allergy-0': 'confirm'})
    assert accepted.status_code == 200, accepted.get_data(as_text=True)


def test_the_page_marks_only_absence_entries(
        client, app, tenant_headers, auth_headers, action_registry):
    """The page's own gate reads the mark, so Approve stays off until the
    tick, as the server requires. MUTATION: drop the attribute from the
    template -> red here."""
    _seed(app, TENANT, _penicillin(), _nka(),
          _nka('fhi58-not-asked', code=NOT_ASKED))
    action_id = _committed_action(client, tenant_headers, auth_headers)
    html = _page(client, auth_headers, action_id)

    marks = {name.split('.')[0]: absent for name, absent in _rows(html)}
    assert marks == {'Allergy to penicillin': False,
                     "Recorded entry: 'no known allergy'": True,
                     "Recorded entry: 'patient not asked about allergies'":
                     True}, marks
    assert "selected(n) === 'confirm' && !absenceEntry(n)" in html


def test_a_row_without_a_source_falls_back_to_the_entry_text():
    """Every stored record has an id, so every row has a source; the text
    fallback covers a row that somehow lacks one."""
    def row(allergen):
        return {'linkId': 'allergies.item', 'item': [
            {'linkId': 'allergies.item.allergen',
             'answer': [{'valueString': allergen}]}]}

    assert _is_absence_entry(row(
        "Recorded entry: 'no known allergy'. Not a confirmed absence; "
        "confirm with a clinician."), set())
    assert not _is_absence_entry(row('Allergy to penicillin'), set())
