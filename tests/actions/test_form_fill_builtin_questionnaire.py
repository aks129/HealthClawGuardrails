"""The form-fill rail renders, reviews and prints only the built-in intake
Questionnaire (#1170, #1169).

A tenant write token can create a Questionnaire, under any id it likes. The
rail used to prefer a stored one at the review and again at execution, so
the agent holding that token chose the questions the person answered, and
the labels printed beside their answers after they approved:

  - #1170: a planted copy of the intake with one extra demographics leaf,
    of a type the review page does not list, carried an answer the person
    never saw into the reviewed response and printed it on the PDF as an
    attested "no known allergies".
  - #1169: a copy planted between the review and the confirm renamed the
    attestation item, so the person's "No" printed beside the opposite
    question.

The rail now reads `r6.sdc.intake.intake_questionnaire()` and nothing else,
a proposal naming another Questionnaire is refused at propose, and the
reviewed response is built from a fixed list of item ids.

The write token (Scope.WRITE, no audience) is the credential the proposing
agent holds; `approval_headers` stands in for the approve surface's mint.
Set-up steps raise RuntimeError rather than AssertionError, so a broken
set-up fails loudly instead of reading as a pass.

Synthetic data only.
"""
import copy
import json

import pytest

from models import db
from r6.actions import errors
from r6.actions.models import ProposedAction
from r6.models import R6Resource
from r6.sdc import intake
from r6.sdc.intake import intake_questionnaire
from tests.actions.test_review_flow import (ALLERGY_A, FORM_FILL_BODY,
                                            PATIENT, _seed, _staged_form_fill)
from tests.approval_helpers import (approval_headers, propose_headers,
                                    reviewed)

NKA = 'allergies.no-known-allergies'
CANON_NKA_TEXT = 'No known allergies (patient confirmed)'
INITIAL_EXPRESSION_URL = ('http://hl7.org/fhir/uv/sdc/StructureDefinition/'
                          'sdc-questionnaire-initialExpression')
HIDDEN = 'demographics.allergy-attestation'


def _require(cond, msg):
    if not cond:
        raise RuntimeError(msg)


def _review(client, headers, action_id, body):
    return client.post('/r6/actions/%s/review' % action_id, headers=headers,
                       json=reviewed(client, headers, action_id, body,
                                     strict=False))


def _confirm(client, app, auth_headers, action_id):
    """The approve surface's confirm, on a credential it alone can mint."""
    return client.post('/r6/actions/%s/confirm' % action_id,
                       json={'approved_via': 'review-page'},
                       headers=approval_headers(app, auth_headers, action_id))


def _status(app, action_id):
    with app.app_context():
        return db.session.get(ProposedAction, action_id).status


def _spy_rows(monkeypatch):
    """Every printed "<label>: <answer>" leaf row, and the render kwargs."""
    import r6.sdc.pdf as pdf
    rows, calls = [], []
    real_item = pdf._render_item
    real_render = pdf.render_questionnaire_response_pdf

    def item_spy(item, q_index, styles, level, occurrence):
        if not pdf._is_group(item):
            rows.append('%s: %s' % (pdf._label_for(item, q_index),
                                    pdf._format_answer(item)))
        return real_item(item, q_index, styles, level, occurrence)

    def render_spy(qr, **kw):
        calls.append(kw)
        return real_render(qr, **kw)
    monkeypatch.setattr(pdf, '_render_item', item_spy)
    monkeypatch.setattr(pdf, 'render_questionnaire_response_pdf', render_spy)
    return rows, calls


def _with_hidden_leaf(questionnaire_id):
    """The intake plus one demographics leaf the review page never lists
    (type url -> valueUri), answered by a literal expression, not by the
    person."""
    q = copy.deepcopy(intake_questionnaire())
    q['id'] = questionnaire_id
    demo = [i for i in q['item'] if i.get('linkId') == 'demographics']
    _require(len(demo) == 1, 'no demographics group in the intake')
    demo[0]['item'].append({
        'linkId': HIDDEN, 'type': 'url', 'text': CANON_NKA_TEXT,
        'extension': [{'url': INITIAL_EXPRESSION_URL, 'valueExpression': {
            'language': 'text/fhirpath', 'expression': "'Yes'"}}],
    })
    return q


def _plant(client, auth_headers, questionnaire):
    """The agent's write token stores a Questionnaire under an id it picks."""
    r = client.post('/r6/fhir/Questionnaire', headers=auth_headers,
                    json=questionnaire)
    _require(r.status_code == 201, 'plant refused: %s %s'
             % (r.status_code, r.get_data(as_text=True)[:300]))
    _require(r.get_json().get('id') == questionnaire['id'],
             'planted under another id: %s' % r.get_json().get('id'))


def _review_and_confirm(client, app, auth_headers, action_id):
    """The person confirms their real allergy and does NOT tick the box."""
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))
    conf = _confirm(client, app, auth_headers, action_id)
    _require(conf.status_code == 200, conf.get_data(as_text=True))
    _require(_status(app, action_id) == 'completed',
             'not completed: %s' % conf.get_data(as_text=True)[:300])
    return person.get_json()['reviewed_qr_id']


# --- #1170: a planted Questionnaire's hidden answer -------------------------

def test_a_planted_intake_does_not_print_an_nka_the_person_never_gave(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """MUTATION: let review.py's _draft_qr read a stored Questionnaire
    again -> red, the hidden leaf printed as "No known allergies: Yes"."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    rows, _calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    _plant(client, auth_headers, _with_hidden_leaf('healthclaw-intake'))

    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=auth_headers)
    _require(page.status_code == 200, 'review page not reachable')
    _review_and_confirm(client, app, auth_headers, action_id)

    assert '%s: Yes' % CANON_NKA_TEXT not in rows, ' | '.join(rows)
    assert '%s: No' % CANON_NKA_TEXT in rows, ' | '.join(rows)


def test_an_action_naming_another_questionnaire_still_uses_the_intake(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """An action staged before propose refused other Questionnaires, naming
    one stored under a non-canonical id, still renders the built-in intake.
    The canonical-id plant above can be masked by that id's global key; this
    one cannot."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    rows, calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    planted = _with_hidden_leaf('sec1170-other-intake')
    planted['title'] = 'Planted intake'
    _plant(client, auth_headers, planted)

    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    with app.app_context():
        action = db.session.get(ProposedAction, action_id)
        payload = action.payload
        payload['questionnaire'] = 'Questionnaire/sec1170-other-intake'
        action.payload_json = json.dumps(payload)
        db.session.commit()

    qr_id = _review_and_confirm(client, app, auth_headers, action_id)
    with app.app_context():
        stored_row = R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse', id=qr_id).first()
        stored = stored_row.resource_json
    assert HIDDEN not in stored, stored
    assert not [r for r in rows if r.endswith(': Yes')], ' | '.join(rows)
    assert calls and calls[-1]['questionnaire']['id'] == 'healthclaw-intake'


@pytest.mark.parametrize('ref', [
    'Questionnaire/sec1170-other-intake', 'sec1170-other-intake',
    'http://example.test/Questionnaire/healthclaw-intake',
    'Questionnaire/healthclaw-intake/_history/2', 'healthclaw-intake|9.9.9',
])
def test_propose_refuses_a_form_fill_naming_another_questionnaire(
        client, tenant_headers, ref):
    payload = dict(FORM_FILL_BODY['payload'], questionnaire=ref)
    r = client.post('/r6/actions/propose',
                    headers=propose_headers(tenant_headers),
                    json={'kind': 'form-fill', 'payload': payload})
    assert r.status_code == 422, r.get_data(as_text=True)
    assert r.get_json()['error_code'] == errors.PAYLOAD_INVALID


@pytest.mark.parametrize('ref', [
    'healthclaw-intake', 'Questionnaire/healthclaw-intake',
    'http://healthclaw.io/fhir/Questionnaire/healthclaw-intake',
    'http://healthclaw.io/fhir/Questionnaire/healthclaw-intake|1.0.0',
])
def test_propose_accepts_each_name_of_the_builtin_intake(
        client, tenant_headers, ref):
    payload = dict(FORM_FILL_BODY['payload'], questionnaire=ref)
    r = client.post('/r6/actions/propose',
                    headers=propose_headers(tenant_headers),
                    json={'kind': 'form-fill', 'payload': payload})
    assert r.status_code == 201, r.get_data(as_text=True)


# --- #1169: the label printed beside the reviewed answer --------------------

def test_a_label_planted_after_review_does_not_invert_the_attestation(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """MUTATION: let form_fill.py's execute read a stored Questionnaire for
    labels again -> red, "No" printed beside the opposite question."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    rows, _calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))

    # Between review and confirm, the agent stores a copy under the
    # canonical id whose attestation item asks the opposite question.
    q = copy.deepcopy(intake_questionnaire())
    for group in q['item']:
        for child in group.get('item') or []:
            if child.get('linkId') == NKA:
                child['text'] = 'Patient reports any known allergies'
    _plant(client, auth_headers, q)

    conf = _confirm(client, app, auth_headers, action_id)
    _require(conf.status_code == 200, conf.get_data(as_text=True))
    assert '%s: No' % CANON_NKA_TEXT in rows, ' | '.join(rows)
    assert not [r for r in rows if r.startswith('Patient reports')], rows


# --- The reviewed response is built from a fixed list of item ids -----------

def _leaf_ids(items):
    for item in items or []:
        yield item.get('linkId')
        yield from _leaf_ids(item.get('item'))


def test_the_reviewed_response_carries_only_the_fixed_item_ids(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """Even if the intake itself grew an item, the review would not carry it
    into the response the person approves: no extra leaf in demographics or
    in a row, and no item text a later reader could prefer over the label.

    MUTATION: append the draft's demographics group whole again -> red."""
    from r6.actions import review
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    widened = _with_hidden_leaf('healthclaw-intake')
    for group in widened['item']:
        for child in group.get('item') or []:
            if child.get('linkId') == 'allergies.item':
                child['item'].append({
                    'linkId': 'allergies.item.extra', 'type': 'string',
                    'text': 'Extra', 'extension': [{
                        'url': INITIAL_EXPRESSION_URL, 'valueExpression': {
                            'language': 'text/fhirpath',
                            'expression': "'planted'"}}]})
    monkeypatch.setattr(intake, 'INTAKE_QUESTIONNAIRE', widened)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))
    with app.app_context():
        stored = json.loads(R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse',
            id=person.get_json()['reviewed_qr_id']).first().resource_json)
        _q, _p, draft, _c = review._draft_qr(
            db.session.get(ProposedAction, action_id), tenant_id)
    drafted = set(_leaf_ids(draft['item']))
    _require({HIDDEN, 'allergies.item.extra'} <= drafted,
             'the widened intake did not populate: %r' % drafted)

    ids = set(_leaf_ids(stored['item']))
    allowed = ({'demographics', 'medications', 'allergies', 'conditions'}
               | set(review.DEMOGRAPHIC_LABELS)
               | {NKA} | set(review.ROW_LEAVES)
               | {leaf for leaves in review.ROW_LEAVES.values()
                  for leaf in leaves})
    assert ids <= allowed, ids - allowed
    assert 'demographics.family-name' in ids, ids
    assert not [i for i in _items(stored['item']) if 'text' in i], stored


def _items(items):
    for item in items or []:
        yield item
        yield from _items(item.get('item'))


def test_the_fixed_item_ids_match_the_builtin_intake():
    """The fixed lists name the intake's own items, so a renamed intake
    item fails here rather than silently dropping from every form."""
    from r6.actions import review
    q = intake_questionnaire()
    by_id = {}

    def walk(items):
        for item in items or []:
            by_id[item['linkId']] = item
            walk(item.get('item'))
    walk(q['item'])
    demo = [c['linkId'] for c in by_id['demographics']['item']]
    assert list(review.DEMOGRAPHIC_LABELS) == demo
    for row, leaves in review.ROW_LEAVES.items():
        assert tuple(c['linkId'] for c in by_id[row]['item']) == leaves


# --- A review staged before the fixed-id build (#1172 review, F5) -----------

def test_a_review_staged_before_the_fix_prints_no_hidden_leaf(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """A response reviewed under the old build still carries a planted
    leaf, under a digest that matches, and the PDF would label it by its
    own linkId. The executor refuses it instead of rendering.

    MUTATION: drop the foreign-linkId refusal from execute() -> red."""
    from r6.actions import review
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    rows, _calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    real_build = review._build_reviewed_qr

    def old_build(draft_qr, *a, **kw):
        qr = real_build(draft_qr, *a, **kw)
        qr['item'][0]['item'].append(
            {'linkId': CANON_NKA_TEXT, 'answer': [{'valueUri': 'Yes'}]})
        return qr
    monkeypatch.setattr(review, '_build_reviewed_qr', old_build)
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))
    monkeypatch.setattr(review, '_build_reviewed_qr', real_build)  # deploy

    conf = _confirm(client, app, auth_headers, action_id)
    _require(conf.status_code == 200, conf.get_data(as_text=True))
    assert _status(app, action_id) == 'needs_review'
    assert not rows, rows


def test_a_staged_review_with_a_known_linkid_out_of_place_is_refused(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """R2-1: the intake's own attestation linkId, planted inside the
    demographics group with a url-typed 'Yes', passed a check on the set of
    linkIds and printed under the intake's attestation label.

    MUTATION: compare linkIds as a set again -> red."""
    from r6.actions import review
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    rows, _calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    real_build = review._build_reviewed_qr

    def old_build(draft_qr, *a, **kw):
        qr = real_build(draft_qr, *a, **kw)
        qr['item'][0]['item'].append(
            {'linkId': NKA, 'answer': [{'valueUri': 'Yes'}]})
        return qr
    monkeypatch.setattr(review, '_build_reviewed_qr', old_build)
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))
    monkeypatch.setattr(review, '_build_reviewed_qr', real_build)  # deploy

    conf = _confirm(client, app, auth_headers, action_id)
    _require(conf.status_code == 200, conf.get_data(as_text=True))
    assert _status(app, action_id) == 'needs_review'
    assert '%s: Yes' % CANON_NKA_TEXT not in rows, rows
