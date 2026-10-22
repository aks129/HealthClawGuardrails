"""SMART Health Link store: shl_links.

Revision ID: 0012_shl_links
Revises: 0011_record_source

r6/shl_links.py. Ciphertext only, keyed by a SHA-256 of the link id; no
key, no plaintext, no patient id. Rows expire within SHL_LINK_TTL_SECONDS
and are deleted on the next issue after that.
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_shl_links"
down_revision = "0011_record_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "shl_links" in tables:
        return
    op.create_table(
        "shl_links",
        sa.Column("id_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("exp", sa.BigInteger(), nullable=False),
        sa.Column("uses_left", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id_hash", name="pk_shl_links"),
    )
    op.create_index("ix_shl_links_tenant_id", "shl_links", ["tenant_id"])
    op.create_index("ix_shl_links_exp", "shl_links", ["exp"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "shl_links" not in tables:
        return
    op.drop_index("ix_shl_links_exp", table_name="shl_links")
    op.drop_index("ix_shl_links_tenant_id", table_name="shl_links")
    op.drop_table("shl_links")
