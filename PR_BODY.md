candidate: adopt a pre-v1.8.0 database without stamping a schema it does not have

Defect lane (council ruling D1, amendment 1). Live reproduction on the maintainer's machine, no SOW, no PRD, no evidence pack.

## What

`upgrade_database()` stamped any unstamped database that had `r6_resources` at `0001_v1_8_0`, which asserts it has every table 0001 creates. A database built by an older `db.create_all()` has fewer tables, so 0002's first `inspect(...).get_columns("proposed_actions")` raised `NoSuchTableError` and boot died.

The fix (Option A) creates only the absent **baseline** tables before the stamp:

- `r6/database_migrations.py`
  - `_baseline_tables()` reads the table set from migration 0001 itself, by running it against a scratch in-memory SQLite database. The set cannot drift from the DDL.
  - `_create_missing_baseline_tables(connection)` creates `baseline - present` from current model metadata with `checkfirst=True`. Tables introduced after the baseline stay absent so 0004–0007 can create them.
  - `upgrade_database()` calls it on the adoption branch before `command.stamp`. The docstring now says what the code does.
  - `register_model_metadata()` moved here from `main.py`, because `upgrade_database` must see every model to create a table. `main.py` imports it from here (same name, so `main.register_model_metadata()` still works). `migrations/env.py` calls it instead of carrying a second copy of the nine imports.
- `tests/test_database_migrations.py`: new `test_pre_v1_8_database_missing_baseline_tables_is_adopted` (the 11-table fixture from the brief). One stale comment corrected: a DB missing whole baseline tables is now covered, not "fails loud by design".
- `docs/runbooks/database-migrations.md`: one paragraph saying `init-db` performs this adoption itself.

Why Option A and not B: B (make 0002 skip absent tables) leaves `proposed_actions` absent at head unless something else creates it, and nothing in 0003–0007 does. Creating every current metadata table instead of the 0001 set was rejected and mutation-checked (below): it fails in 0004 with `table cc_conversations already exists`, because the legacy `cc_conversation_messages` still lacks the columns 0004 keys on.

Net diff: 5 files, +123 / −33 (+~35 of that is the new test).

## Why

Property protected: an operator's existing database migrates instead of crashing at boot.

The three documented states keep working and are each pinned by a test:

| State | Test |
| --- | --- |
| fresh DB | `test_fresh_install_builds_current_schema_without_flask_app` |
| legacy v1.8 `create_all` DB (full and pre-W0 shapes) | `test_legacy_create_all_database_is_adopted_not_recreated`, `test_pre_w0_sqlite_database_with_unnamed_pk_upgrades` |
| pre-v1.8 `create_all` DB (11 tables) | `test_pre_v1_8_database_missing_baseline_tables_is_adopted` (new) |
| already Alembic-managed DB | second `upgrade_database()` call in the legacy tests, `test_initialize_database_runs_alembic_on_the_app_engine` |

`create_app` stays side-effect free: no DDL was added to the factory. `tests/test_app_factory.py` passes unchanged.

## Repro

Dev SQLite database with 11 tables (`r6_resources`, `audit_events` without `outcome_detail_code`, `context_envelopes`, `context_items`, `fasten_connections`, `fasten_jobs`, `cc_agent_tasks`, `cc_conversation_messages`, `telegram_bindings`, `wearable_connections`, empty `alembic_version`). `upgrade_database(engine)` before the fix, reproduced by the new test on this branch:

```text
INFO  [alembic.runtime.migration] Running stamp_revision  -> 0001_v1_8_0
INFO  [alembic.runtime.migration] Running upgrade 0001_v1_8_0 -> 0002_current_contract, Replace boot-time schema reconciliation with deterministic DDL.
  migrations/versions/0002_current_schema_contract.py:145: in upgrade
    _add_missing("proposed_actions", ...)
  migrations/versions/0002_current_schema_contract.py:31: in _add_missing
    existing = _columns(table)
  migrations/versions/0002_current_schema_contract.py:27: in _columns
    return {column["name"]: column for column in inspect(op.get_bind()).get_columns(table)}
  .venv/lib/python3.12/site-packages/sqlalchemy/dialects/sqlite/base.py:2435: NoSuchTableError
E   sqlalchemy.exc.NoSuchTableError: proposed_actions
FAILED tests/test_database_migrations.py::test_pre_v1_8_database_missing_baseline_tables_is_adopted
1 failed in 1.48s
```

## Ran

All from this worktree, `uv run`, Python 3.12.14 (CI is 3.11; no 3.12-only syntax used). Observed output:

```text
$ uv run python -m pytest tests/test_database_migrations.py::test_pre_v1_8_database_missing_baseline_tables_is_adopted -q
1 passed in 0.28s

$ uv run python -m pytest tests/test_database_migrations.py tests/test_app_factory.py -q
16 passed, 1 skipped in 1.48s        (skip = Postgres lane, MIGRATION_TEST_DATABASE_URL unset)

$ uv run ruff check .
All checks passed!

$ uv run python -m pytest tests/ -q
3158 passed, 13 skipped, 1 xfailed, 2 warnings in 90.43s (0:01:30)

$ SQLALCHEMY_DATABASE_URI=sqlite:///<scratch>/cli-check.db uv run alembic upgrade head && uv run alembic current && uv run alembic check
0007_agent_worker_presence (head)
No new upgrade operations detected.
```

The `alembic` CLI run is there because `migrations/env.py` now imports `r6.database_migrations`; the CLI import context differs from the in-process one and was exercised, not assumed.

Idempotency on the adopted 11-table database (scratch script, observed): `tables before: 11`, first call `0007_agent_worker_presence`, second call `0007_agent_worker_presence`, `tables after: 20`.

## Mutation evidence

Two mutants, each: mutate → run the new test → restore → confirm `grep -c MUTANT r6/database_migrations.py` prints `0` and the test is green again.

Mutant 1 — fix removed (`created = _create_missing_baseline_tables(connection)` → `created = []`):

```text
E           sqlalchemy.exc.NoSuchTableError: proposed_actions
1 failed in 0.40s
```

Mutant 2 — the rejected variant, create every current metadata table instead of the 0001 set (`_baseline_tables() - present` → `set(db.metadata.tables) - present`):

```text
INFO  [alembic.runtime.migration] Running upgrade 0003_audit_outcome_detail -> 0004_conversation_identity, ...
E       sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) table cc_conversations already exists
1 failed in 0.37s
```

Restored: `1 passed`, then `16 passed, 1 skipped` on the two named files.

## What was NOT run

- Not run against Postgres. `test_postgres_fresh_install_and_v1_8_upgrade_path` and the Postgres variant of `test_legacy_create_all_upgrade_on_configured_database` skipped locally (no `MIGRATION_TEST_DATABASE_URL` / Postgres `SQLALCHEMY_DATABASE_URI`). The CI Postgres lane runs `tests/test_database_migrations.py`, so the new test will run there on the PR. The one Postgres-specific consideration: `create_all` on the supplied connection autobegins a transaction before `command.stamp`; the existing code already ran stamp and upgrade on that same connection in one transaction, and Alembic reuses a transaction that is already open, so the pattern is unchanged. Not observed.
- Not run against the maintainer's actual dev database file. The fixture reproduces its table set from the brief.
- Not run: Node tests, Playwright, `./scripts/demo_e2e.sh` (no Node or Flask surface touched).
- Not pushed, no PR opened.

## Generalization check

- Any unstamped database with `r6_resources` and any subset of the 0001 tables now adopts. A subset that also lacks `r6_resources` is not a HealthClaw database and still runs every migration from scratch (unchanged).
- The `_CREATE_ALL_SCHEMA_REVISION` branch (full present-day schema, no stamp) also passes through `_create_missing_baseline_tables`; by definition nothing is missing there, so it is a no-op and the branch is unchanged.
- A baseline table whose model is removed in a future release would raise `KeyError: '<table>'` from `db.metadata.tables[name]` on adoption of a database that lacks it. That is loud and names the table; it is not silently skipped.
- `_baseline_tables()` costs one in-memory SQLite run of 0001 and only on the adoption branch (once per database lifetime).
- Column drift within a table (a legacy `audit_events` without `outcome_detail_code`) was already handled by 0002/0003 and is unchanged; the new test pins that it still is.
