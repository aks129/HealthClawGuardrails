"""Separate public AuditEvent outcome evidence from internal audit detail.

Revision ID: 0003_audit_outcome_detail
Revises: 0002_current_contract
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0003_audit_outcome_detail"
down_revision = "0002_current_contract"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    return name in {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("audit_events")
    }


def upgrade() -> None:
    # Legacy databases are stamped at 0001 after loading current model
    # metadata, so they may already have this column when Alembic reaches 0003.
    if not _has_column("outcome_detail_code"):
        op.add_column(
            "audit_events",
            sa.Column("outcome_detail_code", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    if _has_column("outcome_detail_code"):
        op.drop_column("audit_events", "outcome_detail_code")
