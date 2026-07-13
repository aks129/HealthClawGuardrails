"""Replace boot-time schema reconciliation with deterministic DDL.

Revision ID: 0002_current_contract
Revises: 0001_v1_8_0

This revision formalizes the known drift previously repaired by
``r6.schema_sync``: the tenant-scoped resource identity, externally issued ID
widths, the action attempt ledger, Fasten recovery fields, and curation fields.
It is conditional so an existing v1.8 database already reconciled at boot can
be stamped at the baseline and upgraded safely without duplicate-column DDL.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0002_current_contract"
down_revision = "0001_v1_8_0"
branch_labels = None
depends_on = None


def _columns(table: str) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(op.get_bind()).get_columns(table)}


def _add_missing(table: str, columns: list[sa.Column]) -> None:
    existing = _columns(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def _widen(table: str, column: str, old_length: int, new_length: int) -> None:
    live = _columns(table)[column]
    live_length = getattr(live["type"], "length", None)
    if live_length is None or live_length >= new_length:
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.alter_column(
                column,
                existing_type=sa.String(old_length),
                type_=sa.String(new_length),
                existing_nullable=live["nullable"],
            )
    else:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(old_length),
            type_=sa.String(new_length),
            existing_nullable=live["nullable"],
        )


def _upgrade_resource_identity() -> None:
    bind = op.get_bind()
    schema = inspect(bind)
    pk = schema.get_pk_constraint("r6_resources")
    expected = ["tenant_id", "resource_type", "id"]

    if bind.dialect.name == "sqlite":
        columns = _columns("r6_resources")
        needs_rebuild = (
            pk.get("constrained_columns") != expected
            or columns["tenant_id"]["nullable"]
            or getattr(columns["id"]["type"], "length", None) != 255
        )
        if not needs_rebuild:
            return
        constraint_name = pk.get("name") or "pk_r6_resources_legacy"
        batch_kwargs = {"recreate": "always"}
        if not pk.get("name"):
            # A legacy-boot-era SQLite PK is unnamed; drop_constraint would
            # raise "No such constraint". Alembic's documented recipe: give
            # reflection a naming convention so the legacy PK gets exactly the
            # deterministic name we then drop.
            batch_kwargs["naming_convention"] = {"pk": "pk_%(table_name)s_legacy"}
        with op.batch_alter_table("r6_resources", **batch_kwargs) as batch:
            batch.alter_column(
                "id",
                existing_type=sa.String(64),
                type_=sa.String(255),
                existing_nullable=False,
            )
            batch.alter_column(
                "tenant_id",
                existing_type=sa.String(64),
                nullable=False,
            )
            if pk.get("constrained_columns") != expected:
                batch.drop_constraint(constraint_name, type_="primary")
                batch.create_primary_key("pk_r6_resources_identity", expected)
        return

    _widen("r6_resources", "id", 64, 255)
    columns = _columns("r6_resources")
    if columns["tenant_id"]["nullable"]:
        op.alter_column(
            "r6_resources",
            "tenant_id",
            existing_type=sa.String(64),
            nullable=False,
        )
    if columns["resource_type"]["nullable"]:
        op.alter_column(
            "r6_resources",
            "resource_type",
            existing_type=sa.String(64),
            nullable=False,
        )
    if pk.get("constrained_columns") != expected:
        if not pk.get("name"):
            raise RuntimeError("r6_resources has an unnamed legacy primary key")
        op.drop_constraint(pk["name"], "r6_resources", type_="primary")
        op.create_primary_key("pk_r6_resources_identity", "r6_resources", expected)


def upgrade() -> None:
    _add_missing(
        "r6_resources",
        [
            sa.Column("curation_state", sa.String(32), nullable=True),
            sa.Column("quality_score", sa.Float(), nullable=True),
            sa.Column("review_needed", sa.Boolean(), nullable=True),
        ],
    )
    _add_missing(
        "fasten_connections",
        [
            sa.Column("webhook_verified_at", sa.DateTime(), nullable=True),
            sa.Column("agent_token_issued_at", sa.DateTime(), nullable=True),
            sa.Column("enrollment_proof_hash", sa.String(64), nullable=True),
            sa.Column("enrollment_expires_at", sa.DateTime(), nullable=True),
        ],
    )
    _add_missing(
        "fasten_jobs",
        [sa.Column("download_links_json", sa.Text(), nullable=True)],
    )
    _add_missing(
        "proposed_actions",
        [
            sa.Column("attempt_id", sa.String(64), nullable=True),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.Column("provider_request_at", sa.DateTime(), nullable=True),
        ],
    )

    _upgrade_resource_identity()
    _widen("audit_events", "resource_id", 64, 255)
    _widen("fasten_connections", "org_connection_id", 64, 255)
    _widen("fasten_jobs", "task_id", 64, 255)
    _widen("fasten_jobs", "org_connection_id", 64, 255)
    _widen("proposed_actions", "status", 16, 32)
    _widen("action_events", "from_status", 32, 128)


def downgrade() -> None:
    """Return to the compatibility baseline; may fail if data exceeds it."""
    # Narrowing and restoring the global resource PK are intentionally
    # explicit and will fail safely when current data cannot fit the legacy
    # schema. Operators should restore a snapshot instead of forcing it.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("r6_resources", recreate="always") as batch:
            batch.drop_constraint("pk_r6_resources_identity", type_="primary")
            batch.create_primary_key("pk_r6_resources_legacy", ["id"])
            batch.alter_column(
                "tenant_id", existing_type=sa.String(64), nullable=True
            )
            batch.alter_column(
                "id", existing_type=sa.String(255), type_=sa.String(64)
            )
    else:
        pk = inspect(bind).get_pk_constraint("r6_resources")
        op.drop_constraint(pk["name"], "r6_resources", type_="primary")
        op.create_primary_key(
            "pk_r6_resources_legacy", "r6_resources", ["id"]
        )
        op.alter_column(
            "r6_resources",
            "tenant_id",
            existing_type=sa.String(64),
            nullable=True,
        )
        op.alter_column(
            "r6_resources",
            "id",
            existing_type=sa.String(255),
            type_=sa.String(64),
        )

    for table, column, old_length, new_length in (
        ("audit_events", "resource_id", 255, 64),
        ("fasten_connections", "org_connection_id", 255, 64),
        ("fasten_jobs", "task_id", 255, 64),
        ("fasten_jobs", "org_connection_id", 255, 64),
        ("proposed_actions", "status", 32, 16),
        ("action_events", "from_status", 128, 32),
    ):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column(
                    column,
                    existing_type=sa.String(old_length),
                    type_=sa.String(new_length),
                )
        else:
            op.alter_column(
                table,
                column,
                existing_type=sa.String(old_length),
                type_=sa.String(new_length),
            )

    for table, columns in (
        ("proposed_actions", ["provider_request_at", "claimed_at", "attempt_id"]),
        ("fasten_jobs", ["download_links_json"]),
        (
            "fasten_connections",
            [
                "enrollment_expires_at",
                "enrollment_proof_hash",
                "agent_token_issued_at",
                "webhook_verified_at",
            ],
        ),
        ("r6_resources", ["review_needed", "quality_score", "curation_state"]),
    ):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table, recreate="always") as batch:
                for column in columns:
                    batch.drop_column(column)
        else:
            for column in columns:
                op.drop_column(table, column)
