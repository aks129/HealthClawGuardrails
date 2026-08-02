"""Add durable queue-access presence for agent workers.

Revision ID: 0007_agent_worker_presence
Revises: 0006_tool_reconciliation_state
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_agent_worker_presence"
down_revision = "0006_tool_reconciliation_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_worker_presence" in tables:
        return
    op.create_table(
        "agent_worker_presence",
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name="pk_agent_worker_presence"),
    )
    op.create_index(
        "ix_agent_worker_presence_last_seen_at",
        "agent_worker_presence",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_worker_presence_last_seen_at",
        table_name="agent_worker_presence",
    )
    op.drop_table("agent_worker_presence")
