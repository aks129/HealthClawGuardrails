#!/usr/bin/env python
"""Migrate r6_resources to the composite identity PK (tenant_id, resource_type, id).

Why: the original schema's PRIMARY KEY was the raw FHIR id alone — global
across tenants AND resource types. FHIR ids are only unique per resource
type per source server, so a second tenant importing an id the first tenant
already held (Synthea 'example', Epic numeric ids) collided on the PK and
the resource was silently dropped from the import. See
tests/test_resource_identity.py and
docs/runbooks/resource-identity-migration.md.

Usage:
    SQLALCHEMY_DATABASE_URI=postgresql://... python scripts/migrate_resource_identity.py --dry-run
    SQLALCHEMY_DATABASE_URI=postgresql://... python scripts/migrate_resource_identity.py

Properties:
  * Postgres-only. Aborts on any other dialect (SQLite dev DBs are
    recreated from the model by create_all() and never need this).
  * Idempotent. If the PK is already (tenant_id, resource_type, id) it
    exits 0 without touching anything.
  * Pre-checked. Refuses to run if any rows would violate the new PK:
      - duplicate (tenant_id, resource_type, id) triples
        (impossible under the old global PK, but VERIFIED, never assumed)
      - NULL tenant_id or resource_type (PK columns must be NOT NULL)
  * Transactional. The constraint swap runs in a single transaction; any
    failure rolls the whole swap back.
  * Verified. After commit, the new constraint is read back from
    pg_constraint and its column list checked exactly.
  * --dry-run prints the pre-check results and the exact statements
    without executing the swap.

Exit codes: 0 ok / already migrated; 1 precondition or verification failure.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

TABLE = "r6_resources"
NEW_PK_COLUMNS = ["tenant_id", "resource_type", "id"]
NEW_PK_NAME = "pk_r6_resources_identity"

# The swap, as executed (inside one transaction). {old_pk} is discovered
# from pg_constraint at runtime — Flask-SQLAlchemy's create_all() named it
# r6_resources_pkey, but we never assume.
SWAP_STATEMENTS = [
    'ALTER TABLE {table} DROP CONSTRAINT "{old_pk}"',
    'ALTER TABLE {table} ADD CONSTRAINT "{new_pk}" '
    "PRIMARY KEY (tenant_id, resource_type, id)",
]


def _fail(msg: str) -> None:
    print(f"ABORT: {msg}", file=sys.stderr)
    sys.exit(1)


def _get_engine():
    uri = (os.environ.get("SQLALCHEMY_DATABASE_URI")
           or os.environ.get("DATABASE_URL"))
    if not uri:
        _fail("SQLALCHEMY_DATABASE_URI (or DATABASE_URL) must be set")
    # Railway hands out postgres:// which SQLAlchemy 2.x rejects
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    engine = create_engine(uri)
    if engine.dialect.name != "postgresql":
        _fail(f"this migration is Postgres-only (dialect: {engine.dialect.name}). "
              "SQLite databases are rebuilt from the model and never need it.")
    return engine


def _current_pk(conn) -> tuple[str, list[str]] | None:
    """Return (constraint_name, [columns in PK order]) or None if no PK."""
    row = conn.execute(text("""
        SELECT c.conname,
               ARRAY(
                   SELECT a.attname
                   FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                   JOIN pg_attribute a
                     ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                   ORDER BY k.ord
               ) AS cols
        FROM pg_constraint c
        WHERE c.conrelid = :table ::regclass
          AND c.contype = 'p'
    """), {"table": TABLE}).fetchone()
    if row is None:
        return None
    return row[0], list(row[1])


def _precheck(conn) -> list[str]:
    """Return a list of blocking problems (empty = safe to migrate)."""
    problems = []

    dup_rows = conn.execute(text(f"""
        SELECT tenant_id, resource_type, id, COUNT(*) AS n
        FROM {TABLE}
        GROUP BY tenant_id, resource_type, id
        HAVING COUNT(*) > 1
        LIMIT 20
    """)).fetchall()
    if dup_rows:
        problems.append(
            f"{len(dup_rows)}+ duplicate (tenant_id, resource_type, id) "
            f"triples exist — first few: "
            + ", ".join(f"({r[0]!r},{r[1]!r},{r[2]!r})x{r[3]}" for r in dup_rows[:5])
            + ". The old global PK should have made this impossible; "
            "investigate before migrating."
        )

    null_counts = conn.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE tenant_id IS NULL)     AS null_tenant,
            COUNT(*) FILTER (WHERE resource_type IS NULL) AS null_type
        FROM {TABLE}
    """)).fetchone()
    if null_counts[0]:
        problems.append(
            f"{null_counts[0]} rows have NULL tenant_id — PK columns must be "
            "NOT NULL. Backfill or delete these orphaned rows first "
            "(a NULL tenant is unreachable by every tenant-scoped query)."
        )
    if null_counts[1]:
        problems.append(f"{null_counts[1]} rows have NULL resource_type — "
                        "PK columns must be NOT NULL.")

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print pre-check results and the exact statements "
                             "without executing the swap")
    args = parser.parse_args()

    engine = _get_engine()

    # Phase 1 — inspect + pre-check, then CLOSE the connection. The swap
    # needs an ACCESS EXCLUSIVE lock; a still-open pre-check transaction
    # (even read-only) holds ACCESS SHARE on the table and would deadlock
    # the migration against itself.
    with engine.connect() as conn:
        pk = _current_pk(conn)
        if pk is None:
            _fail(f"table {TABLE} has no primary key — refusing to guess; "
                  "inspect the schema by hand.")
        old_pk_name, old_cols = pk

        row_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        print(f"table {TABLE}: {row_count} rows")
        print(f"current PK: {old_pk_name} ({', '.join(old_cols)})")

        if old_cols == NEW_PK_COLUMNS:
            print("already migrated — PK is (tenant_id, resource_type, id). "
                  "Nothing to do.")
            return

        if old_cols != ["id"]:
            _fail(f"unexpected current PK columns {old_cols} — expected ['id'] "
                  "(pre-migration) or the composite key (post-migration). "
                  "Inspect by hand before proceeding.")

        print("running pre-checks ...")
        problems = _precheck(conn)
        for p in problems:
            print(f"  PRE-CHECK FAILED: {p}", file=sys.stderr)
        if not problems:
            print("  pre-checks passed: 0 duplicate identity triples, "
                  "0 NULL tenant_id, 0 NULL resource_type")
        conn.rollback()  # end the read transaction; release ACCESS SHARE

    statements = [
        s.format(table=TABLE, old_pk=old_pk_name, new_pk=NEW_PK_NAME)
        for s in SWAP_STATEMENTS
    ]

    if args.dry_run:
        print("\n--dry-run: would execute in ONE transaction:")
        for s in statements:
            print(f"  {s};")
        if problems:
            print("\n--dry-run verdict: WOULD ABORT (pre-checks failed)",
                  file=sys.stderr)
            sys.exit(1)
        print("\n--dry-run verdict: safe to migrate")
        return

    if problems:
        _fail("pre-checks failed — not migrating")

    # Phase 2 — the swap, in ONE transaction on a fresh connection. Even if
    # rows changed between pre-check and now, ADD PRIMARY KEY re-validates
    # uniqueness and NOT NULL itself: a violation fails the transaction and
    # everything rolls back — the pre-check is a courtesy, Postgres is the
    # enforcement.
    print("swapping PK (single transaction) ...")
    with engine.begin() as tx:  # BEGIN ... COMMIT (rollback on error)
        for s in statements:
            print(f"  {s};")
            tx.execute(text(s))
    print("committed.")

    # Post-commit verification on a FRESH connection — prove the catalog
    # really holds the new constraint, not just that our transaction thinks so.
    with engine.connect() as conn:
        pk = _current_pk(conn)
        if pk is None or pk[0] != NEW_PK_NAME or pk[1] != NEW_PK_COLUMNS:
            _fail(f"post-commit verification FAILED — pg_constraint reports "
                  f"{pk!r}, expected ({NEW_PK_NAME!r}, {NEW_PK_COLUMNS!r}). "
                  "RESTORE FROM SNAPSHOT and investigate.")
        verified_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        print(f"verified: PK is now {pk[0]} ({', '.join(pk[1])}); "
              f"{verified_count} rows present.")
    print("migration complete.")


if __name__ == "__main__":
    main()
