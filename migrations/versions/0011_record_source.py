"""Record how a resource arrived: r6_resources.source.

Revision ID: 0011_record_source
Revises: 0010_fasten_tenant_revocation

FHI-42: one of a closed set of channel codes (fasten, upload, sample,
connected-app), set by the ingest path and emitted as a code-only meta.tag.
Nullable with no backfill: existing rows emit no tag, because inferring a
channel for them would be a guess. The code set is enforced by the model,
not by a database constraint, so adding a channel needs no migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_record_source"
down_revision = "0010_fasten_tenant_revocation"
branch_labels = None
depends_on = None


def _columns():
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("r6_resources")}


def upgrade() -> None:
    if "source" in _columns():
        return
    with op.batch_alter_table("r6_resources") as batch:
        batch.add_column(sa.Column("source", sa.String(16), nullable=True))


def downgrade() -> None:
    if "source" not in _columns():
        return
    with op.batch_alter_table("r6_resources") as batch:
        batch.drop_column("source")
