"""Attested facts: delegation_facts.

Revision ID: 0014_delegation_facts
Revises: 0013_delegation_contacts

r6/actions/facts.py. What a person attested about themselves for a call,
one row per tenant and key. Purged with the tenant.
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_delegation_facts"
down_revision = "0013_delegation_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "delegation_facts" in tables:
        return
    op.create_table(
        "delegation_facts",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("attested_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "key",
                                name="pk_delegation_facts"),
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "delegation_facts" not in tables:
        return
    op.drop_table("delegation_facts")
