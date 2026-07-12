"""Boot reaper for zombie Fasten ingestion jobs.

A Flask restart mid-ingest kills the daemon ingest thread
(r6.fasten.routes._launch_ingest), leaving the FastenJob row stuck in a
non-terminal state ('pending'/'downloading'/'ingesting') forever. The
persisted signed download URLs expire quickly, so the /retry endpoint would
just replay dead links. Recovery: at boot, re-trigger a FRESH EHI export via
trigger_ehi_export — Fasten answers the (idempotent) request with a new
ehi_export_success webhook carrying fresh URLs, and _handle_export_success
already reprocesses a non-complete job for the same task_id.

Called from main.py right after schema_sync, inside a try/except so a reaper
failure can NEVER block boot. Guards:
- only jobs older than ZOMBIE_MIN_AGE (5 min) are reaped, so a job started
  seconds before a rolling deploy's second worker boots is not
  double-triggered;
- skipped entirely (with a loud log) when FASTEN_PUBLIC_KEY/
  FASTEN_PRIVATE_KEY are unset — dev boxes must boot cleanly;
- a trigger failure marks the job 'failed' with a clear note instead of
  leaving it wedged non-terminal.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from models import db
from r6.audit import record_audit_event
from r6.fasten.api import trigger_ehi_export
from r6.fasten.models import FastenJob

logger = logging.getLogger(__name__)

# Lifecycle: pending -> downloading -> ingesting -> complete | failed.
# 'completed' is also written by the /fasten/demo flow — treat it as
# terminal too so demo rows are never "recovered".
TERMINAL_STATUSES = ('complete', 'completed', 'failed')

# Rolling-deploy guard: a job younger than this may still be running in the
# worker that started it.
ZOMBIE_MIN_AGE = timedelta(minutes=5)


def reap_zombie_jobs() -> int:
    """Re-trigger fresh EHI exports for jobs stranded by a restart.

    Returns the number of jobs successfully re-triggered. Never raises for
    per-job failures; callers additionally wrap the whole call so boot is
    never blocked.
    """
    public = os.environ.get('FASTEN_PUBLIC_KEY', '').strip()
    private = os.environ.get('FASTEN_PRIVATE_KEY', '').strip()
    if not public or not private:
        logger.info('Fasten boot reaper: FASTEN_PUBLIC_KEY/FASTEN_PRIVATE_KEY '
                    'not set — skipping zombie-job sweep (nothing to trigger '
                    'exports with)')
        return 0

    cutoff = datetime.now(timezone.utc) - ZOMBIE_MIN_AGE
    zombies = (
        FastenJob.query
        .filter(FastenJob.status.notin_(TERMINAL_STATUSES),
                FastenJob.created_at < cutoff)
        .all()
    )
    if not zombies:
        return 0

    logger.warning('Fasten boot reaper: %d zombie job(s) found in '
                   'non-terminal states — re-triggering fresh exports',
                   len(zombies))

    reaped = 0
    for job in zombies:
        try:
            result = trigger_ehi_export(job.org_connection_id)
        except Exception as exc:  # noqa: BLE001 — one bad job must not stop the sweep
            logger.error('Fasten boot reaper: trigger raised for job %s: %s',
                         job.task_id, type(exc).__name__)
            result = None

        if result is None:
            # Terminal, with a note — never leave the job wedged.
            job.status = 'failed'
            job.failure_reason = ('boot reaper: job stranded by a restart; '
                                  're-trigger of EHI export failed')[:256]
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            record_audit_event(
                event_type='fasten_job_reap_failed',
                agent_id='fasten-boot-reaper',
                tenant_id=job.tenant_id,
                outcome='failure',
                detail=f'job={job.task_id} stale_status_recovered=zombie',
            )
            continue

        # Fresh export requested. The old signed URLs are expired — clear
        # them and reset the job to 'pending'; the ehi_export_success
        # webhook (same task_id, idempotent trigger) or a new job (new
        # task_id) delivers the fresh links and reprocesses.
        job.status = 'pending'
        job.download_links_json = None
        job.ingested_resources = 0
        job.skipped_resources = 0
        job.failed_resources = 0
        job.failure_reason = None
        db.session.commit()
        record_audit_event(
            event_type='fasten_job_reaped',
            agent_id='fasten-boot-reaper',
            tenant_id=job.tenant_id,
            outcome='success',
            detail=f'job={job.task_id} fresh export triggered '
                   f'(new task={result.get("task_id")})',
        )
        logger.info('Fasten boot reaper: job %s re-triggered (fresh export '
                    'task=%s)', job.task_id, result.get('task_id'))
        reaped += 1

    return reaped
