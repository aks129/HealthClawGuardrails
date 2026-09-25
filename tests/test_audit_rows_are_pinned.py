"""The audit rows no other test drives, pinned field by field.

Written before these sites moved off `record_audit_event` onto
`add_audit_event` in the caller's transaction. The move changes WHEN a row
is committed, never WHAT it says, so each test here states the whole row —
event_type, resource type and id, tenant, agent, outcome, detail — and was
green on the shim before it was green on the new pattern.

A site with its own content pin elsewhere is not repeated here; these are
the paths no test reached at all when the migration started.
"""

import json
from unittest.mock import MagicMock, patch

from models import db
from r6.models import AuditEventRecord, R6Resource


def _rows(tenant_id):
    return [(r.event_type, r.resource_type, r.resource_id, r.tenant_id,
             r.agent_id, r.outcome, r.detail)
            for r in AuditEventRecord.query.filter_by(tenant_id=tenant_id)
            .order_by(AuditEventRecord.recorded).all()]


# --- r6/routes.py: the upstream proxy refusing a write ---------------------

def test_an_upstream_create_rejection_is_audited_as_a_failure(
        client, auth_headers, tenant_id):
    """MUTATION: drop the audit on the rejected-create branch -> red."""
    proxy = MagicMock()
    proxy.create.return_value = ({'resourceType': 'OperationOutcome'}, 422)
    with patch('r6.routes.get_proxy_for_request', return_value=proxy):
        resp = client.post('/r6/fhir/Patient',
                           json={'resourceType': 'Patient'},
                           headers={**auth_headers, 'X-Agent-Id': 'pin-agent',
                                    'X-Human-Confirmed': 'true'})
    assert resp.status_code == 422
    assert _rows(tenant_id) == [
        ('create', 'Patient', None, tenant_id, 'pin-agent', 'failure',
         'create (upstream): rejected HTTP 422'),
    ]


def test_an_upstream_update_rejection_is_audited_as_a_failure(
        client, auth_headers, tenant_id):
    """MUTATION: drop the audit on the rejected-update branch -> red."""
    proxy = MagicMock()
    proxy.update.return_value = (None, 412)
    with patch('r6.routes.get_proxy_for_request', return_value=proxy):
        resp = client.put('/r6/fhir/Patient/pin-pt',
                          json={'resourceType': 'Patient', 'id': 'pin-pt'},
                          headers={**auth_headers, 'X-Agent-Id': 'pin-agent',
                                   'X-Human-Confirmed': 'true'})
    assert resp.status_code == 412
    assert _rows(tenant_id) == [
        ('update', 'Patient', 'pin-pt', tenant_id, 'pin-agent', 'failure',
         'update (upstream): rejected HTTP 412'),
    ]


# --- r6/fasten/routes.py: the two webhook events with no other test ---------

def _fasten_webhook(client, event_type, data):
    with patch('r6.fasten.routes.verify_webhook', return_value=True):
        return client.post('/fasten/webhook', data=json.dumps(
            {'type': event_type, 'data': data}),
            content_type='application/json')


def _fasten_connection(tenant_id, org_connection_id):
    from r6.fasten.models import FastenConnection
    conn = FastenConnection(tenant_id=tenant_id,
                            org_connection_id=org_connection_id)
    db.session.add(conn)
    db.session.commit()
    return conn


def test_a_failed_export_is_audited_and_marks_the_job(client):
    """MUTATION: drop the audit in _handle_export_failed -> red."""
    from r6.fasten.models import FastenJob
    _fasten_connection('pin-fasten', 'oc-pin-1')
    db.session.add(FastenJob(task_id='task-pin-1', org_connection_id='oc-pin-1',
                             tenant_id='pin-fasten', status='ingesting'))
    db.session.commit()

    resp = _fasten_webhook(client, 'patient.ehi_export_failed', {
        'org_connection_id': 'oc-pin-1', 'task_id': 'task-pin-1',
        'failure_reason': 'timeout'})

    assert resp.status_code == 200
    job = FastenJob.query.filter_by(task_id='task-pin-1').first()
    assert (job.status, job.failure_reason) == ('failed', 'timeout')
    assert _rows('pin-fasten') == [
        ('fasten_import_failed', None, None, 'pin-fasten', 'fasten-connect',
         'failure', 'job=task-pin-1'),
    ]


def test_a_failed_export_for_an_unknown_job_is_still_audited(client):
    """No job row and no connection: the failure is recorded anyway."""
    resp = _fasten_webhook(client, 'patient.ehi_export_failed', {
        'org_connection_id': 'oc-nobody', 'task_id': 'task-nobody'})

    assert resp.status_code == 200
    assert _rows('unknown') == [
        ('fasten_import_failed', None, None, 'unknown', 'fasten-connect',
         'failure', 'job=task-nobody'),
    ]


def test_a_revoked_authorization_is_audited_and_marks_the_connection(client):
    """MUTATION: drop the audit in _handle_revoked -> red."""
    from r6.fasten.models import FastenConnection
    _fasten_connection('pin-revoke', 'oc-pin-2')

    resp = _fasten_webhook(client, 'patient.authorization_revoked',
                           {'org_connection_id': 'oc-pin-2'})

    assert resp.status_code == 200
    conn = FastenConnection.query.filter_by(org_connection_id='oc-pin-2').first()
    assert conn.connection_status == 'revoked'
    assert _rows('pin-revoke') == [
        ('fasten_connection_revoked', None, None, 'pin-revoke',
         'fasten-connect', 'success', None),
    ]


# --- r6/fasten/ingester.py: the post-ingest quality scan --------------------

def test_the_post_ingest_quality_scan_audits_each_flagged_resource(app):
    """MUTATION: drop the audit in _run_curatr_scan -> red."""
    from r6.fasten.ingester import _run_curatr_scan
    db.session.add(R6Resource(resource_type='Condition',
                              resource_json=json.dumps(
                                  {'resourceType': 'Condition'}),
                              resource_id='cond-pin', tenant_id='pin-scan'))
    db.session.commit()

    flagged = MagicMock(issues=['a', 'b'])
    with patch('r6.curatr.CuratrEngine.evaluate', return_value=flagged):
        found = _run_curatr_scan([('Condition', 'cond-pin')], 'pin-scan',
                                 'task-pin-scan')

    assert found == 2
    assert _rows('pin-scan') == [
        ('curatr_scan', 'Condition', 'cond-pin', 'pin-scan', 'fasten-connect',
         'success', 'issues=2'),
    ]


# --- r6/wearables/routes.py: the OAuth callback -----------------------------

def test_a_wearable_connection_is_audited_when_the_callback_lands(client):
    """MUTATION: drop the audit in oauth_callback -> red."""
    import time
    from r6.wearables.models import WearableConnection
    from r6.wearables.routes import _sign_state
    state = _sign_state({'tenant_id': 'pin-wear', 'provider': 'oura',
                         'ow_user_id': 'hc-pin-wear',
                         'exp': int(time.time()) + 300})

    resp = client.get(f'/wearables/oauth/callback?state={state}')

    assert resp.status_code == 200
    conn = WearableConnection.query.filter_by(tenant_id='pin-wear').one()
    assert _rows('pin-wear') == [
        ('create', 'WearableConnection', str(conn.id), 'pin-wear',
         'wearable-oauth', 'success', 'connected oura'),
    ]


# --- a read that cannot be audited is not served ----------------------------

def test_a_read_whose_audit_cannot_be_written_is_not_served(
        client, tenant_headers, monkeypatch):
    """Fail closed. With the shim, an audit failure raised AuditWriteError;
    with add_audit_event it raises the store's own error. Either way the
    interpretation must not reach the caller. Under TESTING the error
    propagates; in production Flask answers the same failure with a 500.

    MUTATION: wrap the $interpret audit in a try/except that passes -> red.
    """
    def boom(*args, **kwargs):
        raise RuntimeError('simulated audit insert failure')
    monkeypatch.setattr('r6.audit._new_audit_event', boom)

    obs = {'resourceType': 'Observation', 'status': 'final',
           'code': {'coding': [{'system': 'http://loinc.org',
                                'code': '2823-3'}]},
           'valueQuantity': {'value': 7.0, 'unit': 'mmol/L'}}
    try:
        resp = client.post('/r6/fhir/Observation/$interpret',
                           headers=tenant_headers, json=obs)
    except RuntimeError:
        return
    assert resp.status_code >= 500, (
        f'$interpret answered {resp.status_code} with no audit row')
