"""Boot reaper for zombie Fasten jobs.

A Flask restart mid-ingest kills the daemon ingest thread and leaves the
FastenJob row stuck in a non-terminal state ('pending'/'downloading'/
'ingesting') forever. The stored signed download URLs expire, so replaying
them is useless — the fix is to re-trigger a FRESH EHI export at boot
(fresh URLs arrive via the ehi_export_success webhook, which already
reprocesses a non-complete job for the same task).

Contract under test (r6.fasten.reaper.reap_zombie_jobs):
- zombie (non-terminal, older than the 5-minute guard) -> trigger_ehi_export
  called with the job's org_connection_id; job reset to 'pending' with the
  expired links cleared.
- young non-terminal job (rolling deploy: second worker boots seconds after
  the first triggered the ingest) -> untouched, no double-trigger.
- terminal jobs ('complete'/'completed'/'failed') -> untouched.
- FASTEN_PUBLIC_KEY/FASTEN_PRIVATE_KEY missing -> clean skip, no crash.
- trigger failure -> job marked 'failed' with a clear outcome note, never
  left wedged in a non-terminal state.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from models import db
from r6.fasten.models import FastenJob
from r6.fasten.reaper import reap_zombie_jobs, ZOMBIE_MIN_AGE


@pytest.fixture
def fasten_env(monkeypatch):
    monkeypatch.setenv('FASTEN_PUBLIC_KEY', 'pk_test')
    monkeypatch.setenv('FASTEN_PRIVATE_KEY', 'sk_test')


def _utcnow():
    return datetime.now(timezone.utc)


def _make_job(task_id, status, age=timedelta(minutes=10),
              org_connection_id='oc-reap-1', links=True):
    job = FastenJob(
        task_id=task_id,
        org_connection_id=org_connection_id,
        tenant_id='reap-tenant',
        status=status,
        created_at=_utcnow() - age,
        ingested_resources=7,
        download_links_json=(json.dumps(['https://example.com/expired.jsonl'])
                             if links else None),
    )
    db.session.add(job)
    db.session.commit()
    return job.task_id


def _job(task_id):
    return FastenJob.query.filter_by(task_id=task_id).first()


def test_zombie_older_than_threshold_gets_fresh_export(app, fasten_env):
    with app.app_context():
        _make_job('task-z1', 'ingesting', org_connection_id='oc-z1')
        with patch('r6.fasten.reaper.trigger_ehi_export',
                   return_value={'task_id': 'task-z1', 'status': 'pending'}) as trig:
            reaped = reap_zombie_jobs()
        trig.assert_called_once_with('oc-z1')
        assert reaped == 1
        job = _job('task-z1')
        assert job.status == 'pending'          # webhook will reprocess it
        assert job.download_links_json is None  # old signed URLs are expired
        assert job.ingested_resources == 0      # counters reset for the re-run


@pytest.mark.parametrize('status', ['pending', 'downloading', 'ingesting'])
def test_every_non_terminal_state_is_reaped(app, fasten_env, status):
    with app.app_context():
        _make_job(f'task-nt-{status}', status,
                  org_connection_id=f'oc-nt-{status}')
        with patch('r6.fasten.reaper.trigger_ehi_export',
                   return_value={'task_id': 't', 'status': 'pending'}) as trig:
            assert reap_zombie_jobs() == 1
        trig.assert_called_once_with(f'oc-nt-{status}')


def test_young_job_is_not_double_triggered(app, fasten_env):
    # A job started 10s before a rolling deploy's second worker boots must
    # not be re-triggered — it is (probably) still running in worker one.
    with app.app_context():
        _make_job('task-young', 'downloading', age=timedelta(seconds=10))
        with patch('r6.fasten.reaper.trigger_ehi_export') as trig:
            assert reap_zombie_jobs() == 0
        trig.assert_not_called()
        assert _job('task-young').status == 'downloading'


@pytest.mark.parametrize('status', ['complete', 'completed', 'failed'])
def test_terminal_jobs_untouched(app, fasten_env, status):
    with app.app_context():
        _make_job(f'task-term-{status}', status)
        with patch('r6.fasten.reaper.trigger_ehi_export') as trig:
            assert reap_zombie_jobs() == 0
        trig.assert_not_called()
        assert _job(f'task-term-{status}').status == status


def test_missing_config_is_a_clean_skip(app, monkeypatch):
    # Dev boxes without Fasten keys must boot without crashing or touching
    # jobs — trigger_ehi_export could not do anything useful anyway.
    monkeypatch.delenv('FASTEN_PUBLIC_KEY', raising=False)
    monkeypatch.delenv('FASTEN_PRIVATE_KEY', raising=False)
    with app.app_context():
        _make_job('task-noconf', 'ingesting')
        with patch('r6.fasten.reaper.trigger_ehi_export') as trig:
            assert reap_zombie_jobs() == 0
        trig.assert_not_called()
        assert _job('task-noconf').status == 'ingesting'


def test_trigger_failure_marks_job_failed_not_wedged(app, fasten_env):
    # trigger_ehi_export returns None on any failure (missing keys, HTTP
    # error, network). The zombie must land in a terminal state with a clear
    # note — never stay non-terminal forever.
    with app.app_context():
        _make_job('task-trigfail', 'downloading', org_connection_id='oc-tf')
        with patch('r6.fasten.reaper.trigger_ehi_export', return_value=None):
            assert reap_zombie_jobs() == 0
        job = _job('task-trigfail')
        assert job.status == 'failed'
        assert 'boot reaper' in (job.failure_reason or '')
        assert job.completed_at is not None


def test_trigger_exception_never_propagates_and_marks_failed(app, fasten_env):
    # A reaper failure must never block boot: an exception from the trigger
    # call is contained per-job and the job still lands terminal.
    with app.app_context():
        _make_job('task-boom', 'ingesting', org_connection_id='oc-boom')
        _make_job('task-after', 'ingesting', org_connection_id='oc-after')
        calls = []

        def _trigger(org_id):
            calls.append(org_id)
            if org_id == 'oc-boom':
                raise RuntimeError('provider exploded')
            return {'task_id': 't', 'status': 'pending'}

        with patch('r6.fasten.reaper.trigger_ehi_export', side_effect=_trigger):
            reaped = reap_zombie_jobs()   # must not raise
        assert reaped == 1                # the healthy one
        assert calls == ['oc-boom', 'oc-after'] or calls == ['oc-after', 'oc-boom']
        assert _job('task-boom').status == 'failed'
        assert _job('task-after').status == 'pending'


def test_threshold_is_five_minutes():
    assert ZOMBIE_MIN_AGE == timedelta(minutes=5)


def test_main_calls_reaper_after_db_ready():
    # The boot hook must exist in main.py next to the other startup hooks
    # (schema_sync reconcile), wrapped so a reaper failure never blocks boot.
    import inspect as _inspect
    import main as _main
    src = _inspect.getsource(_main)
    assert 'reap_zombie_jobs' in src
