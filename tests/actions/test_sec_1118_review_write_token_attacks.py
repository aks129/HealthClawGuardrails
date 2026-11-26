"""Security review of #1118: what a tenant write token alone can still do on
the action rail's human gate.

The write token (Scope.WRITE, no audience) is the credential the proposing
agent holds. Every test here acts with it and nothing else, unless it says
it is playing the approve surface (which alone holds the internal secret
that mints the action-bound approval credential).

These tests pin what #1118, #1116 and #1163 closed. They were written as
strict xfails (raises=AssertionError) while the holes were open, and their
set-up steps raise RuntimeError, not AssertionError, so a broken set-up fails
loudly. When the form-fill review POST began to require the approval
credential (#1116), the steps that play the person moved to that credential.
The attacker's steps and the final assertions are unchanged.

Synthetic data only: 555-01xx numbers are fictional, patients are made up.
"""
import json

import pytest

from models import db
from r6.actions.confirmations import ActionConfirmation
from r6.actions.models import ProposedAction
from r6.audit import AuditWriteError
from r6.models import AuditEventRecord, R6Resource
from tests.actions.test_action_approve_page import (CURATR, SMS, _staged,
                                                    seeded)  # noqa: F401
from tests.actions.test_review_flow import (ALLERGY_A, FORM_FILL_BODY,
                                            PATIENT, _seed, _staged_form_fill)
from tests.approval_helpers import approval_headers, propose_headers, reviewed

# ruff: noqa: F811  (the `seeded` fixture is imported, then requested by name)


def _require(cond, msg):
    """Set-up check. RuntimeError, so a strict xfail with
    raises=AssertionError does not swallow a broken set-up."""
    if not cond:
        raise RuntimeError(msg)


def _confirmations(app, action_id):
    with app.app_context():
        return [(c.approved_via, c.consumed_at) for c in
                ActionConfirmation.query.filter_by(action_id=action_id)]


def _details(app, tenant_id, action_id):
    with app.app_context():
        return [r.detail or '' for r in AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type='ProposedAction',
            resource_id=action_id)]


def _status(app, action_id):
    with app.app_context():
        return db.session.get(ProposedAction, action_id).status


def _review(client, headers, action_id, body):
    return client.post('/r6/actions/%s/review' % action_id, headers=headers,
                       json=reviewed(client, headers, action_id, body,
                                     strict=False))


# --- What #1118 closed: non-form kinds ------------------------------------

@pytest.mark.parametrize('body', [CURATR, SMS], ids=['curatr-fix', 'sms'])
def test_a_write_token_review_with_spoofed_approver_records_nothing(
        client, app, tenant_headers, auth_headers, tenant_id, seeded, body):
    action_id = _staged(client, tenant_headers, auth_headers, body)
    spoofed = dict(auth_headers, **{'X-Agent-Id': 'human-approval',
                                    'X-Human-Confirmed': 'true'})
    for _ in range(3):
        r = _review(client, spoofed, action_id,
                    {'ack': 'true', 'approved_via': 'dashboard',
                     'confirmed': True})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert 'approved_via' not in r.get_json()
    assert _confirmations(app, action_id) == []
    details = _details(app, tenant_id, action_id)
    assert any(d.startswith('review received') for d in details), details
    assert not any('approved via' in d for d in details), details
    assert _status(app, action_id) == 'awaiting_confirmation'


def test_a_write_token_cannot_confirm_decline_or_mint(
        client, app, tenant_headers, auth_headers, seeded):
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _review(client, auth_headers, action_id,
                   {'ack': 'true'}).status_code == 200
    r = client.post('/r6/actions/%s/confirm' % action_id, headers=auth_headers,
                    json={'approved_via': 'review-page'})
    assert r.status_code == 401, r.get_data(as_text=True)
    r = client.post('/r6/actions/%s/decline' % action_id, headers=auth_headers,
                    json={'declined_via': 'review-page'})
    assert r.status_code == 401, r.get_data(as_text=True)
    for secret in ('', 'changeme', auth_headers['X-Step-Up-Token']):
        r = client.post('/r6/actions/%s/approval-token' % action_id,
                        headers=dict(auth_headers,
                                     **{'X-Internal-Secret': secret}))
        assert r.status_code == 403, r.get_data(as_text=True)
    assert _confirmations(app, action_id) == []
    assert _status(app, action_id) == 'awaiting_confirmation'


def test_another_tenants_write_token_cannot_review(
        client, app, tenant_headers, auth_headers, seeded):
    from r6.stepup import generate_step_up_token
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    other = generate_step_up_token('sec1118-other-tenant')
    # Their token, our tenant header.
    r = _review(client, {'X-Tenant-Id': auth_headers['X-Tenant-Id'],
                         'X-Step-Up-Token': other}, action_id, {'ack': 'true'})
    assert r.status_code == 401, r.get_data(as_text=True)
    # Their token, their tenant header, our action id.
    r = _review(client, {'X-Tenant-Id': 'sec1118-other-tenant',
                         'X-Step-Up-Token': other}, action_id, {'ack': 'true'})
    assert r.status_code in (401, 404), r.get_data(as_text=True)
    assert _confirmations(app, action_id) == []


def test_an_audit_failure_on_the_review_post_fails_closed(
        client, app, tenant_headers, auth_headers, seeded, monkeypatch):
    """The relay confirms only on a 200, so a review POST whose audit row
    did not land must not answer 200."""
    import r6.actions.review as review
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)

    def boom(*a, **k):
        raise AuditWriteError('forced')
    monkeypatch.setattr(review, 'add_audit_event', boom)
    try:
        r = _review(client, auth_headers, action_id, {'ack': 'true'})
        code = r.status_code
    except AuditWriteError:
        code = 500
    assert code != 200
    assert _confirmations(app, action_id) == []


def test_two_approvals_after_a_write_token_review_execute_once(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    """The PR claims a double-tap is single-winner now that the review POST
    no longer 409s the second one. Two credentials, two confirms."""
    action_id = _staged(client, tenant_headers, auth_headers, CURATR)
    assert _review(client, auth_headers, action_id,
                   {'ack': 'true'}).status_code == 200
    h1 = approval_headers(app, auth_headers, action_id)
    h2 = approval_headers(app, auth_headers, action_id)
    r1 = client.post('/r6/actions/%s/confirm' % action_id, headers=h1,
                     json={'approved_via': 'review-page'})
    r2 = client.post('/r6/actions/%s/confirm' % action_id, headers=h2,
                     json={'approved_via': 'review-page'})
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (
        r1.get_data(as_text=True), r2.get_data(as_text=True))
    assert len(_confirmations(app, action_id)) == 1
    approved = [d for d in _details(app, tenant_id, action_id)
                if d.startswith('approved via')]
    assert len(approved) == 1, approved


# --- Closed by #1116: form-fill takes the approval credential -------------

def test_a_write_token_cannot_attest_no_known_allergies_over_a_real_one(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry):
    """The agent removes the person's recorded allergy and attests "no known
    allergies" on their behalf, then the person's own submit is refused."""
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=auth_headers)
    _require(page.status_code == 200, 'review page not reachable')
    _require(_status(app, action_id) == 'awaiting_confirmation', 'not open')

    r = _review(client, auth_headers, action_id,
                {'allergy-0': 'remove', 'nka': 'true'})

    assert r.status_code in (401, 403), r.get_data(as_text=True)
    assert _confirmations(app, action_id) == []
    with app.app_context():
        assert R6Resource.query.filter_by(
            resource_type='QuestionnaireResponse',
            tenant_id=tenant_id).count() == 0
    # And the person's own answers are still accepted afterwards, on the
    # credential only the approve surface can mint.
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    assert person.status_code == 200, person.get_data(as_text=True)


# --- Closed by #1163: the reviewed answers are bound by content ----------

#: The attestation item the review route writes (r6/actions/review.py,
#: _build_reviewed_qr).
NKA = 'allergies.no-known-allergies'


def _spy_render(monkeypatch):
    import r6.sdc.pdf as pdf
    real = pdf.render_questionnaire_response_pdf
    seen = []

    def spy(qr, **kw):
        seen.append(json.loads(json.dumps(qr)))
        return real(qr, **kw)
    monkeypatch.setattr(pdf, 'render_questionnaire_response_pdf', spy)
    # The printed "<label>: <answer>" rows, as the PDF writes them.
    real_item = pdf._render_item

    def item_spy(item, q_index, styles, level, occurrence):
        if not pdf._is_group(item):
            seen_rows.append('%s: %s' % (pdf._label_for(item, q_index),
                                         pdf._format_answer(item)))
        return real_item(item, q_index, styles, level, occurrence)
    seen_rows = []
    monkeypatch.setattr(pdf, '_render_item', item_spy)
    spy.rows = seen_rows
    return seen


def _nka_answer(qr):
    """Every boolean answer in the response, by linkId."""
    out = {}

    def walk(items):
        for it in items or []:
            for a in it.get('answer') or []:
                if 'valueBoolean' in a:
                    out[it.get('linkId')] = a['valueBoolean']
                walk(a.get('item'))
            walk(it.get('item'))
    walk(qr.get('item'))
    return out


def test_a_write_token_cannot_rewrite_the_reviewed_answers_before_execution(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    seen = _spy_render(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)

    # The person's review: they confirm their allergy.
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))
    qr_id = person.get_json()['reviewed_qr_id']
    # The agent learns the id from the status route its token can read.
    st = client.get('/r6/actions/%s' % action_id, headers=auth_headers)
    _require(st.get_json()['payload'].get('reviewed_qr_id') == qr_id,
             'status route did not expose reviewed_qr_id')

    # The agent rewrites the reviewed answers with its write token.
    g = client.get('/r6/fhir/QuestionnaireResponse/%s' % qr_id,
                   headers=auth_headers)
    _require(g.status_code == 200, 'reviewed QR not readable')
    swapped = g.get_json()
    # The minimal edit: in the person's own allergies group, flip the real
    # attestation item to true and drop the allergy row they confirmed.
    groups = [i for i in swapped['item'] if i.get('linkId') == 'allergies']
    _require(len(groups) == 1, 'no allergies group in the reviewed QR')
    _require(_nka_answer(swapped).get(NKA) is False,
             'the person did not leave the attestation false')
    _require(len(groups[0]['item']) == 2, 'confirmed allergy row missing')
    groups[0]['item'] = [{'linkId': NKA, 'answer': [{'valueBoolean': True}]}]
    put = client.put('/r6/fhir/QuestionnaireResponse/%s' % qr_id,
                     headers=auth_headers, json=swapped)

    # The approve surface then confirms what the person reviewed.
    conf = client.post('/r6/actions/%s/confirm' % action_id,
                       json={'approved_via': 'review-page'},
                       headers=approval_headers(app, auth_headers, action_id))
    _require(conf.status_code in (200, 409, 422), conf.get_data(as_text=True))

    # Secure outcome: the PUT is refused, or what is rendered is what the
    # person reviewed (no attested "no known allergies" they never gave).
    refused = put.status_code in (401, 403, 409, 422)
    rendered_swap = any(_nka_answer(q).get(NKA) for q in seen)
    import r6.sdc.pdf as pdf
    rows = [r for r in pdf.render_questionnaire_response_pdf.rows
            if 'llerg' in r]
    assert refused or not rendered_swap, (put.status_code, rows)


def test_a_planted_reviewed_qr_id_is_not_executed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    seen = _spy_render(monkeypatch)
    _seed(app, tenant_id, [PATIENT])
    forged = {'resourceType': 'QuestionnaireResponse', 'status': 'completed',
              'subject': {'reference': 'Patient/%s' % PATIENT['id']},
              'item': [{'linkId': 'allergies', 'item': [
                  {'linkId': NKA, 'answer': [{'valueBoolean': True}]}]}]}
    c = client.post('/r6/fhir/QuestionnaireResponse', headers=auth_headers,
                    json=forged)
    _require(c.status_code == 201, c.get_data(as_text=True))
    payload = dict(FORM_FILL_BODY['payload'], reviewed_qr_id=c.get_json()['id'])
    p = client.post('/r6/actions/propose', headers=propose_headers(tenant_headers),
                    json={'kind': 'form-fill', 'payload': payload})
    _require(p.status_code == 201, p.get_data(as_text=True))
    action_id = p.get_json()['id']
    _require(client.post('/r6/actions/%s/commit' % action_id,
                         headers=auth_headers).status_code == 202, 'commit')

    conf = client.post('/r6/actions/%s/confirm' % action_id,
                       json={'approved_via': 'review-page'},
                       headers=approval_headers(app, auth_headers, action_id))
    assert conf.status_code != 200 or not seen, conf.get_data(as_text=True)
