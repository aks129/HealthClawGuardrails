"""After a form-fill completes, its delivered records are not the tenant's to
rewrite (#1171).

The rail renders the person's reviewed answers to a PDF and stores it as a
DocumentReference; a signed link serves those bytes. Both that
DocumentReference and the reviewed QuestionnaireResponse it was made from
were ordinary tenant resources, so the write token the proposing agent holds
could PUT over either after the person approved, and the link then served
the agent's bytes. They are now refused once the action has completed.

The PDF title name used to be read from the Patient at execution, so a
rename between review and confirm changed whose form it said it was. It is
now read from the reviewed response's name answers, which the answers digest
binds, and is never copied into the payload the confirm response returns.

Synthetic data only.
"""
import base64
import copy
import json
from urllib.parse import urlsplit

from models import db
from r6.actions.models import ProposedAction
from r6.models import R6Resource
from tests.actions.test_form_fill_builtin_questionnaire import (
    _confirm, _require, _review, _review_and_confirm, _spy_rows)
from tests.actions.test_review_flow import (ALLERGY_A, PATIENT, _seed,
                                            _staged_form_fill)
from tests.approval_helpers import approval_headers

FORGED = b'%PDF-1.4\n% synthetic forged intake: No known allergies: Yes\n'


def _completed_form(client, app, tenant_headers, auth_headers, tenant_id,
                    monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    qr_id = _review_and_confirm(client, app, auth_headers, action_id)
    with app.app_context():
        outcome = json.loads(
            db.session.get(ProposedAction, action_id).outcome_summary)
    return action_id, qr_id, outcome


def _stored(client, auth_headers, resource_type, resource_id):
    g = client.get('/r6/fhir/%s/%s' % (resource_type, resource_id),
                   headers=auth_headers)
    _require(g.status_code == 200, '%s not readable' % resource_type)
    return {k: v for k, v in g.get_json().items()
            if not k.startswith('_') and k != 'meta'}


def _forged_docref(doc):
    doc['content'] = [{'attachment': {
        'contentType': 'application/pdf', 'title': 'Completed intake form',
        'size': len(FORGED), 'data': base64.b64encode(FORGED).decode()}}]
    return doc


def _served(client, link):
    parts = urlsplit(link)
    served = client.get(parts.path + '?' + parts.query)
    _require(served.status_code == 200, 'link not served: %s'
             % served.status_code)
    return served.data


def _refusals(app, tenant_id, resource_type, resource_id):
    from r6.models import AuditEventRecord
    with app.app_context():
        return AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type=resource_type,
            resource_id=resource_id, outcome='failure').count()


def test_the_delivered_form_cannot_be_replaced_after_approval(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F3 of the #1168 review. MUTATION: drop the locked-record check from
    the PUT route -> red, the link serves the agent's bytes."""
    _a, _q, outcome = _completed_form(client, app, tenant_headers,
                                      auth_headers, tenant_id, monkeypatch)
    docref_id = outcome['document_reference_id']
    doc = _forged_docref(_stored(client, auth_headers, 'DocumentReference',
                                 docref_id))
    put = client.put('/r6/fhir/DocumentReference/%s' % docref_id,
                     headers=auth_headers, json=doc)
    assert put.status_code == 403, put.get_data(as_text=True)
    assert _served(client, outcome['delivery_link']) != FORGED
    assert _refusals(app, tenant_id, 'DocumentReference', docref_id) == 1


def test_the_reviewed_answers_cannot_be_rewritten_after_approval(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    _a, qr_id, _o = _completed_form(client, app, tenant_headers,
                                    auth_headers, tenant_id, monkeypatch)
    qr = _stored(client, auth_headers, 'QuestionnaireResponse', qr_id)
    before = json.dumps(qr, sort_keys=True)
    qr['item'] = [{'linkId': 'allergies', 'item': [{
        'linkId': 'allergies.no-known-allergies',
        'answer': [{'valueBoolean': True}]}]}]
    put = client.put('/r6/fhir/QuestionnaireResponse/%s' % qr_id,
                     headers=auth_headers, json=qr)
    assert put.status_code == 403, put.get_data(as_text=True)
    after = _stored(client, auth_headers, 'QuestionnaireResponse', qr_id)
    assert json.dumps(after, sort_keys=True) == before


def test_the_delivered_form_cannot_be_replaced_through_bundle_ingest(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """$ingest-context upserts by (tenant, type, id): the same overwrite as
    the PUT, by another door."""
    _a, qr_id, outcome = _completed_form(client, app, tenant_headers,
                                         auth_headers, tenant_id, monkeypatch)
    docref_id = outcome['document_reference_id']
    doc = _forged_docref(_stored(client, auth_headers, 'DocumentReference',
                                 docref_id))
    doc['id'] = docref_id
    bundle = {'resourceType': 'Bundle', 'type': 'collection',
              'entry': [{'resource': doc}]}
    r = client.post('/r6/fhir/Bundle/$ingest-context', headers=auth_headers,
                    json=bundle)
    _require(r.status_code != 501, 'bundle ingest not installed')
    assert r.status_code == 400, r.get_data(as_text=True)
    assert _served(client, outcome['delivery_link']) != FORGED


def test_there_is_no_delete_for_a_delivered_form(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """No FHIR DELETE route exists; pinned so adding one has to face this."""
    _a, qr_id, outcome = _completed_form(client, app, tenant_headers,
                                         auth_headers, tenant_id, monkeypatch)
    for path in ('DocumentReference/%s' % outcome['document_reference_id'],
                 'QuestionnaireResponse/%s' % qr_id):
        r = client.delete('/r6/fhir/%s' % path, headers=auth_headers)
        assert r.status_code == 405, (path, r.status_code)
    with app.app_context():
        assert R6Resource.query.filter_by(
            id=outcome['document_reference_id'],
            is_deleted=False).count() == 1


def test_an_unrelated_document_reference_is_still_writable(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """The lock is the completed form's records, not the resource type."""
    _completed_form(client, app, tenant_headers, auth_headers, tenant_id,
                    monkeypatch)
    doc = {'resourceType': 'DocumentReference', 'status': 'current',
           'subject': {'reference': 'Patient/%s' % PATIENT['id']},
           'content':[{'attachment': {'contentType': 'text/plain',
                                       'title': 'synthetic note'}}]}
    c = client.post('/r6/fhir/DocumentReference', headers=auth_headers,
                    json=doc)
    _require(c.status_code == 201, c.get_data(as_text=True))
    doc = dict(doc, id=c.get_json()['id'], status='superseded')
    put = client.put('/r6/fhir/DocumentReference/%s' % doc['id'],
                     headers=auth_headers, json=doc)
    assert put.status_code == 200, put.get_data(as_text=True)


def test_a_patient_name_changed_after_review_is_not_printed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    """F4 of the #1168 review. MUTATION: read the label from the Patient at
    execution again -> red."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    _rows, calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    action_id = _staged_form_fill(client, tenant_headers, auth_headers)
    person = _review(client, approval_headers(app, auth_headers, action_id),
                     action_id, {'allergy-0': 'confirm'})
    _require(person.status_code == 200, person.get_data(as_text=True))

    renamed = copy.deepcopy(PATIENT)
    renamed['name'] = [{'given': ['Synthetic'], 'family': 'Impostor'}]
    put = client.put('/r6/fhir/Patient/%s' % PATIENT['id'],
                     headers=auth_headers, json=renamed)
    _require(put.status_code == 200, 'Patient PUT: %s %s' % (
        put.status_code, put.get_data(as_text=True)[:200]))

    conf = _confirm(client, app, auth_headers, action_id)
    _require(conf.status_code == 200, conf.get_data(as_text=True))
    _require(calls, 'nothing rendered')
    assert calls[-1].get('subject_label') == 'John Smith', calls


def test_a_label_planted_in_the_payload_is_not_printed(
        client, app, tenant_headers, auth_headers, tenant_id, action_registry,
        monkeypatch):
    from tests.approval_helpers import propose_headers
    from tests.actions.test_review_flow import FORM_FILL_BODY
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.test')
    _rows, calls = _spy_rows(monkeypatch)
    _seed(app, tenant_id, [PATIENT, ALLERGY_A])
    payload = dict(FORM_FILL_BODY['payload'],
                   reviewed_subject_label='Synthetic Impostor')
    p = client.post('/r6/actions/propose',
                    headers=propose_headers(tenant_headers),
                    json={'kind': 'form-fill', 'payload': payload})
    _require(p.status_code == 201, p.get_data(as_text=True))
    action_id = p.get_json()['id']
    _require(client.post('/r6/actions/%s/commit' % action_id,
                         headers=auth_headers).status_code == 202, 'commit')
    _review_and_confirm(client, app, auth_headers, action_id)
    assert calls[-1].get('subject_label') == 'John Smith', calls
