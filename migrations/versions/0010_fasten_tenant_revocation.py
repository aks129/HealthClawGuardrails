"""Record a tenant the account holder closed: tenant_closures.

Revision ID: 0010_fasten_tenant_revocation
Revises: 0009_audit_append_only

A CareAgents disconnect or delete writes one row per tenant, and no new
records may arrive in it after that: every Fasten import path and every
record-writing route checks it. The table started as a Fasten-only
revocation and was renamed before it reached production; the revision id
kept its first name.
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_fasten_tenant_revocation"
down_revision = "0009_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "tenant_closures" in tables:
        return
    op.create_table(
        "tenant_closures",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_closures"),
    )


def downgrade() -> None:
    op.drop_table("tenant_closures")
