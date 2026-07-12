# Runbook: r6_resources identity migration — (tenant_id, resource_type, id)

**Change:** swap the `r6_resources` primary key from the raw FHIR `id`
(global) to the composite `(tenant_id, resource_type, id)`.

**Why:** FHIR ids are only unique per resource type per source server. With
the global PK, tenant B importing an id tenant A already held (Synthea
`example`, Epic numeric ids) collided on the PK and the resource was
**silently dropped** from the import; `Patient/X` and `Observation/X`
collided within a single tenant too. Fixed in code by
`r6/models.py` + `r6/fasten/ingester.py`; this runbook applies the matching
schema change to the live Railway Postgres.

**Script:** `scripts/migrate_resource_identity.py` — idempotent,
transactional, Postgres-only, refuses to run unless pre-checks pass.

**Downtime:** none expected. The swap is two `ALTER TABLE` statements in one
transaction; the `ADD PRIMARY KEY` builds a unique index, which takes an
exclusive lock on `r6_resources` for the duration of the build (seconds at
current row counts). Ingest jobs running at that moment will briefly queue.

---

## Deploy ordering (code vs. schema)

Either order is safe — neither step makes anything worse than today:

* **New code + old (un-migrated) DB:** boots fine. `db.create_all()` skips
  existing tables, and `r6/schema_sync.py` only ADDs missing columns and
  WIDENs varchars — it never inspects or alters constraints, so the PK
  mismatch between the ORM model and the live table is invisible at boot.
  Cross-tenant collisions still fail at the DB level exactly as before
  (per-resource, rolled back, counted as `failed`) until the migration runs.
* **Migrated DB + old code:** also fine — old code never inserts a
  duplicate `id` on purpose, and all reads are plain SELECTs.

Recommended sequence anyway: **snapshot → migrate → deploy code → verify**,
so the moment the new ingester ships, the schema already accepts what it
writes.

## 0. Rehearse against a prod COPY first (required before first prod run)

Never run a PK swap against prod without having watched it succeed on a
copy of prod data the same day.

```bash
# 1. Dump prod (Railway: get DATABASE_URL from the Postgres service → Connect tab)
pg_dump "$PROD_DATABASE_URL" -Fc -f /tmp/hcg-prod.dump

# 2. Restore into a scratch DB (local Docker is fine)
docker run -d --name hcg-rehearsal -e POSTGRES_PASSWORD=rehearse \
  -p 55432:5432 postgres:16
export SCRATCH="postgresql://postgres:rehearse@localhost:55432/postgres"
pg_restore -d "$SCRATCH" --no-owner --no-privileges /tmp/hcg-prod.dump

# 3. Dry-run, then run, against the copy
SQLALCHEMY_DATABASE_URI="$SCRATCH" python scripts/migrate_resource_identity.py --dry-run
SQLALCHEMY_DATABASE_URI="$SCRATCH" python scripts/migrate_resource_identity.py

# 4. Run the Postgres-sensitive test subset against the migrated copy
SQLALCHEMY_DATABASE_URI="$SCRATCH" python -m pytest \
  tests/test_resource_identity.py tests/test_ingest_resilience.py tests/actions/ -q

# 5. Clean up
docker rm -f hcg-rehearsal
```

The test subset matters: `test_ingest_resilience.py` exists precisely
because two earlier bugs only manifested on Postgres (SQLite doesn't
enforce varchar lengths), and `tests/actions/` exercises the widest write
paths.

## 1. Snapshot prod (the rollback)

Railway → the project's **Postgres service → Backups → Create Backup**
(console), or from a shell with the prod URL:

```bash
pg_dump "$PROD_DATABASE_URL" -Fc -f hcg-prod-$(date +%Y%m%dT%H%M).dump
```

Keep the dump until the post-migration smoke checks (step 4) pass and at
least one real ingest has completed cleanly.

## 2. Dry-run against prod

```bash
SQLALCHEMY_DATABASE_URI="$PROD_DATABASE_URL" \
  python scripts/migrate_resource_identity.py --dry-run
```

Expected output: row count, `current PK: r6_resources_pkey (id)`,
`pre-checks passed: 0 duplicate identity triples, 0 NULL tenant_id, 0 NULL
resource_type`, the two ALTER statements, and `--dry-run verdict: safe to
migrate`. **Any pre-check failure aborts — do not work around it; fix the
data first.** (NULL `tenant_id` rows would be unreachable by every
tenant-scoped query in the codebase anyway; investigate how they got there.)

## 3. Run

```bash
SQLALCHEMY_DATABASE_URI="$PROD_DATABASE_URL" \
  python scripts/migrate_resource_identity.py
```

The script re-runs the pre-checks, executes both ALTERs in **one
transaction**, then verifies the new constraint on a fresh connection.
Running it twice is safe — the second run prints `already migrated` and
exits 0.

## 4. Verify

The script already checks `pg_constraint`; belt-and-braces by hand:

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'r6_resources'::regclass AND contype = 'p';
-- expect: pk_r6_resources_identity | PRIMARY KEY (tenant_id, resource_type, id)
```

Smoke read (any live tenant):

```bash
curl -s -H "X-Tenant-Id: desktop-demo" \
  https://<prod-host>/r6/fhir/Patient | python -m json.tool | head
```

Then watch one Fasten ingest complete with `failed=0` where the old
cross-tenant collisions used to show up as nonzero `failed` counts.

## 5. Rollback = restore the snapshot

**In-place rollback is deliberately not offered.** The constraint swap
itself is symmetric (drop composite PK, re-add PK on `id`) — but any data
written **under the composite identity after the migration** may legally
contain the same `id` in two tenants or two types, which violates the old
global PK. Re-adding `PRIMARY KEY (id)` would then either fail or force us
to choose which patient's record to delete. A snapshot restore is the only
honest rollback:

Railway → Postgres service → **Backups → Restore**, or:

```bash
pg_restore -d "$PROD_DATABASE_URL" --clean --no-owner hcg-prod-<stamp>.dump
```

(Accepting the loss of anything written between snapshot and restore — which
is why the snapshot is taken immediately before the migration and the
verify happens immediately after.)

---

## W2 note

The `source` column (which upstream server a row came from) and ingest
Provenance are **explicitly out of scope** for this migration — see the W2
item in `docs/superpowers/specs/2026-07-11-real-actions-reliability-design.md`
and the comment on `R6Resource` in `r6/models.py`.
