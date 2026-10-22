"""Adversarial pass on the FHI-58 NKA gate (PR #986).

"No known allergies" is only ever the person's own tick. These forge the
review POST against every absence spelling the terminology table knows, mix
substance and absence records, and try to make a real allergy read as an
absence entry so that the tick plus a confirmed real allergy slips past the
(3a) contradiction check. Synthetic data only.
"""
import json

import pytest

from models import db
from r6.actions.review import _absence_only_refs, _is_absence_entry
from r6.models import R6Resource
from r6.sdc.populate import POPULATED_ROW_SOURCE_URL
from r6.terminology import ABSENCE_CONCEPTS, LOINC, SNOMED

TENANT = 'test-tenant'
PATIENT_ID = 'fhi58-atk-patient'
PATIENT_REF = 'Patient/%s' % PATIENT_ID
SNOMED_OID = 'urn:oid:2.16.840.1.113883.6.96'
LOINC_OID = 'urn:oid:2.16.840.1.113883.6.1'
PENICILLIN = '91936005'
CANARY = 'Zebulon Canaryfield'

ENTRY_TEXT = ("Recorded entry: 'no known allergy'. Not a confirmed absence; "
              "confirm with a clinician.")


def _store(resource):
    db.session.add(R6Resource(resource_type=resource['resourceType'],
                              resource_json=json.dumps(resource),
                              resource_id=resource.get('id'),
                              tenant_id=TENANT))
    db.session.commit()


def _allergy(rid, *codings, text=None, **extra):
    code = {'coding': list(codings)}
    if text is not None:
        code['text'] = text
    body = {'resourceType': 'AllergyIntolerance', 'id': rid, 'code': code,
            'patient': {'reference': PATIENT_REF}}
    body.update(extra)
    return body


def _seed(app, *allergies):
    with app.app_context():
        _store({'resourceType': 'Patient', 'id': PATIENT_ID,
                'name': [{'family': 'Attack', 'given': ['Gate']}],
                'gender': 'female', 'birthDate': '1970-01-02'})
        for allergy in allergies:
            _store(allergy)


def _action(client, tenant_headers, auth_headers):
    proposed = client.post('/r6/actions/propose', headers=tenant_headers,
                           json={'kind': 'form-fill',
                                 'payload': {'to': 'Intake portal',
                                             'questionnaire': 'healthclaw-intake',
                                             'body': 'intake'}})
    assert proposed.status_code == 201, proposed.get_data(as_text=True)
    aid = proposed.get_json()['id']
    committed = client.post('/r6/actions/%s/commit' % aid,
                            headers=auth_headers)
    assert committed.status_code == 202, committed.get_data(as_text=True)
    return aid


def _rows(html):
    out = []
    for chunk in html.split('class="allergy-row ')[1:]:
        head, _, rest = chunk.partition('>')
        name = rest.split('<div class="fw-bold">', 1)[1].split('</div>', 1)[0]
        out.append((name, 'data-absence-entry="true"' in head))
    return out


def _page_rows(client, auth_headers, aid):
    page = client.get('/r6/actions/%s/review' % aid, headers=auth_headers)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert CANARY not in html
    return _rows(html)


def _post(client, auth_headers, aid, **answers):
    return client.post('/r6/actions/%s/review' % aid, headers=auth_headers,
                       json=answers)


def _spellings():
    out = []
    for system, code in sorted(ABSENCE_CONCEPTS):
        out.append((system, code))
        out.append((system, ' %s ' % code))
        if system == SNOMED:
            out.append((SNOMED_OID, code))
            out.append((system, int(code)))
        if system == LOINC:
            out.append((LOINC_OID, code))
    return out


@pytest.mark.parametrize('system,code', _spellings())
def test_every_absence_spelling_confirmed_without_tick_is_refused(
        client, app, tenant_headers, auth_headers, action_registry,
        system, code):
    _seed(app, _allergy('atk-abs', {'system': system, 'code': code,
                                    'display': CANARY}, text=CANARY))
    aid = _action(client, tenant_headers, auth_headers)
    assert [m for _, m in _page_rows(client, auth_headers, aid)] == [True]
    for nka in ('', 'false', 'off', '0', 'no'):
        r = _post(client, auth_headers, aid, **{'allergy-0': 'confirm',
                                                'nka': nka})
        assert r.status_code == 422, (nka, r.get_data(as_text=True))
    ok = _post(client, auth_headers, aid, **{'allergy-0': 'confirm',
                                             'nka': 'on'})
    assert ok.status_code == 200, ok.get_data(as_text=True)


def test_many_absence_entries_confirmed_real_removed_no_tick_is_refused(
        client, app, tenant_headers, auth_headers, action_registry):
    _seed(app,
          _allergy('atk-pen', {'system': SNOMED, 'code': PENICILLIN}),
          *[_allergy('atk-abs-%d' % i, {'system': s, 'code': c})
            for i, (s, c) in enumerate(sorted(ABSENCE_CONCEPTS))])
    aid = _action(client, tenant_headers, auth_headers)
    rows = _page_rows(client, auth_headers, aid)
    answers = {'allergy-%d' % i: ('confirm' if absent else 'remove')
               for i, (_, absent) in enumerate(rows)}
    assert sum(1 for _, a in rows if not a) == 1
    r = _post(client, auth_headers, aid, **answers)
    assert r.status_code == 422, r.get_data(as_text=True)


def test_a_record_coded_with_two_absence_concepts_is_still_an_absence(
        client, app, tenant_headers, auth_headers, action_registry):
    _seed(app, _allergy('atk-two', {'system': SNOMED, 'code': '716186003'},
                        {'system': SNOMED_OID, 'code': '409137002'}))
    aid = _action(client, tenant_headers, auth_headers)
    r = _post(client, auth_headers, aid, **{'allergy-0': 'confirm'})
    assert r.status_code == 422, r.get_data(as_text=True)


@pytest.mark.parametrize('upstream', [ENTRY_TEXT, 'No known allergy', 'NKDA'])
def test_a_real_allergy_dressed_as_an_absence_entry_is_a_contradiction(
        client, app, tenant_headers, auth_headers, action_registry, upstream):
    """Upstream text/display claiming the entry wording on a penicillin
    code: redaction strips it, the row stays a real allergy, and the tick
    beside it is refused by (3a)."""
    _seed(app, _allergy('atk-dressed',
                        {'system': SNOMED, 'code': PENICILLIN,
                         'display': upstream}, text=upstream))
    aid = _action(client, tenant_headers, auth_headers)
    assert _page_rows(client, auth_headers, aid) == [
        ('Allergy to penicillin', False)]
    r = _post(client, auth_headers, aid, **{'allergy-0': 'confirm',
                                            'nka': 'on'})
    assert r.status_code == 422, r.get_data(as_text=True)
    assert 'confirmed an allergy' in r.get_json()['error']


@pytest.mark.parametrize('second', [
    {'system': SNOMED, 'code': PENICILLIN},
    {'system': 'http://example.org/local', 'code': 'X1', 'display': CANARY},
    {'code': '716186003'},  # no system: not a recognised absence
    {},
])
def test_absence_beside_any_other_coding_is_real_and_tick_contradicts(
        client, app, tenant_headers, auth_headers, action_registry, second):
    _seed(app, _allergy('atk-mixed', {'system': SNOMED, 'code': '716186003'},
                        second, text=ENTRY_TEXT))
    aid = _action(client, tenant_headers, auth_headers)
    [(name, absent)] = _page_rows(client, auth_headers, aid)
    assert absent is False and not name.startswith('Recorded entry'), name
    r = _post(client, auth_headers, aid, **{'allergy-0': 'confirm',
                                            'nka': 'on'})
    assert r.status_code == 422, r.get_data(as_text=True)


def test_a_forged_source_marker_on_the_stored_record_changes_nothing(
        client, app, tenant_headers, auth_headers, action_registry):
    """A real allergy whose stored JSON carries the populated-row-source
    extension naming the absence record still counts as an allergy."""
    forged = [{'url': POPULATED_ROW_SOURCE_URL,
               'valueReference': {'reference': 'AllergyIntolerance/atk-abs'}}]
    _seed(app,
          _allergy('atk-abs', {'system': SNOMED, 'code': '716186003'}),
          _allergy('atk-pen', {'system': SNOMED, 'code': PENICILLIN},
                   extension=forged))
    aid = _action(client, tenant_headers, auth_headers)
    rows = _page_rows(client, auth_headers, aid)
    pen = [i for i, (n, _) in enumerate(rows) if 'penicillin' in n]
    abs_ = [i for i, (_, a) in enumerate(rows) if a]
    assert len(pen) == 1 and len(abs_) == 1, rows
    r = _post(client, auth_headers, aid, **{'allergy-%d' % pen[0]: 'confirm',
                                            'allergy-%d' % abs_[0]: 'remove',
                                            'nka': 'on'})
    assert r.status_code == 422, r.get_data(as_text=True)


def test_the_marker_outranks_the_text_and_no_marker_falls_back():
    def row(text, ref=None):
        r = {'linkId': 'allergies.item', 'item': [
            {'linkId': 'allergies.item.allergen',
             'answer': [{'valueString': text}]}]}
        if ref:
            r['extension'] = [{'url': POPULATED_ROW_SOURCE_URL,
                               'valueReference': {'reference': ref}}]
        return r

    refs = {'AllergyIntolerance/abs'}
    assert not _is_absence_entry(row(ENTRY_TEXT, 'AllergyIntolerance/pen'),
                                 refs)
    assert _is_absence_entry(row('anything', 'AllergyIntolerance/abs'), refs)
    assert not _is_absence_entry(row('No known allergy'), set())
    assert not _is_absence_entry(row(ENTRY_TEXT + ' '), set())


def test_absence_only_refs_ignores_other_types_and_empty_codings():
    nka = {'system': SNOMED, 'code': '716186003'}
    assert _absence_only_refs([
        {'resourceType': 'Condition', 'id': 'c', 'code': {'coding': [nka]}},
        {'resourceType': 'AllergyIntolerance', 'id': 'e',
         'code': {'coding': []}},
        {'resourceType': 'AllergyIntolerance', 'id': 'n', 'code': None},
        {'resourceType': 'AllergyIntolerance', 'id': 'x',
         'code': {'coding': [nka, 'junk']}},
        {'resourceType': 'AllergyIntolerance', 'id': 5,
         'code': {'coding': [nka]}},
        {'resourceType': 'AllergyIntolerance', 'id': 'ok',
         'code': {'coding': [nka]}},
    ]) == {'AllergyIntolerance/ok'}


def test_another_tenants_absence_record_does_not_reclassify_a_real_one(
        client, app, tenant_headers, auth_headers, action_registry):
    """Same id in another tenant: tenant B's absence record must not make
    tenant A's penicillin row read as an absence entry."""
    _seed(app, _allergy('atk-shared', {'system': SNOMED, 'code': PENICILLIN}))
    with app.app_context():
        db.session.add(R6Resource(
            resource_type='AllergyIntolerance', resource_id='atk-shared',
            tenant_id='other-tenant', resource_json=json.dumps(_allergy(
                'atk-shared', {'system': SNOMED, 'code': '716186003'}))))
        db.session.commit()
    aid = _action(client, tenant_headers, auth_headers)
    assert _page_rows(client, auth_headers, aid) == [
        ('Allergy to penicillin', False)]
    r = _post(client, auth_headers, aid, **{'allergy-0': 'confirm',
                                            'nka': 'on'})
    assert r.status_code == 422, r.get_data(as_text=True)
