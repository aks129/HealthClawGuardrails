"""Add durable agent-run queue, tool calls, and append-only events.

Revision ID: 0005_agent_run_control_plane
Revises: 0004_conversation_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_agent_run_control_plane"
down_revision = "0004_conversation_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    run_tables = {"agent_runs", "agent_tool_calls", "agent_run_events"}
    if run_tables <= tables:
        return
    if run_tables & tables:
        raise RuntimeError(
            "partial agent-run schema detected; refusing an ambiguous upgrade")

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("surface", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["cc_conversations.tenant_id", "cc_conversations.id"],
            name="fk_agent_run_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["cc_conversation_messages.id"],
            name="fk_agent_run_message", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "message_id", name="uq_agent_run_message"),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_for_human',"
            "'completed','failed','cancelled')",
            name="ck_agent_run_status"),
    )
    for column in (
        "agent_id", "available_at", "conversation_id", "created_at",
        "deadline_at", "error_class", "lease_expires_at", "message_id",
        "status", "tenant_id", "worker_id",
    ):
        op.create_index(f"ix_agent_runs_{column}", "agent_runs", [column])

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("provider_call_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("outcome_ref", sa.String(256), nullable=True),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tool_calls"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"],
            name="fk_agent_tool_call_run", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id", "provider_call_id",
            name="uq_agent_tool_call_provider"),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_agent_tool_call_status"),
    )
    for column in (
        "created_at", "outcome_ref", "run_id", "status", "tenant_id",
        "tool_name",
    ):
        op.create_index(
            f"ix_agent_tool_calls_{column}", "agent_tool_calls", [column])

    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run_events"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"],
            name="fk_agent_run_event_run", ondelete="CASCADE"),
    )
    for column in ("created_at", "event_type", "run_id", "tenant_id"):
        op.create_index(
            f"ix_agent_run_events_{column}", "agent_run_events", [column])


def downgrade() -> None:
    for column in ("tenant_id", "run_id", "event_type", "created_at"):
        op.drop_index(
            f"ix_agent_run_events_{column}", table_name="agent_run_events")
    op.drop_table("agent_run_events")

    for column in (
        "tool_name", "tenant_id", "status", "run_id", "outcome_ref",
        "created_at",
    ):
        op.drop_index(
            f"ix_agent_tool_calls_{column}", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")

    for column in (
        "worker_id", "tenant_id", "status", "message_id",
        "lease_expires_at", "error_class", "deadline_at", "created_at",
        "conversation_id", "available_at", "agent_id",
    ):
        op.drop_index(f"ix_agent_runs_{column}", table_name="agent_runs")
    op.drop_table("agent_runs")
