"""Record what bytes a human approved: action_confirmations.payload_digest.

Revision ID: 0008_action_confirmation_payload_digest
Revises: 0007_agent_worker_presence

#559, human-gate spec 8.3: sha256 over the canonical payload at the moment
of approval, verified at execution. Nullable so existing rows survive; the
confirm route refuses to execute on a row that carries no digest.
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_action_confirmation_payload_digest"
down_revision = "0007_agent_worker_presence"
branch_labels = None
depends_on = None


def _columns():
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("action_confirmations")}


def upgrade() -> None:
    if "payload_digest" in _columns():
        return
    with op.batch_alter_table("action_confirmations") as batch:
        batch.add_column(sa.Column("payload_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    if "payload_digest" not in _columns():
        return
    with op.batch_alter_table("action_confirmations") as batch:
        batch.drop_column("payload_digest")
