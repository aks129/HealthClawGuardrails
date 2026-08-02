"""Add fail-closed reconciliation state for ambiguous tool outcomes.

Revision ID: 0006_tool_reconciliation_state
Revises: 0005_agent_run_control_plane
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_tool_reconciliation_state"
down_revision = "0005_agent_run_control_plane"
branch_labels = None
depends_on = None


def _replace_constraint(allowed: str) -> None:
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.drop_constraint("ck_agent_tool_call_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_tool_call_status", f"status IN ({allowed})")


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_tool_calls" not in tables:
        raise RuntimeError(
            "agent_tool_calls is missing; apply the 0005 foundation first")
    _replace_constraint(
        "'pending','running','completed','failed','needs_reconciliation'")


def downgrade() -> None:
    bind = op.get_bind()
    unresolved = bind.execute(sa.text(
        "SELECT COUNT(*) FROM agent_tool_calls "
        "WHERE status = 'needs_reconciliation'"
    )).scalar_one()
    if unresolved:
        raise RuntimeError(
            "cannot downgrade while tool calls need reconciliation")
    _replace_constraint("'pending','running','completed','failed'")
