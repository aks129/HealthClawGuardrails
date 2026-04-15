"""
AuditEvent middleware for R6 FHIR operations.

All reads and writes emit AuditEvent records.
R6 defines AuditEvent as a record of events relevant for
operations, privacy, security, maintenance, and performance analysis.
"""

import logging
from models import db
from r6.models import AuditEventRecord
from r6.health_context import get as _hc_get

logger = logging.getLogger(__name__)


def record_audit_event(event_type, resource_type=None, resource_id=None,
                       agent_id=None, context_id=None, outcome='success',
                       detail=None, tenant_id=None):
    """
    Record an audit event for a FHIR operation.

    Uses a nested transaction (SAVEPOINT) so that audit failures
    do not roll back the caller's already-committed work.

    Args:
        event_type: Type of event (read, create, update, delete, validate)
        resource_type: FHIR resource type involved
        resource_id: ID of the resource involved
        agent_id: Identifier of the agent/user performing the action
        context_id: Context envelope ID if applicable
        outcome: Event outcome (success, failure)
        detail: Additional detail text
        tenant_id: Tenant identifier for isolation
    """
    try:
        nested = db.session.begin_nested()
        audit = AuditEventRecord(
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            context_id=context_id,
            tenant_id=tenant_id,
            agent_id=(
                agent_id
                or _hc_get('audit_agent_default', 'healthclaw-guardrails')
            ),
            outcome=outcome,
            detail=detail
        )
        db.session.add(audit)
        nested.commit()
        db.session.commit()
        logger.debug(
            f'AuditEvent recorded: {event_type} on {resource_type}/{resource_id} '
            f'by {agent_id or "system"}'
        )
    except Exception as e:
        logger.error(f'Failed to record audit event: {e}')
        # Roll back only the nested savepoint, not the caller's transaction
        try:
            db.session.rollback()
        except Exception:
            pass
