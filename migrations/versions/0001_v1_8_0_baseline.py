"""Pre-Alembic v1.8.0 compatibility baseline.

Revision ID: 0001_v1_8_0
Revises: None

The baseline deliberately describes the oldest supported v1.8 deployment
shape. Revision 0002 replaces the former best-effort boot reconciler with
deterministic DDL for the known width, identity, and action-ledger changes.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_v1_8_0"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_confirmations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("approved_via", sa.String(32), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_action_confirmations"),
    )
    op.create_index(
        "ix_action_confirmations_action_id",
        "action_confirmations",
        ["action_id"],
    )

    op.create_table(
        "action_events",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_action_events"),
    )
    op.create_index("ix_action_events_action_id", "action_events", ["action_id"])
    op.create_index("ix_action_events_created_at", "action_events", ["created_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("context_id", sa.String(64), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("agent_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("recorded", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_context_id", "audit_events", ["context_id"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])

    op.create_table(
        "cc_agent_tasks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("resource_ref", sa.String(256), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cc_agent_tasks"),
    )
    for column in ("agent_id", "created_at", "status", "tenant_id"):
        op.create_index(f"ix_cc_agent_tasks_{column}", "cc_agent_tasks", [column])

    op.create_table(
        "cc_conversation_messages",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cc_conversation_messages"),
    )
    for column in ("agent_id", "created_at", "session_id", "tenant_id"):
        op.create_index(
            f"ix_cc_conversation_messages_{column}",
            "cc_conversation_messages",
            [column],
        )

    op.create_table(
        "context_envelopes",
        sa.Column("context_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("patient_ref", sa.String(128), nullable=False),
        sa.Column("encounter_ref", sa.String(128), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("window_end", sa.DateTime(), nullable=True),
        sa.Column("redaction_profile", sa.String(64), nullable=True),
        sa.Column("consent_decision", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("context_id", name="pk_context_envelopes"),
    )
    op.create_index(
        "ix_context_envelopes_tenant_id", "context_envelopes", ["tenant_id"]
    )

    op.create_table(
        "fasten_connections",
        sa.Column("org_connection_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("endpoint_id", sa.String(128), nullable=True),
        sa.Column("brand_id", sa.String(128), nullable=True),
        sa.Column("portal_id", sa.String(128), nullable=True),
        sa.Column("tefca_directory_id", sa.String(128), nullable=True),
        sa.Column("platform_type", sa.String(64), nullable=True),
        sa.Column("connection_status", sa.String(32), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_export_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("org_connection_id", name="pk_fasten_connections"),
    )
    op.create_index(
        "ix_fasten_connections_tenant_id", "fasten_connections", ["tenant_id"]
    )

    op.create_table(
        "fasten_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("org_connection_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("ingested_resources", sa.Integer(), nullable=True),
        sa.Column("skipped_resources", sa.Integer(), nullable=True),
        sa.Column("failed_resources", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_fasten_jobs"),
    )
    op.create_index(
        "ix_fasten_jobs_org_connection_id", "fasten_jobs", ["org_connection_id"]
    )
    op.create_index("ix_fasten_jobs_task_id", "fasten_jobs", ["task_id"], unique=True)
    op.create_index("ix_fasten_jobs_tenant_id", "fasten_jobs", ["tenant_id"])

    op.create_table(
        "proposed_actions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("outcome_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_proposed_actions"),
    )
    op.create_index("ix_proposed_actions_tenant_id", "proposed_actions", ["tenant_id"])

    op.create_table(
        "r6_resources",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.Column("resource_json", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_r6_resources_legacy"),
    )
    op.create_index("ix_r6_resources_resource_type", "r6_resources", ["resource_type"])
    op.create_index("ix_r6_resources_tenant_id", "r6_resources", ["tenant_id"])

    op.create_table(
        "smbp_sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("patient_ref", sa.String(128), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("started", sa.DateTime(), nullable=False),
        sa.Column("consent_captured", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_smbp_sessions"),
    )
    op.create_index("ix_smbp_sessions_tenant_id", "smbp_sessions", ["tenant_id"])

    op.create_table(
        "telegram_bindings",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("bound_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_bindings"),
        sa.UniqueConstraint("tenant_id", "chat_id", name="uq_tenant_chat"),
    )
    op.create_index("ix_telegram_bindings_chat_id", "telegram_bindings", ["chat_id"])
    op.create_index(
        "ix_telegram_bindings_tenant_id", "telegram_bindings", ["tenant_id"]
    )

    op.create_table(
        "wearable_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("ow_user_id", sa.String(128), nullable=False),
        sa.Column("patient_ref", sa.String(128), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_status", sa.String(32), nullable=True),
        sa.Column("last_sync_detail", sa.Text(), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_wearable_connections"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "ow_user_id",
            name="uq_wearable_tenant_provider_user",
        ),
    )
    op.create_index(
        "ix_wearable_connections_tenant_id", "wearable_connections", ["tenant_id"]
    )

    op.create_table(
        "context_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("context_id", sa.String(64), nullable=False),
        sa.Column("resource_ref", sa.String(128), nullable=False),
        sa.Column("resource_version", sa.String(16), nullable=True),
        sa.Column("slice_name", sa.String(64), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["context_id"],
            ["context_envelopes.context_id"],
            name="fk_context_items_context_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_context_items"),
    )
    op.create_index("ix_context_items_context_id", "context_items", ["context_id"])


def downgrade() -> None:
    op.drop_index("ix_context_items_context_id", table_name="context_items")
    op.drop_table("context_items")
    op.drop_index("ix_wearable_connections_tenant_id", table_name="wearable_connections")
    op.drop_table("wearable_connections")
    op.drop_index("ix_telegram_bindings_tenant_id", table_name="telegram_bindings")
    op.drop_index("ix_telegram_bindings_chat_id", table_name="telegram_bindings")
    op.drop_table("telegram_bindings")
    op.drop_index("ix_smbp_sessions_tenant_id", table_name="smbp_sessions")
    op.drop_table("smbp_sessions")
    op.drop_index("ix_r6_resources_tenant_id", table_name="r6_resources")
    op.drop_index("ix_r6_resources_resource_type", table_name="r6_resources")
    op.drop_table("r6_resources")
    op.drop_index("ix_proposed_actions_tenant_id", table_name="proposed_actions")
    op.drop_table("proposed_actions")
    op.drop_index("ix_fasten_jobs_tenant_id", table_name="fasten_jobs")
    op.drop_index("ix_fasten_jobs_task_id", table_name="fasten_jobs")
    op.drop_index("ix_fasten_jobs_org_connection_id", table_name="fasten_jobs")
    op.drop_table("fasten_jobs")
    op.drop_index("ix_fasten_connections_tenant_id", table_name="fasten_connections")
    op.drop_table("fasten_connections")
    op.drop_index("ix_context_envelopes_tenant_id", table_name="context_envelopes")
    op.drop_table("context_envelopes")
    for column in ("tenant_id", "session_id", "created_at", "agent_id"):
        op.drop_index(
            f"ix_cc_conversation_messages_{column}",
            table_name="cc_conversation_messages",
        )
    op.drop_table("cc_conversation_messages")
    for column in ("tenant_id", "status", "created_at", "agent_id"):
        op.drop_index(f"ix_cc_agent_tasks_{column}", table_name="cc_agent_tasks")
    op.drop_table("cc_agent_tasks")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_index("ix_audit_events_context_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_action_events_created_at", table_name="action_events")
    op.drop_index("ix_action_events_action_id", table_name="action_events")
    op.drop_table("action_events")
    op.drop_index(
        "ix_action_confirmations_action_id", table_name="action_confirmations"
    )
    op.drop_table("action_confirmations")
