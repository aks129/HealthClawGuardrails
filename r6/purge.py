"""Tenant data deletion — the mechanism behind "delete my records".

A patient who can connect records must be able to remove them (CARIN
Transparency (i): explain what happens to data after consent withdrawal).

What deletion means here, stated explicitly because the answer is not
obvious for a guardrailed system:

  * PURGED — every store that can hold PHI or PHI-adjacent detail: clinical
    resources, context envelopes and their items, proposed actions and their
    lifecycle events and approval records, agent
    conversation/tasks, BP sessions, and the connector rows that could pull
    more data (Fasten, wearables, Telegram binding).
  * RETAINED — AuditEventRecord. The audit trail is the immutable record of
    what happened to the data, it is PHI-free by contract, and destroying it
    on request would let a deletion erase evidence of the access that
    preceded it. Deletion itself is audited, so the trail gains an entry
    rather than losing one.
  * WRITTEN — a TenantClosure tombstone (tenant id and time only),
    so nothing Fasten delivers later can refill the emptied tenant.

The engine is Flask-free so it can be unit-tested directly; routes wire it.
"""

import logging

logger = logging.getLogger(__name__)


#: (table, label) for the tenant-keyed tables purged by name, in delete order.
#: The labels are the keys of the summary purge_tenant returns.
_TENANT_TABLES = (
    ("proposed_actions", "proposed_actions"),
    ("agent_run_events", "agent_run_events"),
    ("agent_tool_calls", "agent_tool_calls"),
    ("agent_runs", "agent_runs"),
    ("cc_conversation_messages", "messages"),
    ("cc_conversations", "conversations"),
    ("cc_agent_tasks", "agent_tasks"),
    ("fasten_connections", "fasten_connections"),
    ("fasten_jobs", "fasten_jobs"),
    ("smbp_sessions", "smbp_sessions"),
    ("wearable_connections", "wearable_connections"),
)


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

    # Same shape for actions (#217): events and confirmations hang off an
    # action by action_id, not tenant, so collect the ids before the actions
    # themselves are deleted below.
    try:
        from r6.actions.confirmations import ActionConfirmation
        from r6.actions.events import ActionEvent
        from r6.actions.models import ProposedAction
    except ImportError:
        logger.info("purge: action rail not present in this deployment")
    else:
        action_ids = [a.id for a in ProposedAction.query.filter_by(
            tenant_id=tenant_id).all()]
        if action_ids:
            for model, label in ((ActionEvent, "action_events"),
                                 (ActionConfirmation, "action_confirmations")):
                deleted[label] = model.query.filter(
                    model.action_id.in_(action_ids)).delete(
                        synchronize_session=False)

    for model, label in ((R6Resource, "resources"),
                         (ContextEnvelope, "context_envelopes"),
                         (TelegramBinding, "telegram_bindings")):
        deleted[label] = model.query.filter_by(
            tenant_id=tenant_id).delete(synchronize_session=False)

    # Every other tenant-keyed store, by TABLE name and in this order (child
    # rows before the rows they reference). Named here rather than taken from
    # the loaded extensions: the migrations create every one of these tables
    # whichever optional modules a process loads, so a deployment started
    # without a module still holds that module's rows and must still delete
    # them. A migrated database has every one, and a database built with
    # create_all has every table a loaded model defines. A table missing
    # where it should exist means the schema is not the one this purge was
    # written for, or reflection came back short: refuse rather than report
    # a deletion that skipped it. Only a table no model defines, in a
    # database the migrations never ran on, can hold no rows and is skipped.
    from sqlalchemy import inspect, text

    from models import db

    present = set(inspect(db.session.connection()).get_table_names())
    migrated = "alembic_version" in present
    missing = [table for table, _label in _TENANT_TABLES
               if table not in present
               and (migrated or table in db.metadata.tables)]
    if missing:
        raise RuntimeError(
            "purge: tenant tables missing from the database: "
            + ", ".join(missing))
    purged = [(table, label) for table, label in _TENANT_TABLES
              if table in present]
    for table, label in purged:
        deleted[label] = db.session.execute(
            text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id}).rowcount

    # A purge never reports success over rows it left behind.
    for table, _label in purged:
        left = db.session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id}).scalar()
        if left:
            raise RuntimeError(f"purge: {table} still holds tenant rows")

    # The Fasten rows are gone, but a late connection_success, an old
    # connect link or the boot reaper could start a new import into the
    # emptied tenant. The tombstone is what they check; it holds no PHI and
    # is retained, like the audit trail.
    # Not for a public tenant: purging one is open by design (the demo
    # resets), and a permanent tombstone would close it for good.
    from r6.tenancy import is_public
    from r6.fasten.models import TenantClosure
    if (not is_public(tenant_id)
            and db.session.get(TenantClosure, tenant_id) is None):
        db.session.add(TenantClosure(tenant_id=tenant_id))

    return deleted


def purge_summary(deleted):
    """Total rows removed — what the caller confirms back to the patient."""
    return sum(v for v in deleted.values() if isinstance(v, int))
