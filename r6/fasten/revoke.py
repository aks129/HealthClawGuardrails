"""Stop Fasten bringing records into a tenant — the engine half of Disconnect.

A disconnect that only flipped CareAgents' own row left the engine willing:
an old /connect/<tenant> link, a late connection_success, a job retry or the
boot reaper could still import into the tenant. `revoke_tenant` writes the
tenant tombstone (which also covers a connection that has not arrived yet),
revokes every FastenConnection on the tenant and fails every unfinished job,
in the caller's request transaction. Fasten itself is not called (#911).

Flask-free so the route in r6/routes.py stays a thin gate.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from models import db
from r6.audit import add_audit_event
from r6.fasten.models import (FastenConnection, FastenJob,
                              FastenTenantRevocation)
from r6.fasten.reaper import TERMINAL_STATUSES

logger = logging.getLogger(__name__)


def revoke_tenant(tenant_id: str) -> dict:
    """Revoke and commit. Returns the PHI-free summary the route answers.

    Idempotent: a repeat changes nothing, writes no audit row, and reports
    already_revoked. Raises on any other failure, after rolling back.
    """
    now = datetime.now(timezone.utc)
    try:
        already = db.session.get(FastenTenantRevocation, tenant_id) is not None
        if not already:
            db.session.add(FastenTenantRevocation(tenant_id=tenant_id,
                                                  revoked_at=now))
        connections = (FastenConnection.query
                       .filter(FastenConnection.tenant_id == tenant_id,
                               FastenConnection.connection_status != 'revoked')
                       .update({'connection_status': 'revoked'},
                               synchronize_session=False))
        jobs = (FastenJob.query
                .filter(FastenJob.tenant_id == tenant_id,
                        FastenJob.status.notin_(TERMINAL_STATUSES))
                .update({'status': 'failed',
                         'failure_reason': 'disconnected',
                         'completed_at': now},
                        synchronize_session=False))
        if not already or connections or jobs:
            add_audit_event(
                event_type='fasten_connection_revoked',
                agent_id='careagents',
                tenant_id=tenant_id,
                outcome='success',
                detail='disconnected by account holder',
            )
        db.session.commit()
    except IntegrityError:
        # A concurrent revoke inserted the tombstone first and committed;
        # that request did this work.
        db.session.rollback()
        already, connections, jobs = True, 0, 0
    except Exception:
        db.session.rollback()
        raise
    return {
        'tenant_id': tenant_id,
        'revoked': True,
        'already_revoked': already,
        'connections_revoked': connections,
        'jobs_stopped': jobs,
    }
