# Runbook: Alembic database migrations

HealthClaw uses immutable Alembic revisions for database DDL. Application
imports, WSGI workers, and Celery workers never create or reconcile tables.
Apply migrations once as an operator-controlled release step before starting
new application code.

## New database

Set the production database URL, preview the revision chain, then upgrade:

```bash
export SQLALCHEMY_DATABASE_URI=postgresql://...
uv run alembic history
uv run flask --app main init-db
uv run alembic current
uv run alembic check
```

Expected current revision: `0002_current_contract (head)`. `alembic check`
must print `No new upgrade operations detected.`

## Existing v1.8.0 database (first Alembic deployment)

Revision `0001_v1_8_0` is a compatibility marker for the supported pre-Alembic
schema. It must be *stamped*, not executed, on an existing database. Stamping
does not run DDL, so verify the database and rehearse on a same-day copy first.

1. Stop ingestion and action workers; leave read-only traffic draining.
2. Create and retain a Postgres snapshot.
3. Restore the snapshot into a scratch database.
4. Confirm all expected v1.8 tables exist and run the legacy identity script
   in dry-run mode. Resolve any null tenant/resource identities or duplicate
   `(tenant_id, resource_type, id)` values before proceeding.
5. On the scratch database, stamp and upgrade:

   ```bash
   export SQLALCHEMY_DATABASE_URI="$SCRATCH_DATABASE_URL"
   uv run alembic stamp 0001_v1_8_0
   uv run alembic upgrade head
   uv run alembic current
   uv run alembic check
   ```

6. Run the Postgres-sensitive suite against the scratch copy. Verify that
   `r6_resources` has primary key `(tenant_id, resource_type, id)`, externally
   issued Fasten/resource IDs are `varchar(255)`, and existing row counts and
   checksums are unchanged.
7. Repeat the stamp/upgrade against production, then deploy web and worker
   processes.

The checked-in deployment configs enforce this ordering: Compose runs the
one-shot `migrate` service before its web service, while Railway runs `init-db`
as its first pre-deploy command. Railway then runs a separate idempotent
`seed-demo` command because that project is the explicitly public synthetic
desktop demo; production/private deployments must omit that seed command.

Revision `0002_current_contract` replaces the former `schema_sync` behavior. It
adds the curation, Fasten recovery/enrollment-proof, and action attempt-ledger
columns; widens known externally issued IDs and action states; and installs the
tenant-scoped resource primary key. It is conditional so a v1.8 database that
was already fully reconciled can be stamped and upgraded without
duplicate-column errors.

## Failure and rollback

Do not edit a revision that has run in any shared environment. Add a new
revision instead.

If an upgrade fails, keep workers stopped and inspect the database before
retrying. PostgreSQL rolls transactional DDL back, but lock timeouts and manual
operator changes still need investigation. For the resource identity change,
rollback means restoring the snapshot: after new writes, multiple tenants may
legitimately share the same FHIR `id`, so restoring the old global primary key
can be impossible without deleting data.

Never use `alembic stamp` to hide a failed migration. A stamp only changes the
version marker; it does not make the schema match the models.
