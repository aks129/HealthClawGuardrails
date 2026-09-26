"""Make audit_events append-only at the database.

Revision ID: 0009_audit_append_only
Revises: 0008_confirmation_digest

The ORM listeners on AuditEventRecord block Session.delete() and dirty
flushes, but a bulk Query.update/delete or raw SQL never reaches them. This
installs BEFORE UPDATE / BEFORE DELETE triggers that refuse the statement on
PostgreSQL and SQLite. The DDL lives in r6/audit_ddl.py, shared with the
model's after_create hook; every statement is idempotent, since an adopted
legacy schema may already carry the triggers.

A later SQLite batch_alter_table("audit_events") rebuilds the table and
drops these triggers; such a migration must re-run install_statements.
"""

from alembic import op

from r6.audit_ddl import install_statements, remove_statements


revision = "0009_audit_append_only"
down_revision = "0008_confirmation_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in install_statements(op.get_bind().dialect.name):
        op.execute(statement)


def downgrade() -> None:
    for statement in remove_statements(op.get_bind().dialect.name):
        op.execute(statement)
