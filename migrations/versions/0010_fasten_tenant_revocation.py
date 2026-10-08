"""Record a tenant whose Fasten access the account holder took back.

Revision ID: 0010_fasten_tenant_revocation
Revises: 0009_audit_append_only

A CareAgents disconnect or delete writes one row per tenant. Every path that
could bring records into the tenant checks it, including a connection that
arrives after the disconnect and so had no row to flip.
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_fasten_tenant_revocation"
down_revision = "0009_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "fasten_tenant_revocations" in tables:
        return
    op.create_table(
        "fasten_tenant_revocations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_fasten_tenant_revocations"),
    )


def downgrade() -> None:
    op.drop_table("fasten_tenant_revocations")
