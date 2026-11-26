"""Saved contacts: delegation_contacts.

Revision ID: 0013_delegation_contacts
Revises: 0012_shl_links

r6/actions/contacts.py. The business numbers a person attested may be
called, per tenant. Purged with the tenant.
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_delegation_contacts"
down_revision = "0012_shl_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "delegation_contacts" in tables:
        return
    op.create_table(
        "delegation_contacts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("phone_e164", sa.String(16), nullable=False),
        sa.Column("callee_kind", sa.String(16), nullable=False),
        sa.Column("tz", sa.String(64), nullable=False),
        sa.Column("attested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_delegation_contacts"),
    )
    op.create_index("ix_delegation_contacts_tenant_id",
                    "delegation_contacts", ["tenant_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "delegation_contacts" not in tables:
        return
    op.drop_index("ix_delegation_contacts_tenant_id",
                  table_name="delegation_contacts")
    op.drop_table("delegation_contacts")
