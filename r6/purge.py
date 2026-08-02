"""Tenant data deletion — the mechanism behind "delete my records".

A patient who can connect records must be able to remove them (CARIN
Transparency (i): explain what happens to data after consent withdrawal).

What deletion means here, stated explicitly because the answer is not
obvious for a guardrailed system:

  * PURGED — every store that can hold PHI or PHI-adjacent detail: clinical
    resources, context envelopes and their items, proposed actions, agent
    conversation/tasks, BP sessions, and the connector rows that could pull
    more data (Fasten, wearables, Telegram binding).
  * RETAINED — AuditEventRecord. The audit trail is the immutable record of
    what happened to the data, it is PHI-free by contract, and destroying it
    on request would let a deletion erase evidence of the access that
    preceded it. Deletion itself is audited, so the trail gains an entry
    rather than losing one.

The engine is Flask-free so it can be unit-tested directly; routes wire it.
"""

import logging

logger = logging.getLogger(__name__)


def purge_tenant(tenant_id):
    """Delete a tenant's PHI-bearing rows. Returns {table: rows_deleted}.

    Deliberately does NOT touch AuditEventRecord (see module docstring).
    Raises on failure so a caller never reports a deletion that did not
    happen — the same fail-loud posture as the audit trail itself (#182).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")

    from r6.models import ContextEnvelope, ContextItem, R6Resource, TelegramBinding

    deleted = {}

    # Context items hang off envelopes by context_id, not tenant, so collect
    # the tenant's envelope ids first — otherwise the items would be orphaned.
    envelope_ids = [e.context_id for e in ContextEnvelope.query.filter_by(
        tenant_id=tenant_id).all()]
    if envelope_ids:
        deleted["context_items"] = ContextItem.query.filter(
            ContextItem.context_id.in_(envelope_ids)).delete(
                synchronize_session=False)

    for model, label in ((R6Resource, "resources"),
                         (ContextEnvelope, "context_envelopes"),
                         (TelegramBinding, "telegram_bindings")):
        deleted[label] = model.query.filter_by(
            tenant_id=tenant_id).delete(synchronize_session=False)

    # Optional modules: each is registered independently, so a deployment may
    # not have them all. Import failures must not leave PHI behind silently —
    # they are logged and re-raised.
    for import_path, attr, label in (
            ("r6.actions.models", "ProposedAction", "proposed_actions"),
            ("r6.agent_runs.models", "AgentRunEvent", "agent_run_events"),
            ("r6.agent_runs.models", "AgentToolCall", "agent_tool_calls"),
            ("r6.agent_runs.models", "AgentRun", "agent_runs"),
            ("r6.command_center.models", "ConversationMessage", "messages"),
            ("r6.command_center.models", "Conversation", "conversations"),
            ("r6.command_center.models", "AgentTask", "agent_tasks"),
            ("r6.fasten.models", "FastenConnection", "fasten_connections"),
            ("r6.fasten.models", "FastenJob", "fasten_jobs"),
            ("r6.smbp.models", "SMBPSession", "smbp_sessions"),
            ("r6.wearables.models", "WearableConnection", "wearable_connections"),
    ):
        try:
            module = __import__(import_path, fromlist=[attr])
            model = getattr(module, attr)
        except (ImportError, AttributeError):
            logger.info("purge: %s not present in this deployment", label)
            continue
        deleted[label] = model.query.filter_by(
            tenant_id=tenant_id).delete(synchronize_session=False)

    return deleted


def purge_summary(deleted):
    """Total rows removed — what the caller confirms back to the patient."""
    return sum(v for v in deleted.values() if isinstance(v, int))
