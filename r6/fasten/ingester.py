"""
Fasten EHI Export streaming ingester.

Downloads FHIR R4 NDJSON files from Fasten export links and ingests them
into the HealthClaw Guardrails resource store with full audit trail.

Design:
- Streams downloads (httpx streaming) to handle 30MB–3GB files without OOM
- Runs in a daemon thread so the webhook handler returns 200 immediately
- Commits progress every _PROGRESS_BATCH resources (avoids long DB locks)
- Skips unsupported resource types gracefully (no crash, logged as skipped)
- Runs Curatr evaluation post-ingestion for clinical resource types when
  FASTEN_CURATR_SCAN=true is set
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

from models import db
from r6.audit import record_audit_event
from r6.models import R6Resource

logger = logging.getLogger(__name__)

_PROGRESS_BATCH = 50  # commit progress every N resources
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)

# Clinical types eligible for automatic Curatr scan after ingestion
_CURATR_ELIGIBLE = frozenset({
    'Condition', 'AllergyIntolerance', 'MedicationRequest',
    'Immunization', 'Procedure', 'DiagnosticReport',
})


def stream_ingest(app, job_id: int, download_links: list, tenant_id: str) -> None:
    """
    Background worker: stream-download NDJSON export files and ingest FHIR resources.

    Runs in a daemon thread via threading.Thread.
    Uses app.app_context() for all DB access.
    """
    from r6.fasten.models import FastenJob  # avoid circular import at module level

    with app.app_context():
        job = db.session.get(FastenJob, job_id)
        if not job:
            logger.error('FastenJob %s not found', job_id)
            return

        job.status = 'downloading'
        db.session.commit()

        ingested = 0
        skipped = 0
        failed = 0
        curatr_eligible_ids: list[tuple[str, str]] = []  # (resource_type, resource_id)

        try:
            for url in download_links:
                logger.info('Fasten: streaming download for job %s', job.task_id)
                with httpx.stream(
                    'GET', url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True
                ) as resp:
                    resp.raise_for_status()

                    job.status = 'ingesting'
                    db.session.commit()

                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            resource = json.loads(line)
                            result, rid = _ingest_one(resource, tenant_id)
                            if result == 'ok':
                                ingested += 1
                                rt = resource.get('resourceType', '')
                                if rt in _CURATR_ELIGIBLE and rid:
                                    curatr_eligible_ids.append((rt, rid))
                            else:
                                skipped += 1
                        except json.JSONDecodeError:
                            failed += 1
                        except Exception as exc:
                            failed += 1
                            logger.warning('Fasten ingest error: %s', exc)

                        total = ingested + skipped + failed
                        if total % _PROGRESS_BATCH == 0:
                            job.ingested_resources = ingested
                            job.skipped_resources = skipped
                            job.failed_resources = failed
                            db.session.commit()

            job.status = 'complete'
            job.ingested_resources = ingested
            job.skipped_resources = skipped
            job.failed_resources = failed
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()

            record_audit_event(
                event_type='fasten_import_complete',
                agent_id='fasten-connect',
                tenant_id=tenant_id,
                outcome='success',
                detail=(
                    f'job={job.task_id} '
                    f'ingested={ingested} skipped={skipped} failed={failed}'
                ),
            )
            logger.info(
                'Fasten job %s complete: ingested=%d skipped=%d failed=%d',
                job.task_id, ingested, skipped, failed,
            )

            # Optional: run Curatr quality scan on clinical resources
            if os.environ.get('FASTEN_CURATR_SCAN', '').lower() == 'true':
                _run_curatr_scan(curatr_eligible_ids, tenant_id, job.task_id)

        except Exception as exc:
            job.status = 'failed'
            # Truncate — never persist full error strings that may contain PHI
            job.failure_reason = str(exc)[:200]
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            record_audit_event(
                event_type='fasten_import_failed',
                agent_id='fasten-connect',
                tenant_id=tenant_id,
                outcome='failure',
                detail=f'job={job.task_id}',
            )
            logger.error('Fasten job %d failed: %s', job_id, exc)


def _ingest_one(resource: dict, tenant_id: str) -> tuple[str, str | None]:
    """
    Ingest a single FHIR resource into the guardrails store.

    Returns ('ok', resource_id) on success, ('skipped', None) for unsupported types.
    Raises on unexpected DB errors.
    """
    resource_type = resource.get('resourceType', '')

    if not resource_type or not R6Resource.is_supported_type(resource_type):
        return 'skipped', None

    resource_id = resource.get('id') or str(uuid.uuid4())
    resource_json = json.dumps(resource, separators=(',', ':'))

    existing = db.session.get(R6Resource, resource_id)
    if existing and existing.tenant_id == tenant_id and not existing.is_deleted:
        existing.update_resource(resource_json)
    else:
        new_res = R6Resource(
            resource_type=resource_type,
            resource_json=resource_json,
            resource_id=resource_id,
            tenant_id=tenant_id,
        )
        db.session.add(new_res)

    db.session.flush()

    record_audit_event(
        event_type='create',
        resource_type=resource_type,
        resource_id=resource_id,
        agent_id='fasten-connect',
        tenant_id=tenant_id,
        outcome='success',
        detail='Ingested via Fasten EHI export',
    )

    return 'ok', resource_id


def _run_curatr_scan(
    eligible: list[tuple[str, str]], tenant_id: str, task_id: str
) -> None:
    """
    Run Curatr quality evaluation on ingested clinical resources.
    Results are logged and audited; no auto-fix is applied (patient must approve).
    """
    if not eligible:
        return

    from r6.curatr import CuratrEvaluator  # local import to keep module lightweight

    evaluator = CuratrEvaluator()
    issues_found = 0

    for resource_type, resource_id in eligible[:100]:  # cap at 100 per import
        try:
            res_obj = db.session.get(R6Resource, resource_id)
            if not res_obj or res_obj.tenant_id != tenant_id:
                continue
            resource = json.loads(res_obj.resource_json)
            result = evaluator.evaluate(resource_type, resource, resource_id)
            count = len(result.get('issues', []))
            if count:
                issues_found += count
                record_audit_event(
                    event_type='curatr_scan',
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id='fasten-connect',
                    tenant_id=tenant_id,
                    outcome='success',
                    detail=f'issues={count}',
                )
        except Exception as exc:
            logger.warning('Curatr scan error for %s/%s: %s', resource_type, resource_id, exc)

    logger.info(
        'Fasten job %s Curatr scan complete: %d issues across %d resources',
        task_id, issues_found, len(eligible),
    )
