"""Add durable conversation identity and idempotent message metadata.

Revision ID: 0004_conversation_identity
Revises: 0003_audit_outcome_detail

Existing tenant-wide transcripts are preserved in one documented compatibility
thread named ``legacy:<tenant_id>``. New callers should always provide an
explicit conversation ID.
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_conversation_identity"
down_revision = "0003_audit_outcome_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A short-lived pre-Alembic boot path built metadata from the current
    # checkout. Such a database can contain this complete schema while
    # an older table still needs 0002's reconciliation. After 0002 repairs that
    # table, treat the already-current conversation schema as adopted.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "cc_conversations" in tables:
        message_columns = {
            column["name"]
            for column in inspector.get_columns("cc_conversation_messages")
        }
        if {"conversation_id", "request_id", "reply_to"} <= message_columns:
            return

    op.create_table(
        "cc_conversations",
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("created_by_surface", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "id", name="pk_cc_conversations"),
    )
    for column in ("agent_id", "created_at", "status", "tenant_id", "updated_at"):
        op.create_index(
            f"ix_cc_conversations_{column}", "cc_conversations", [column])

    with op.batch_alter_table("cc_conversation_messages") as batch:
        batch.add_column(sa.Column("conversation_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("request_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("reply_to", sa.String(64), nullable=True))

    # One compatibility thread per tenant preserves every pre-migration row.
    op.execute(sa.text("""
        INSERT INTO cc_conversations
            (id, tenant_id, agent_id, created_by_surface, status,
             created_at, updated_at)
        SELECT
            'legacy:' || tenant_id,
            tenant_id,
            NULL,
            'legacy',
            'active',
            COALESCE(MIN(created_at), CURRENT_TIMESTAMP),
            COALESCE(MAX(created_at), CURRENT_TIMESTAMP)
        FROM cc_conversation_messages
        GROUP BY tenant_id
    """))
    op.execute(sa.text("""
        UPDATE cc_conversation_messages
        SET conversation_id = 'legacy:' || tenant_id
        WHERE conversation_id IS NULL
    """))

    with op.batch_alter_table(
            "cc_conversation_messages", recreate="always") as batch:
        batch.alter_column(
            "conversation_id", existing_type=sa.String(128), nullable=False)
        batch.create_foreign_key(
            "fk_cc_message_conversation", "cc_conversations",
            ["tenant_id", "conversation_id"], ["tenant_id", "id"],
            ondelete="CASCADE")
        batch.create_unique_constraint(
            "uq_cc_message_request",
            ["tenant_id", "conversation_id", "request_id"])

    for column in ("conversation_id", "reply_to", "request_id"):
        op.create_index(
            f"ix_cc_conversation_messages_{column}",
            "cc_conversation_messages", [column])


def downgrade() -> None:
    for column in ("request_id", "reply_to", "conversation_id"):
        op.drop_index(
            f"ix_cc_conversation_messages_{column}",
            table_name="cc_conversation_messages")
    with op.batch_alter_table(
            "cc_conversation_messages", recreate="always") as batch:
        batch.drop_constraint("uq_cc_message_request", type_="unique")
        batch.drop_constraint("fk_cc_message_conversation", type_="foreignkey")
        batch.drop_column("reply_to")
        batch.drop_column("request_id")
        batch.drop_column("conversation_id")

    for column in ("updated_at", "tenant_id", "status", "created_at", "agent_id"):
        op.drop_index(
            f"ix_cc_conversations_{column}", table_name="cc_conversations")
    op.drop_table("cc_conversations")
