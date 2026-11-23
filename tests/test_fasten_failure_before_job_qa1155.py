"""QA for #1155: a Fasten export failure that arrives before its job row.

The fix records a failed FastenJob when `patient.ehi_export_failed` names a
known connection and no row exists yet. These tests drive what the fix's own
pin does not: the ready webhook arriving after the early failure, a
redelivered failure, an absent task id, a payload that names another
tenant, a connection that was revoked, and the job list and retry endpoint
reading a failed row that carries no download links.

Synthetic ids only.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from models import db
from r6.fasten.models import FastenConnection, FastenJob, TenantClosure
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

LINK = 'https://example.com/qa1155-export.jsonl'


def _post(client, event_type, data):
    with patch('r6.fasten.routes.verify_webhook', return_value=True), \
            patch('r6.fasten.routes.threading.Thread') as thread:
        resp = client.post('/fasten/webhook', data=json.dumps(
            {'type': event_type, 'data': data}),
            content_type='application/json')
    return resp, thread


def _fail(client, data):
    return _post(client, 'patient.ehi_export_failed', data)


def _ready(client, org, task):
    return _post(client, 'patient.ehi_export_success', {
        'org_connection_id': org, 'task_id': task,
        'download_links': [{'content_type': 'application/fhir+ndjson',
                            'export_type': 'jsonl', 'url': LINK}]})


def _connection(tenant, org, status='authorized'):
    db.session.add(FastenConnection(tenant_id=tenant, org_connection_id=org,
                                    connection_status=status))
    db.session.commit()


def _headers(tenant):
    return {'X-Tenant-Id': tenant,
            'X-Step-Up-Token': generate_step_up_token(tenant)}


def _jobs(client, tenant):
    resp = client.get('/fasten/jobs', headers=_headers(tenant))
    assert resp.status_code == 200
    return resp.get_json()['jobs']


def _audit(tenant):
    return [(r.event_type, r.outcome, r.detail)
            for r in AuditEventRecord.query.filter_by(tenant_id=tenant)
            .order_by(AuditEventRecord.recorded).all()]


def _rows(task):
    return FastenJob.query.filter_by(task_id=task).all()


# --- order and duplicates ---------------------------------------------------

def test_a_ready_after_an_early_failure_reuses_the_row_and_ingests(client):
    """Fasten reports the failure, then the export after all. The one row
    moves to pending with the links and the ingest starts: the records are
    real, and a failure that turned out wrong must not keep them out."""
    _connection('qa1155-a', 'oc-qa1155-a')
    _fail(client, {'org_connection_id': 'oc-qa1155-a',
                   'task_id': 'task-qa1155-a', 'failure_reason': 'timeout'})

    resp, thread = _ready(client, 'oc-qa1155-a', 'task-qa1155-a')

    assert resp.status_code == 200
    assert thread.called
    rows = _rows('task-qa1155-a')
    assert len(rows) == 1
    job = rows[0]
    assert (job.status, job.failure_reason, job.tenant_id) == (
        'pending', None, 'qa1155-a')
    assert json.loads(job.download_links_json) == [LINK]
    assert [e[0] for e in _audit('qa1155-a')] == [
        'fasten_import_failed', 'fasten_import_start']


def test_a_ready_after_an_early_failure_clears_the_finish_time(client):
    _connection('qa1155-b', 'oc-qa1155-b')
    _fail(client, {'org_connection_id': 'oc-qa1155-b',
                   'task_id': 'task-qa1155-b', 'failure_reason': 'timeout'})
    _ready(client, 'oc-qa1155-b', 'task-qa1155-b')

    (job,) = _jobs(client, 'qa1155-b')
    assert job['status'] == 'pending'
    assert job['completed_at'] is None


def test_a_redelivered_failure_keeps_one_failed_row(client):
    """A webhook retry of the same failure is the same job: one row, still
    failed, and each delivery audited."""
    _connection('qa1155-c', 'oc-qa1155-c')
    data = {'org_connection_id': 'oc-qa1155-c', 'task_id': 'task-qa1155-c',
            'failure_reason': 'timeout'}

    first, _ = _fail(client, data)
    second, _ = _fail(client, data)

    assert (first.status_code, second.status_code) == (200, 200)
    rows = _rows('task-qa1155-c')
    assert len(rows) == 1
    assert (rows[0].status, rows[0].failure_reason) == ('failed', 'timeout')
    assert rows[0].completed_at is not None
    assert _audit('qa1155-c') == [
        ('fasten_import_failed', 'failure', 'job=task-qa1155-c')] * 2


def _lose_the_insert_race(client, tenant, org, task):
    """Two deliveries for one task both saw no row; the other one has
    already committed its row. Replays this delivery's half: its lookup
    misses, so it inserts the same task_id into the unique column."""
    _connection(tenant, org)
    db.session.add(FastenJob(task_id=task, org_connection_id=org,
                             tenant_id=tenant, status='pending'))
    db.session.commit()
    miss = MagicMock()
    miss.filter_by.return_value.first.return_value = None
    with patch.object(FastenJob, 'query', miss):
        return _fail(client, {'org_connection_id': org, 'task_id': task,
                              'failure_reason': 'timeout'})


def test_a_failure_that_loses_the_insert_race_converges_on_redelivery(
        client):
    """The losing delivery now uses the winner's row instead of erroring;
    a redelivery after it still leaves one failed row. Each delivery is
    audited once."""
    resp, _ = _lose_the_insert_race(client, 'qa1155-r', 'oc-qa1155-r',
                                    'task-qa1155-r')
    assert resp.status_code == 200

    resp, _ = _fail(client, {'org_connection_id': 'oc-qa1155-r',
                             'task_id': 'task-qa1155-r',
                             'failure_reason': 'timeout'})

    assert resp.status_code == 200
    assert [j.status for j in _rows('task-qa1155-r')] == ['failed']
    assert _audit('qa1155-r') == [
        ('fasten_import_failed', 'failure', 'job=task-qa1155-r')] * 2


def test_a_failure_that_loses_the_insert_race_still_answers_200(client):
    resp, _ = _lose_the_insert_race(client, 'qa1155-s', 'oc-qa1155-s',
                                    'task-qa1155-s')
    assert resp.status_code == 200


# --- inputs the new branch must not turn into a row ---------------------------

@pytest.mark.parametrize('task', ['<absent>', '', None])
def test_a_failure_with_no_task_id_records_no_job(client, task):
    """No task id, no row: an empty-key row would collide with the next
    one on the unique task_id and fail that webhook."""
    _connection('qa1155-d', 'oc-qa1155-d')
    data = {'org_connection_id': 'oc-qa1155-d', 'failure_reason': 'timeout'}
    if task != '<absent>':
        data['task_id'] = task

    resp, _ = _fail(client, data)

    assert resp.status_code == 200
    assert FastenJob.query.count() == 0
    assert _jobs(client, 'qa1155-d') == []
    assert [(e[0], e[1]) for e in _audit('qa1155-d')] == [
        ('fasten_import_failed', 'failure')]


def test_a_failure_for_an_unknown_connection_records_no_job(client):
    """Unchanged by #1155: with no connection there is no tenant to own a
    row, so the failure is audited under 'unknown' and nothing else."""
    resp, _ = _fail(client, {'org_connection_id': 'oc-qa1155-nobody',
                             'task_id': 'task-qa1155-nobody',
                             'failure_reason': 'timeout'})

    assert resp.status_code == 200
    assert FastenJob.query.count() == 0
    assert _audit('unknown') == [
        ('fasten_import_failed', 'failure', 'job=task-qa1155-nobody')]


def test_the_row_takes_its_tenant_from_the_connection_not_the_payload(
        client):
    """A payload naming another tenant (as tenant_id or the widget's
    external_id) does not place the row there: the connection decides."""
    _connection('qa1155-owner', 'oc-qa1155-owner')

    _fail(client, {'org_connection_id': 'oc-qa1155-owner',
                   'task_id': 'task-qa1155-owner',
                   'tenant_id': 'qa1155-other', 'external_id': 'qa1155-other',
                   'failure_reason': 'timeout'})

    (job,) = _rows('task-qa1155-owner')
    assert job.tenant_id == 'qa1155-owner'
    assert _jobs(client, 'qa1155-other') == []
    assert [j['task_id'] for j in _jobs(client, 'qa1155-owner')] == [
        'task-qa1155-owner']
    assert _audit('qa1155-other') == []


def test_the_audit_row_carries_the_job_id_and_not_the_reason(client):
    """failure_reason comes from upstream and is not ours to vouch for. It
    never reaches the audit detail, on the new branch as on the old one."""
    _connection('qa1155-e', 'oc-qa1155-e')
    reason = 'Synthetic Patient DOB 1970-01-01 MRN 000-SYN'

    _fail(client, {'org_connection_id': 'oc-qa1155-e',
                   'task_id': 'task-qa1155-e', 'failure_reason': reason})

    assert _audit('qa1155-e') == [
        ('fasten_import_failed', 'failure', 'job=task-qa1155-e')]


# --- what reads the row ---------------------------------------------------------

def test_the_job_list_and_retry_read_a_linkless_failed_row(client):
    """The list shows the new row first and failed (the consumer client
    reads the newest status as the import's state); the single-job read
    finds it; a retry is refused because there are no links to re-run, and
    launches nothing."""
    _connection('qa1155-f', 'oc-qa1155-f')
    db.session.add(FastenJob(
        task_id='task-qa1155-f-old', org_connection_id='oc-qa1155-f',
        tenant_id='qa1155-f', status='complete',
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        completed_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.session.commit()

    _fail(client, {'org_connection_id': 'oc-qa1155-f',
                   'task_id': 'task-qa1155-f', 'failure_reason': 'timeout'})

    jobs = _jobs(client, 'qa1155-f')
    assert [(j['task_id'], j['status']) for j in jobs] == [
        ('task-qa1155-f', 'failed'), ('task-qa1155-f-old', 'complete')]
    newest = jobs[0]
    assert newest['failure_reason'] == 'timeout'
    assert newest['completed_at'] is not None
    assert (newest['ingested_resources'], newest['failed_resources']) == (
        0, 0)

    single = client.get('/fasten/jobs/task-qa1155-f',
                        headers=_headers('qa1155-f'))
    assert single.status_code == 200
    assert single.get_json()['status'] == 'failed'

    with patch('r6.fasten.routes.threading.Thread') as thread:
        retry = client.post('/fasten/jobs/task-qa1155-f/retry',
                            headers=_headers('qa1155-f'))
    assert retry.status_code == 409
    assert 'no stored download links' in retry.get_json()['error']
    assert not thread.called
    assert _rows('task-qa1155-f')[0].status == 'failed'


def test_a_revoked_connection_gets_a_failed_row_but_no_ingest(client):
    """After a disconnect the early failure still lands as a failed row
    (terminal, no links). That row is current behaviour, pinned here, not
    a ruling that it is wanted. What must hold either way: neither a later
    ready webhook nor a retry turns it into an import into the closed
    tenant."""
    _connection('qa1155-g', 'oc-qa1155-g', status='revoked')
    db.session.add(TenantClosure(tenant_id='qa1155-g'))
    db.session.commit()

    _fail(client, {'org_connection_id': 'oc-qa1155-g',
                   'task_id': 'task-qa1155-g', 'failure_reason': 'timeout'})
    ready, ready_thread = _ready(client, 'oc-qa1155-g', 'task-qa1155-g')
    with patch('r6.fasten.routes.threading.Thread') as retry_thread:
        retry = client.post('/fasten/jobs/task-qa1155-g/retry',
                            headers=_headers('qa1155-g'))

    assert ready.status_code == 200
    assert not ready_thread.called
    assert retry.status_code == 409
    assert not retry_thread.called
    (job,) = _rows('task-qa1155-g')
    assert (job.status, job.download_links_json) == ('failed', None)


@pytest.mark.xfail(strict=True, reason=(
    'Pre-existing, not introduced by #1155: the failure handler finds the '
    'job by task_id alone, so a failure naming connection A with a task id '
    'of tenant B flips B\'s job and audits it under A. Goes green when the '
    'lookup also matches org_connection_id.'))
def test_a_failure_on_one_connection_leaves_another_tenants_job_alone(
        client):
    _connection('qa1155-h1', 'oc-qa1155-h1')
    _connection('qa1155-h2', 'oc-qa1155-h2')
    db.session.add(FastenJob(task_id='task-qa1155-h2',
                             org_connection_id='oc-qa1155-h2',
                             tenant_id='qa1155-h2', status='ingesting'))
    db.session.commit()

    _fail(client, {'org_connection_id': 'oc-qa1155-h1',
                   'task_id': 'task-qa1155-h2', 'failure_reason': 'timeout'})

    assert _rows('task-qa1155-h2')[0].status == 'ingesting'
