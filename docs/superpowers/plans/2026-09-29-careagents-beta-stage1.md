# CareAgents beta, stage 1 (invited real-record testers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator invite up to 25 people to connect their own
records, show them tester terms once, count the weekly funnel, and pause one
account, without an environment-variable redeploy for each change.

**Architecture:** Two new CareAgents tables (`ca_real_record_invites`,
`ca_activity_days`) and one new account column (`real_paused_at`), all
PHI-free. One gate closure in `create_app` replaces every call to
`cfg.real_records_open_for`. The run worker is the single place that refuses
a turn (paused, or consent older than the current terms), because it is the
only caller of `llm.complete` and it serves web, iMessage and Telegram alike.
Operator actions are Flask CLI commands, the same pattern as `page-views`,
so no admin HTTP surface is added.

**Tech Stack:** Python 3.11, Flask, SQLAlchemy (SQLite locally, Postgres in
production and in CI's Postgres lane), click, pytest, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-09-26-careagents-beta-pathway-design.md`
(PR #838), as narrowed by the product review (below).

## Scope, as narrowed by the product review

| Spec section | Stage 1 decision |
|---|---|
| §4.1 waitlist | **Cut to fast-follow.** Intake is issue #718 and contactus@healthclaw.io. |
| §4.2 invites in the database | **Build** (Tasks 1, 2, 4, 8). |
| §4.3 tester terms, consent-version bump | **Build**, with a placeholder terms file and a gate, because the text waits on owner approval in #565 (Tasks 3, 6). |
| §4.4 in-app feedback form | **Cut to fast-follow.** Same intake as §4.1. |
| §4.5 weekly counts | **Build** (Task 7). |
| §4.6 per-account pause | **Build** (Task 5). |
| Issue #847, Fasten duplicate-pending race | **Build** (Task 9), reusing the sample path's lease. |

**BYOK (#834) is not a stage 1 dependency.** The spec header lists it, but
nothing in stage 1 needs it. The model-host policy that matters for real
records is already enforced at boot: `careagents/config.py` refuses
`CARE_REAL_RECORDS` other than `off` unless every chat host is on the vetted
list (#833, #839). With at most 25 testers and the per-account daily turn cap
(`claim_daily_turn`, `careagents/accounts.py`), the operator's paid key covers
real-record chat, which is also what gate 2 asks for (paid keys, not free
tiers). BYOK is a cost and choice feature for stage 2.

## Preconditions (do these before Task 1)

1. **PR #843 must be merged first.** Task 9 changes
   `AccountService.pending_connection` and the Fasten reuse block in
   `_start_connection`, both added by #843, and Task 6 edits the #843 version
   of `careagents/templates/home.html` and `careagents/static/home.js`. On
   2026-09-29 #843 was open, `mergeStateStatus: CLEAN`, and
   `gh pr view 843 --json autoMergeRequest` returned `null`. If it is still
   open, stop and ask; do not stack on it.
2. Branch from fresh `origin/main` in a worktree named per
   `docs/agent-task-guide.md` §8, for example `feat/careagents-beta-stage1`.
3. Claim the paths:
   `uv run python scripts/lane_check.py careagents/models.py careagents/accounts.py careagents/app.py careagents/worker.py careagents/beta.py careagents/tester_terms.py careagents/operator_cli.py careagents/templates/home.html careagents/templates/_tester_terms.html careagents/static/home.js docs/runbooks/careagents-durable-worker.md tests/`
   PR #846 touches consent wording on the HealthClaw side. If `lane_check`
   names it for any path above, wait for it to land.
4. `uv sync`, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/test_careagents*.py`
   to get a green baseline, and record the counts.

## Global Constraints

- Stage 1 size: **at most 25** active invites. `invites add` refuses the 26th.
- Stage 1 runs with `CARE_REAL_RECORDS=allowlist`. `off` closes real records whatever the table says. `on` opens them to everyone and ignores invites.
- CareAgents stores **no PHI**: accounts and pointers only. New tables hold an email, timestamps, an operator handle and integers. Nothing else.
- Schema changes go through `_create_tables` (new tables) and `_ensure_columns` / `_add_column` (new columns) in `careagents/models.py`. No other migration mechanism.
- Invites are keyed by the normalised email: `email.strip().lower()`, exactly as `start_email_code` and `verify_email_code` normalise it (`careagents/accounts.py:98`, `:153`).
- Operator commands print to the operator's terminal. They never write an email address to the application log. Log lines use the account id.
- The weekly counts are integers only, with no per-person row.
- Python 3.11 syntax only. Lint with `uv run ruff check .`, never `uvx` or `pipx`.
- Synthetic data only in tests and examples: `@example.com` / `@example.org` addresses, no real names, no real tenant ids.
- Patient-facing copy: plain, sentence case, no em dashes, no jargon (see the copy tests added by #843).
- Conformance stays Grade A (`tests/test_guardrail_conformance.py`). Nothing here touches the engine, but the suite runs anyway.

## CTO design pass: risks to PHI, auth and tenancy

Each risk names the decision that answers it and the task that pins it.

| # | Risk | Decision | Task |
|---|---|---|---|
| R1 | **A back door around `off`.** Adding a table check next to the env allowlist makes it easy to write `env or table` above the mode check. | One closure, `_real_open(acct)`, with the mode checked first: `off` returns False before the table is read. A test per mode; a mutation note says that deleting the `off` branch must fail a test. | 4 |
| R2 | **Someone claims an invited email.** Access is granted by email. | Accounts exist only after a one-time email code is verified (`careagents/accounts.py:152-184`), so holding an account with an invited email proves control of that inbox. The invite grants only the right to connect *one's own* records into a new tenant. It never grants a read of anyone else's tenant. | 4 (test notes it) |
| R3 | **A pause that does not stop the model.** An admission check in `/api/chat` misses iMessage, Telegram, and runs queued before the pause. | The gate lives in `RunWorker._execute`, before `recent_messages` and before `llm.complete` (`careagents/worker.py:220-298`). It finalises the run with a fixed, PHI-free sentence. No record is read and no provider is called. | 5 |
| R4 | **What a pause does not stop.** Fasten webhook ingest runs in the engine, and an MCP grant reads through HealthClaw directly. | Pause blocks new real connections, new MCP grants on real connections (`consent_decide` uses the same gate), refresh, upload and every chat turn. It does **not** stop engine-side ingest or an existing MCP grant. For stage 1 the MCP connector is token-locked in production, so no grant exists. For a full stop the operator uses `CARE_REAL_RECORDS=off` plus Disconnect or Delete. The runbook row says so. Before the connector opens: pause must also revoke grants (fast-follow issue). | 5, 8 |
| R5 | **Consent claimed for terms nobody saw.** A terms change that does not bump the version silently claims earlier consent. | `careagents/tester_terms.py` owns `CONSENT_VERSION`. A test ties it to the terms file: while the file carries the pending marker, the version stays `2026-08-01` and the card shows no terms. Once the marker is removed, the version must change. That holds today and fails the moment someone pastes real terms without bumping. | 3 |
| R6 | **Invites honoured before the terms exist.** Stage 1's legal gate (spec §3, gate 1) is the tester terms. | Table invites count only when `tester_terms.approved()`. The env allowlist keeps working as it does today for the people already on it. Invites can be added early; the CLI says they are not honoured yet. | 3, 4 |
| R7 | **A consent bump that only reaches new connections.** Consent is checked at connect (`careagents/app.py:654`) and at refresh, but Fasten refresh answers `requires_consent: False`, so an existing real connection is never asked again. | Any non-sample connection whose `consent_version` differs from the current one gets the terms sentence from the worker instead of an answer, until the person accepts again from the hub (`POST /api/connections/<id>/consent`). The gate is `kind != "sample"`, not a list of kinds, so an older or unknown kind fails closed. **Rollout note:** real connections made before the consent column existed hold `NULL` and will be asked once on deploy. That is correct, and the owner should expect it. The worker sentence and the hub line are worded to be true before #565 is approved ("please review and accept the current terms"), not "we updated the terms". | 6 |
| R8 | **Tenancy on the new endpoint.** A re-consent endpoint that takes a connection id is a cross-account write if it is not scoped. | `svc.record_consent(account_id, conn_id, version)` filters by both ids. A foreign id is a 404, the same as every connection route. | 6 |
| R9 | **Duplicate pending Fasten rows (#847).** Two tabs pass `pending_connection` before either inserts. | Reuse the per-account lease `claim_sample_start` / `release_sample_start` (a compare-and-set on `ca_accounts.sample_claim_at`) around the Fasten reuse-or-insert. No partial unique index: `_create_tables` never adds an index to an existing table, and live duplicate rows would make a `CREATE UNIQUE INDEX` fail at boot. The method keeps its name: renaming it touches evidence files that record past mutation runs. The docstring is updated. | 9 |
| R10 | **Invite emails outlive the person.** `delete_account` promises every row keyed to the person goes. | `delete_account` also deletes the invite row for the account's email and its `ca_activity_days` rows. The operator can invite again. | 1 |
| R11 | **Counts that re-identify.** At 25 testers a per-person row is a person. | `weekly-counts` prints one row per ISO week with four integers, and nothing else. It is a CLI command, so no HTTP surface exists. A test asserts the output has no `@` and no account id. | 7 |
| R12 | **Operator data in a public repo.** | Examples use `@example.com`. `invited_by` is a short operator handle (default `operator`), not a name. | 2, 8 |

No new FHIR access path is added, so no new AuditEvent is needed. The worker
gate returns before the first engine read. The re-consent endpoint changes a
CareAgents row only. `validate_step_up_token`, redaction, `terminology.py`
labels, the allergy-attestation rule and the action rail are untouched.
Nothing is built on `X-Human-Confirmed`.

## Review Focus

1. **An invited email in a different case or with spaces** (`" Tester@Example.COM "`): the invite must match the account, and a second `invites add` must not make a second row. Pinned in Task 2.
2. **A pause that lands while a run is already queued**: the run must answer the paused sentence and never call the provider. Pinned in Task 5 (the run is enqueued before the pause).
3. **A revoked invite and an existing pending Fasten row**: revocation must stop the reuse path from handing out a connect URL, not only the insert path. Pinned in Task 9.
4. **A tester chatting through iMessage after the terms bump**: the terms sentence must reach them there too, not only on the web. Pinned in Task 6 (worker-level test).
5. **`CARE_REAL_RECORDS=on` with a paused account**: `on` must not reopen a paused account. Pinned in Task 4.

## File structure

| File | Responsibility |
|---|---|
| `careagents/models.py` (modify) | `RealRecordInvite`, `ActivityDay` tables; `Account.real_paused_at` column and its `_ensure_columns` entry. |
| `careagents/accounts.py` (modify) | Invite CRUD, pause flag, activity counters, re-consent write, worker context gains `connection` and `paused`, `delete_account` cleanup, `_conn_dict` gains `consent_version`. |
| `careagents/tester_terms.py` (create) | Whether the tester terms are approved, and the consent version derived from that. Nothing else. |
| `careagents/templates/_tester_terms.html` (create) | The terms text. Placeholder with a pending marker until #565 is approved. |
| `careagents/beta.py` (create) | Stage 1 rules that are not storage: the invite cap, the two refusal sentences, `turn_block`, `weekly_counts`. |
| `careagents/operator_cli.py` (create) | `invites`, `records` and `weekly-counts` CLI commands. |
| `careagents/app.py` (modify) | `_real_open` gate, re-consent endpoint, hub banner data, approval counting, Fasten lease, CLI registration. |
| `careagents/worker.py` (modify) | `turn_block` check before any read or model call; `asked` counting. |
| `careagents/templates/home.html`, `careagents/static/home.js` (modify) | Terms inside the consent card; the "updated terms" line on the hub. |
| `docs/runbooks/careagents-durable-worker.md` (modify) | Operator rows for the new commands, including what pause does not stop. |
| `tests/careagents_stage1_helpers.py` (create) | Shared helpers: an allowlist-mode config, approving the terms in a test. |
| `tests/test_careagents_beta_*.py` (create) | One file per task. |

---

### Task 1: Schema: invites, activity counters, the pause column

**Files:**
- Modify: `careagents/models.py` (after `UsageDay`, and `_ensure_columns`)
- Modify: `careagents/accounts.py` (`delete_account`, `_conn_dict`, imports)
- Test: `tests/test_careagents_beta_schema.py`

**Interfaces:**
- Produces: `models.RealRecordInvite(id, email, invited_at, invited_by, revoked_at)`, table `ca_real_record_invites`, unique on `email`.
- Produces: `models.ActivityDay(id, account_id, day, asked, approved)`, table `ca_activity_days`, unique on `(account_id, day)`.
- Produces: `Account.real_paused_at: float | None`.
- Produces: `_conn_dict(c)["consent_version"]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Stage 1's tables and column reach an existing database safely.

Same shape as test_careagents_calm_hub_schema.py: a legacy ca_accounts row
survives with the new column empty, the new tables are created, and a peer
that added the column first is not an error.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import models


@pytest.fixture
def url(tmp_path):
    env = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if env and not env.startswith("sqlite"):
        engine = create_engine(env)
        models.Base.metadata.drop_all(engine)
        engine.dispose()
        return env
    return f"sqlite:///{tmp_path / 'legacy.db'}"


def _legacy_accounts(url):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_accounts (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL UNIQUE, email_verified_at FLOAT, "
            "created_at FLOAT, last_login_at FLOAT)"))
        conn.execute(text(
            "INSERT INTO ca_accounts (id, email, created_at) "
            "VALUES ('acct_legacy', 'legacy@example.com', 1.0)"))
    return engine


def test_an_existing_account_gains_an_empty_pause_column(url):
    legacy = _legacy_accounts(url)
    engine = models.make_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("ca_accounts")}
    assert "real_paused_at" in cols
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT real_paused_at FROM ca_accounts "
            "WHERE id = 'acct_legacy'")).one()
    assert row[0] is None
    engine.dispose()
    legacy.dispose()


def test_the_new_tables_exist_with_only_their_listed_columns(url):
    engine = models.make_engine(url)
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("ca_real_record_invites")} == {
        "id", "email", "invited_at", "invited_by", "revoked_at"}
    assert {c["name"] for c in insp.get_columns("ca_activity_days")} == {
        "id", "account_id", "day", "asked", "approved"}
    engine.dispose()


def test_a_peer_that_added_the_pause_column_first_is_not_an_error(url):
    legacy = _legacy_accounts(url)
    with legacy.begin() as conn:
        conn.execute(text(
            "ALTER TABLE ca_accounts ADD COLUMN real_paused_at FLOAT"))
    engine = models.make_engine(url)
    models._add_column(engine, "ca_accounts", "real_paused_at", "FLOAT")
    engine.dispose()
    legacy.dispose()


def test_deleting_an_account_removes_its_invite_and_activity(cfg, svc,
                                                            monkeypatch):
    from careagents.models import ActivityDay, RealRecordInvite
    from tests.test_careagents import _make_account
    acct_id = _make_account(svc, monkeypatch, "leaver@example.com")
    with svc.session() as s:
        s.add(RealRecordInvite(email="leaver@example.com",
                               invited_by="operator"))
        s.add(ActivityDay(account_id=acct_id, day="2026-10-01", asked=1))
    assert svc.delete_account(acct_id) is True
    with svc.session() as s:
        assert s.query(RealRecordInvite).count() == 0
        assert s.query(ActivityDay).count() == 0
```

Add `from tests.test_careagents import cfg, svc  # noqa: F401` at the top so
the fixtures resolve. Check `_make_account`'s return value in
`tests/test_careagents.py:2510` before relying on it; if it returns the
account row rather than its id, use `.id`.

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_schema.py -v`
Expected: FAIL, `real_paused_at` missing and `ImportError` for the new models.

- [ ] **Step 3: Implement**

In `careagents/models.py`, on `Account` after `switch_prompted_at`:

```python
    # When an operator paused this account (beta spec section 4.6). While
    # set, no chat turn reaches a model and no new real connection starts.
    # A timestamp, not PHI.
    real_paused_at = Column(Float, nullable=True)
```

After `UsageDay`:

```python
class RealRecordInvite(Base):
    """An invitation to connect one's own records (beta spec section 4.2).

    Keyed by the normalised email, one row per email: inviting again clears
    `revoked_at` instead of adding a row. Account-level data, the same class
    as `ca_accounts.email`. No PHI. `invited_by` is a short operator handle.
    """
    __tablename__ = "ca_real_record_invites"
    id = Column(String(32), primary_key=True, default=lambda: _uid("inv"))
    email = Column(String(255), unique=True, nullable=False, index=True)
    invited_at = Column(Float, default=now)
    invited_by = Column(String(64), nullable=False, default="operator")
    revoked_at = Column(Float, nullable=True)


class ActivityDay(Base):
    """Per-account daily counts for the weekly number (beta spec 4.5).

    `asked` counts turns on a real-record assistant, `approved` counts
    approvals of a real-record action. Integers only: no message, no action
    kind, nothing about the record.
    """
    __tablename__ = "ca_activity_days"
    __table_args__ = (UniqueConstraint("account_id", "day",
                                       name="uq_ca_activity_days_acct_day"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("act"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    asked = Column(Integer, default=0)
    approved = Column(Integer, default=0)
```

In `_ensure_columns`, extend the existing `ca_accounts` tuple:

```python
        for name in ("sample_claim_at", "first_agent_at",
                     "switch_prompted_at", "real_paused_at"):
```

In `careagents/accounts.py`: import `ActivityDay` and `RealRecordInvite`
from `careagents.models`. In `delete_account`, add `ActivityDay` to the
tuple of per-account models, and after the `EmailToken` delete:

```python
            s.query(RealRecordInvite).filter_by(email=acct.email).delete()
```

In `_conn_dict`, add `"consent_version": c.consent_version,`.

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_schema.py tests/test_careagents_calm_hub_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add careagents/models.py careagents/accounts.py tests/test_careagents_beta_schema.py
git commit -m "CareAgents: invites, activity counts and pause column (beta stage 1)"
```

---

### Task 2: Invite service methods and the 25 cap

**Files:**
- Create: `careagents/beta.py`
- Modify: `careagents/accounts.py` (new methods after `release_sample_start`)
- Create: `tests/careagents_stage1_helpers.py`
- Test: `tests/test_careagents_beta_invites.py`

**Interfaces:**
- Consumes: `RealRecordInvite` (Task 1).
- Produces: `beta.STAGE1_INVITE_CAP = 25`.
- Produces: `AccountService.add_invite(email: str, invited_by: str = "operator") -> str`, which returns `"invited"`, `"re-invited"` or `"already invited"` and raises `ValueError` for a bad email or a full cohort.
- Produces: `AccountService.revoke_invite(email: str) -> bool`.
- Produces: `AccountService.list_invites() -> list[dict]` with keys `email, invited_at, invited_by, revoked_at`.
- Produces: `AccountService.is_invited(email: str) -> bool` (active rows only).
- Produces: `tests/careagents_stage1_helpers.allowlist_cfg(**env) -> Config`, `approve_terms(monkeypatch, version="2026-10-01") -> None`.

- [ ] **Step 1: Write the helpers and the failing tests**

`tests/careagents_stage1_helpers.py`:

```python
"""Helpers shared by the beta stage 1 tests. Not a test module."""

from __future__ import annotations

import os

from careagents.config import Config


def allowlist_cfg(**extra) -> Config:
    """The shared `cfg` fixture opens real records to everyone (`on`).
    Stage 1 runs in `allowlist`, so these tests build their own config."""
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        from careagents.models import Base, make_engine
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    env = {"CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
           "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
           "HEALTHCLAW_MINT_SECRET": "mint-secret",
           "FASTEN_PUBLIC_KEY": "pub123", "CARE_REAL_RECORDS": "allowlist"}
    env.update(extra)
    return Config(env=env)


def approve_terms(monkeypatch, version: str = "2026-10-01") -> None:
    """Act as if #565 were approved: terms version set, consent bumped."""
    from careagents import tester_terms
    monkeypatch.setattr(tester_terms, "TERMS_VERSION", version)
    monkeypatch.setattr(tester_terms, "CONSENT_VERSION", version)
```

`tests/test_careagents_beta_invites.py`:

```python
from __future__ import annotations

import pytest

from careagents import beta
from careagents.models import RealRecordInvite
from tests.test_careagents import cfg, svc  # noqa: F401


def test_an_invite_is_stored_normalised_and_only_once(svc):
    assert svc.add_invite(" Tester@Example.COM ") == "invited"
    assert svc.add_invite("tester@example.com") == "already invited"
    assert svc.is_invited("TESTER@example.com") is True
    with svc.session() as s:
        assert s.query(RealRecordInvite).count() == 1


def test_a_revoked_invite_stops_counting_and_can_be_renewed(svc):
    svc.add_invite("tester@example.com")
    assert svc.revoke_invite("tester@example.com") is True
    assert svc.is_invited("tester@example.com") is False
    assert svc.revoke_invite("nobody@example.com") is False
    assert svc.add_invite("tester@example.com") == "re-invited"
    assert svc.is_invited("tester@example.com") is True


def test_a_malformed_email_is_refused(svc):
    for bad in ("", "no-at-sign", "a@" + "x" * 300):
        with pytest.raises(ValueError):
            svc.add_invite(bad)


def test_the_cohort_stops_at_the_cap_counting_active_invites_only(svc):
    for i in range(beta.STAGE1_INVITE_CAP):
        svc.add_invite(f"t{i}@example.com")
    with pytest.raises(ValueError, match="25"):
        svc.add_invite("one-more@example.com")
    svc.revoke_invite("t0@example.com")
    assert svc.add_invite("one-more@example.com") == "invited"


def test_the_list_carries_only_the_listed_fields(svc):
    svc.add_invite("tester@example.com", invited_by="ops-1")
    [row] = svc.list_invites()
    assert set(row) == {"email", "invited_at", "invited_by", "revoked_at"}
    assert row["invited_by"] == "ops-1"
```

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_invites.py -v`
Expected: FAIL, `ModuleNotFoundError: careagents.beta` and missing methods.

- [ ] **Step 3: Implement**

`careagents/beta.py` (first part; Tasks 5, 6 and 7 add to it):

```python
"""Rules for the invited-tester stage (beta spec, stage 1).

Storage lives in accounts.py. This module holds the rules that are not
storage: the cohort cap, the two sentences a refused turn answers with, and
the weekly counts.
"""

from __future__ import annotations

#: Stage 1 is "up to 25" invited testers (spec section 2). Counted over
#: active invites. The environment allowlist is not counted: it is the
#: operator's own short list and predates the table.
STAGE1_INVITE_CAP = 25
```

In `careagents/accounts.py`, after `release_sample_start`:

```python
    # --- invites (beta spec section 4.2) ------------------------------------

    @staticmethod
    def _invite_email(email: str) -> str:
        email = (email or "").strip().lower()
        if "@" not in email or len(email) > 255:
            raise ValueError("Enter a valid email address.")
        return email

    def add_invite(self, email: str, invited_by: str = "operator") -> str:
        from careagents.beta import STAGE1_INVITE_CAP
        email = self._invite_email(email)
        with self.session() as s:
            row = s.query(RealRecordInvite).filter_by(email=email).first()
            if row is not None and row.revoked_at is None:
                return "already invited"
            active = (s.query(RealRecordInvite)
                      .filter(RealRecordInvite.revoked_at.is_(None)).count())
            if active >= STAGE1_INVITE_CAP:
                raise ValueError(
                    f"Stage 1 is full: {STAGE1_INVITE_CAP} active invites. "
                    "Revoke one first.")
            if row is not None:
                row.revoked_at = None
                row.invited_at = now()
                row.invited_by = (invited_by or "operator")[:64]
                return "re-invited"
            s.add(RealRecordInvite(email=email,
                                   invited_by=(invited_by or "operator")[:64]))
            return "invited"

    def revoke_invite(self, email: str) -> bool:
        email = (email or "").strip().lower()
        with self.session() as s:
            row = (s.query(RealRecordInvite)
                   .filter_by(email=email, revoked_at=None).first())
            if row is None:
                return False
            row.revoked_at = now()
            return True

    def list_invites(self) -> list[dict]:
        with self.session() as s:
            rows = (s.query(RealRecordInvite)
                    .order_by(RealRecordInvite.invited_at.asc()).all())
            return [{"email": r.email, "invited_at": r.invited_at,
                     "invited_by": r.invited_by, "revoked_at": r.revoked_at}
                    for r in rows]

    def is_invited(self, email: str) -> bool:
        email = (email or "").strip().lower()
        if not email:
            return False
        with self.session() as s:
            return (s.query(RealRecordInvite)
                    .filter_by(email=email, revoked_at=None)
                    .first() is not None)
```

A race between two operators adding the same email ends in the unique
constraint. That is a CLI error on the second terminal, which is acceptable
for a hand-run command. Do not add retry logic.

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_invites.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add careagents/beta.py careagents/accounts.py tests/careagents_stage1_helpers.py tests/test_careagents_beta_invites.py
git commit -m "CareAgents: invite service with a 25-tester cap (beta stage 1)"
```

---

### Task 3: Tester terms placeholder and the consent-version gate

**Files:**
- Create: `careagents/tester_terms.py`
- Create: `careagents/templates/_tester_terms.html`
- Modify: `careagents/app.py:43-48` (remove the local `CONSENT_VERSION`) and every use (`:658`, `:659`, `:1024` on `main`; re-find them after #843 lands)
- Modify: `careagents/templates/home.html` (consent modal) and the `home()` view
- Modify: `tests/test_careagents.py:4392` (import from the new module)
- Test: `tests/test_careagents_beta_terms.py`

**Interfaces:**
- Produces: `tester_terms.TERMS_VERSION: str | None` (None until #565 is approved), `tester_terms.CONSENT_VERSION: str`, `tester_terms.approved() -> bool`, `tester_terms.TEMPLATE = "_tester_terms.html"`, `tester_terms.PENDING_MARKER`.
- Code reads `tester_terms.CONSENT_VERSION` **at call time** (module attribute), never a copy taken at import, so a test can bump it.

- [ ] **Step 1: Write the failing tests**

```python
"""The tester terms and the consent version move together (spec 4.3, R5)."""

from __future__ import annotations

from pathlib import Path

from careagents import tester_terms
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import FakeClient, _login, cfg, svc  # noqa: F401

TERMS = Path(__file__).resolve().parents[1] / "careagents" / "templates" / \
    tester_terms.TEMPLATE


def test_the_terms_file_and_the_consent_version_agree():
    """Green while the placeholder stands. Goes red the day someone pastes
    the approved terms without setting TERMS_VERSION, or sets it without
    replacing the placeholder."""
    pending = tester_terms.PENDING_MARKER in TERMS.read_text()
    assert tester_terms.approved() is (not pending)
    if pending:
        assert tester_terms.CONSENT_VERSION == tester_terms.BASE_VERSION
    else:
        assert tester_terms.CONSENT_VERSION == tester_terms.TERMS_VERSION
        assert tester_terms.CONSENT_VERSION != tester_terms.BASE_VERSION
    assert len(tester_terms.CONSENT_VERSION) <= 16  # ca_connections column


def test_the_card_hides_pending_terms_and_shows_approved_ones(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert 'id="tester-terms"' not in c.get("/home").get_data(as_text=True)
    approve_terms(monkeypatch)
    assert 'id="tester-terms"' in c.get("/home").get_data(as_text=True)


def test_a_new_real_connection_records_the_current_version(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    from careagents.models import Connection
    approve_terms(monkeypatch, "2026-10-01")
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    r = c.post("/api/connections/fasten", json={})
    assert r.status_code == 428
    assert r.get_json()["consent_version"] == "2026-10-01"
    assert c.post("/api/connections/fasten",
                  json={"consent": True}).status_code == 200
    with svc.session() as s:
        assert s.query(Connection).filter_by(
            kind="fasten").one().consent_version == "2026-10-01"
```

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_terms.py -v`
Expected: FAIL, `ModuleNotFoundError: careagents.tester_terms`.

- [ ] **Step 3: Implement**

`careagents/tester_terms.py`:

```python
"""Tester terms for real-record testers (beta spec section 4.3).

The text waits on owner approval (#565). Until then the terms file carries
PENDING_MARKER, TERMS_VERSION is None, the consent card shows no terms, the
consent version stays at BASE_VERSION, and database invites are not honoured
(careagents/app.py, `_real_open`).

To approve: replace the whole of templates/_tester_terms.html with the
approved text (the marker goes with it), set TERMS_VERSION to the approval
date, and ship. tests/test_careagents_beta_terms.py fails if only one of the
two changes is made. Every tester then accepts the new wording once.
The approved file must keep `<section class="consent-terms"
id="tester-terms">` as its outer element: the card test looks for that id.
"""

from __future__ import annotations

#: The consent version before tester terms existed (#203).
BASE_VERSION = "2026-08-01"

#: Set to the approval date ("YYYY-MM-DD") when #565 is approved.
TERMS_VERSION: str | None = None

TEMPLATE = "_tester_terms.html"
PENDING_MARKER = "TESTER-TERMS-PENDING-565"

#: What a connection's consent_version must equal to be current. Read as
#: `tester_terms.CONSENT_VERSION` at call time, never copied at import.
CONSENT_VERSION: str = TERMS_VERSION or BASE_VERSION


def approved() -> bool:
    return TERMS_VERSION is not None
```

`careagents/templates/_tester_terms.html`:

```html
{# TESTER-TERMS-PENDING-565
   Placeholder. The tester terms wait on owner approval (#565). This file is
   not rendered while careagents/tester_terms.py has TERMS_VERSION = None.
   Replace the whole file with the approved text, including this comment,
   and keep the outer <section class="consent-terms" id="tester-terms">. #}
<section class="consent-terms" id="tester-terms">
  <h4>Tester terms</h4>
  <p>The tester terms are not approved yet.</p>
</section>
```

In `careagents/app.py`: delete the `CONSENT_VERSION = "2026-08-01"` block
(keep its comment by moving it into `tester_terms.py` above `BASE_VERSION`
if it is not already there), add `from careagents import tester_terms`, and
replace every `CONSENT_VERSION` with `tester_terms.CONSENT_VERSION`. Check
with `grep -n "CONSENT_VERSION" careagents/app.py`: every hit must be
`tester_terms.CONSENT_VERSION`.

In the `home()` view's `render_template` call, add
`tester_terms_approved=tester_terms.approved(),`.

In `careagents/templates/home.html`, inside `#consent-modal`, after the
closing `</ul>` of `.consent-points`:

```html
    {% if tester_terms_approved %}{% include "_tester_terms.html" %}{% endif %}
```

In `tests/test_careagents.py:4392`, change the import to
`from careagents.tester_terms import CONSENT_VERSION`.

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_terms.py tests/test_careagents.py -q`
Expected: PASS.

Mutation (record the result in the PR): set `TERMS_VERSION = "2026-10-01"`
without editing the template. `test_the_terms_file_and_the_consent_version_agree`
must fail. Revert. Purge `__pycache__` and run with
`PYTHONDONTWRITEBYTECODE=1` (stale-bytecode trap).

- [ ] **Step 5: Commit**

```bash
git add careagents/tester_terms.py careagents/templates/_tester_terms.html careagents/app.py careagents/templates/home.html tests/test_careagents.py tests/test_careagents_beta_terms.py
git commit -m "CareAgents: tester terms placeholder tied to the consent version (#565)"
```

---

### Task 4: One gate for real records: mode, env allowlist, invites, pause

**Files:**
- Modify: `careagents/accounts.py` (`is_paused`, `set_paused`)
- Modify: `careagents/app.py` (new closure; replace each `cfg.real_records_open_for(acct.email)`: `home`, `_offered_connections`, `consent_decide`, `connections_catalog`, `_start_connection`, plus any #843 added)
- Test: `tests/test_careagents_beta_gate.py`

**Interfaces:**
- Consumes: `svc.is_invited` (Task 2), `tester_terms.approved()` (Task 3).
- Produces: `AccountService.is_paused(account_id: str) -> bool`, `AccountService.set_paused(email: str, paused: bool) -> bool` (False when no account has that email).
- Produces: `_real_open(acct) -> bool`, a closure inside `create_app`. `cfg.real_records_open_for` stays as the env-only half and is called only from `_real_open`.

- [ ] **Step 1: Write the failing tests**

```python
"""_real_open: off beats everything, pause beats everything else, and a
table invite counts only in allowlist mode with approved terms (R1, R6)."""

from __future__ import annotations

import pytest

from careagents.app import create_app
from tests.careagents_stage1_helpers import allowlist_cfg, approve_terms
from tests.test_careagents import FakeClient, _login

EMAIL = "tester@example.com"


def _client(cfg, monkeypatch):
    from careagents.accounts import AccountService
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    return c, svc


def _can_start(c) -> bool:
    return c.post("/api/connections/fasten",
                  json={"consent": True}).status_code == 200


@pytest.mark.parametrize("mode", ["off", "allowlist", "on"])
def test_off_is_closed_whatever_the_table_and_env_say(monkeypatch, mode):
    cfg = allowlist_cfg(CARE_REAL_RECORDS=mode,
                        CARE_REAL_RECORDS_ALLOWLIST="")
    c, svc = _client(cfg, monkeypatch)
    approve_terms(monkeypatch)
    svc.add_invite(EMAIL)
    assert _can_start(c) is (mode != "off")


def test_an_invite_opens_allowlist_mode_only_once_terms_are_approved(
        monkeypatch):
    c, svc = _client(allowlist_cfg(), monkeypatch)
    svc.add_invite(EMAIL)
    assert _can_start(c) is False          # terms pending (R6)
    approve_terms(monkeypatch)
    assert _can_start(c) is True


def test_the_env_allowlist_still_works_without_approved_terms(monkeypatch):
    c, _ = _client(allowlist_cfg(CARE_REAL_RECORDS_ALLOWLIST=EMAIL),
                   monkeypatch)
    assert _can_start(c) is True


def test_an_uninvited_account_is_refused(monkeypatch):
    c, svc = _client(allowlist_cfg(), monkeypatch)
    approve_terms(monkeypatch)
    svc.add_invite("someone.else@example.com")
    assert _can_start(c) is False


def test_a_revoked_invite_closes_new_connections(monkeypatch):
    c, svc = _client(allowlist_cfg(), monkeypatch)
    approve_terms(monkeypatch)
    svc.add_invite(EMAIL)
    svc.revoke_invite(EMAIL)
    assert _can_start(c) is False


@pytest.mark.parametrize("mode", ["allowlist", "on"])
def test_a_paused_account_is_closed_even_when_on(monkeypatch, mode):
    c, svc = _client(allowlist_cfg(CARE_REAL_RECORDS=mode,
                                   CARE_REAL_RECORDS_ALLOWLIST=EMAIL),
                     monkeypatch)
    assert svc.set_paused(EMAIL, True) is True
    assert _can_start(c) is False
    assert c.get("/api/connections/catalog").get_json()["connectors"]
    svc.set_paused(EMAIL, False)
    assert _can_start(c) is True


def test_pausing_an_unknown_email_reports_false(monkeypatch):
    _, svc = _client(allowlist_cfg(), monkeypatch)
    assert svc.set_paused("nobody@example.com", True) is False
```

Note for the reviewer (R2): the invite is matched against `acct.email`, and
an account row exists only after the email code for that address was
verified (`careagents/accounts.py:152-184`).

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_gate.py -v`
Expected: FAIL (`set_paused` missing, invites ignored).

- [ ] **Step 3: Implement**

In `careagents/accounts.py`:

```python
    # --- pause (beta spec section 4.6) --------------------------------------

    def set_paused(self, email: str, paused: bool) -> bool:
        email = (email or "").strip().lower()
        with self.session() as s:
            acct = s.query(Account).filter_by(email=email).first()
            if acct is None:
                return False
            acct.real_paused_at = now() if paused else None
            logger.info("account %s %s by operator", acct.id,
                        "paused" if paused else "resumed")
            return True

    def is_paused(self, account_id: str) -> bool:
        with self.session() as s:
            acct = s.get(Account, account_id)
            return bool(acct and acct.real_paused_at is not None)
```

In `careagents/app.py`, inside `create_app` near `current_account`:

```python
    def _real_open(acct) -> bool:
        """May this account START a real-record connection? The one gate.

        Order matters. `off` closes everything, before the table is read
        (never a back door around `off`). A paused account is closed in
        every mode. `on` is open. `allowlist` is the environment list, or an
        active invite once the tester terms are approved (#565).
        """
        if cfg.real_records == "off":
            return False
        if svc.is_paused(acct.id):
            return False
        if cfg.real_records_open_for(acct.email):
            return True
        return (cfg.real_records == "allowlist"
                and tester_terms.approved()
                and svc.is_invited(acct.email))
```

Replace each `cfg.real_records_open_for(acct.email)` in `app.py` with
`_real_open(acct)`. Check: `grep -n "real_records_open_for" careagents/app.py`
returns only the line inside `_real_open`.

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_gate.py tests/test_careagents.py tests/test_careagents_consent.py -q`
Expected: PASS.

Mutations (record each in the PR, and assert the edit applied, since a
pattern can occur more than once in `app.py`):
1. Delete the `if cfg.real_records == "off": return False` lines.
   `test_off_is_closed_whatever_the_table_and_env_say[off]` must fail.
2. Delete the `svc.is_paused` check. Both `test_a_paused_account_is_closed_even_when_on` cases must fail.
3. Drop `tester_terms.approved() and`. `test_an_invite_opens_allowlist_mode_only_once_terms_are_approved` must fail.

- [ ] **Step 5: Commit**

```bash
git add careagents/accounts.py careagents/app.py tests/test_careagents_beta_gate.py
git commit -m "CareAgents: one real-records gate reading invites and pause"
```

---

### Task 5: Pause stops every turn in the worker, before any read or model call

**Files:**
- Modify: `careagents/beta.py` (`PAUSED_TEXT`, `turn_block`)
- Modify: `careagents/accounts.py:628-644` (`get_worker_agent_context`)
- Modify: `careagents/worker.py:220-225` (`_execute`)
- Modify: `careagents/app.py` (`refresh_connection`, `upload_connection`)
- Test: `tests/test_careagents_beta_pause.py`

**Interfaces:**
- Consumes: `svc.set_paused`, `svc.is_paused` (Task 4); `_conn_dict[...]["consent_version"]` (Task 1).
- Produces: `beta.PAUSED_TEXT: str`, `beta.turn_block(connection: dict, paused: bool, consent_version: str) -> str | None`. Task 6 adds the consent branch, and this task's version already takes the argument.
- Produces: `get_worker_agent_context(agent_id)` returns `{"agent", "tenant", "account_id", "connection", "paused"}`.

Pause covers **every** turn on the account, sample included. That is one
state instead of two, and the sample is synthetic, so nothing is lost. The
spec's words are "pauses one account's real connections". Say this choice
in the PR description.

- [ ] **Step 1: Write the failing tests**

```python
"""A paused account's turns answer the paused sentence and call no provider
(spec section 6; R3). The run is queued BEFORE the pause, which is the case
an admission check would miss."""

from __future__ import annotations

import pytest

from careagents import beta
from tests.test_careagents import _chat_app, cfg, svc  # noqa: F401

EMAIL = "gene@example.com"   # the address _login uses


def test_a_run_queued_before_the_pause_answers_paused_without_a_model(
        cfg, svc, monkeypatch):
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "paused-1"}, buffered=False)
    next(iter(r.response))
    r.close()
    assert svc.set_paused(EMAIL, True) is True
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("paused turn hit a model"))
    monkeypatch.setattr(fake, "recent_messages",
                        lambda *a, **k: pytest.fail("paused turn read records"))

    RunWorker(cfg, fake, svc, "pause-worker").run_once()

    run_id = next(iter(fake.runs))
    texts = [e["payload"].get("text") for e in fake.events[run_id]
             if e["type"] == "agent.text"]
    assert beta.PAUSED_TEXT in texts


def test_resuming_lets_the_next_turn_through(cfg, svc, monkeypatch):
    from careagents.worker import RunWorker
    app, c, fake, agent_id, tenant, _ = _chat_app(
        cfg, svc, monkeypatch, reply="back again")
    svc.set_paused(EMAIL, True)
    svc.set_paused(EMAIL, False)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "resumed-1"}, buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "resume-worker").run_once()
    run_id = next(iter(fake.runs))
    texts = [e["payload"].get("text") for e in fake.events[run_id]
             if e["type"] == "agent.text"]
    assert "back again" in texts


def test_refresh_and_upload_refuse_while_paused(cfg, svc, monkeypatch):
    app, c, fake, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    svc.set_paused(EMAIL, True)
    assert c.post(f"/api/connections/{conn_id}/refresh").status_code == 423
    assert c.post(f"/api/connections/{conn_id}/upload", data=b"{}",
                  content_type="application/fhir+json").status_code == 423


def test_the_paused_sentence_is_plain_and_phi_free():
    assert "—" not in beta.PAUSED_TEXT
    assert "contactus@healthclaw.io" in beta.PAUSED_TEXT
```

Before relying on `fake.events[run_id]` payload shape and on how
`finalize_agent_run` records text, read `FakeClient.finalize_agent_run`
(`tests/test_careagents.py:2150`) and match the assertion to what it
appends. If it records the final text somewhere other than an `agent.text`
event, assert on that.

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_pause.py -v`
Expected: FAIL (`beta.PAUSED_TEXT` missing, the model stub fires).

- [ ] **Step 3: Implement**

Append to `careagents/beta.py`:

```python
#: What a paused account's assistant answers (spec section 4.6). Fixed
#: text: no record was read to produce it.
PAUSED_TEXT = ("Your records are paused, so I can't answer right now. If "
               "you didn't expect this, write to contactus@healthclaw.io.")


def turn_block(connection: dict, paused: bool,
               consent_version: str) -> str | None:
    """The sentence a turn answers instead of reaching a model, or None.

    Checked in the run worker, the only caller of llm.complete, so it holds
    for web, iMessage and Telegram, and for runs queued before the change.
    """
    if paused:
        return PAUSED_TEXT
    return None
```

In `get_worker_agent_context`, load the account and return two more keys:

```python
            acct = s.get(Account, a.account_id)
            return {"agent": _agent_dict(a), "tenant": conn.tenant_id,
                    "account_id": a.account_id,
                    "connection": _conn_dict(conn),
                    "paused": bool(acct and acct.real_paused_at is not None)}
```

In `careagents/worker.py`, import `from careagents import beta, tester_terms`,
and directly after the `context is None or context["tenant"] != tenant`
check in `_execute`:

```python
        blocked = beta.turn_block(context["connection"], context["paused"],
                                  tester_terms.CONSENT_VERSION)
        if blocked:
            # Before any record read and before any model call (beta spec
            # 4.3 and 4.6). The run still ends normally, with this sentence.
            self._finish(run, {"text": blocked, "checkpoint_id": "blocked"},
                         set(), heartbeat)
            return
```

In `careagents/app.py`, in `refresh_connection` and `upload_connection`,
right after the ownership lookup returns a connection:

```python
        if svc.is_paused(acct.id):
            return jsonify({"error": "records_paused",
                            "message": beta.PAUSED_TEXT}), 423
```

(import `beta` in the `from careagents import ...` line).

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_pause.py tests/test_careagents.py -q`
Expected: PASS. If an existing test compares `get_worker_agent_context`'s
return value with `==`, update it to the new keys.

Mutation: move the `turn_block` check below `recent_messages`. The first
test must fail on "paused turn read records". Revert.

- [ ] **Step 5: Commit**

```bash
git add careagents/beta.py careagents/accounts.py careagents/worker.py careagents/app.py tests/test_careagents_beta_pause.py
git commit -m "CareAgents: pausing an account stops its turns in the worker"
```

---

### Task 6: A consent bump makes each tester accept again

**Files:**
- Modify: `careagents/beta.py` (`TERMS_TEXT`, consent branch of `turn_block`)
- Modify: `careagents/accounts.py` (`record_consent`)
- Modify: `careagents/app.py` (new route; `home()` passes `stale_consent`)
- Modify: `careagents/templates/home.html`, `careagents/static/home.js`
- Test: `tests/test_careagents_beta_reconsent.py`

**Interfaces:**
- Consumes: `turn_block` (Task 5), `tester_terms.CONSENT_VERSION` (Task 3), `showConsentCard()` and `post()` in `home.js` (from #843).
- Produces: `beta.TERMS_TEXT: str`; `AccountService.record_consent(account_id: str, conn_id: str, version: str) -> bool`; route `POST /api/connections/<conn_id>/consent` with body `{"consent": true}`, answering `{"consent_version": ...}` 200, 428 without consent, 404 for a foreign or sample connection.

- [ ] **Step 1: Write the failing tests**

```python
"""After a terms bump, a real connection's assistant asks for the new terms
until the person accepts them, on every surface (spec 6; R7, R8)."""

from __future__ import annotations

import pytest

from careagents import beta, tester_terms
from careagents.models import Connection
from tests.careagents_stage1_helpers import approve_terms
from tests.test_careagents import FakeClient, _login, cfg, svc  # noqa: F401


def _real_agent(cfg, svc, monkeypatch, email="gene@example.com"):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    svc.set_connection_status(fake.tenants[-1], "active")
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id, conn["id"]


def test_turn_block_asks_for_terms_only_on_a_stale_real_connection():
    v = "2026-10-01"
    assert beta.turn_block({"kind": "sample", "consent_version": None},
                           False, v) is None
    assert beta.turn_block({"kind": "fasten", "consent_version": v},
                           False, v) is None
    for stale in (None, "2026-08-01"):
        assert beta.turn_block({"kind": "fasten", "consent_version": stale},
                               False, v) == beta.TERMS_TEXT
    # an unknown kind fails closed
    assert beta.turn_block({"kind": "legacy", "consent_version": None},
                           False, v) == beta.TERMS_TEXT
    # paused wins
    assert beta.turn_block({"kind": "fasten", "consent_version": None},
                           True, v) == beta.PAUSED_TEXT


def test_a_worker_turn_after_the_bump_answers_the_terms_sentence(
        cfg, svc, monkeypatch):
    """The worker path, so iMessage and Telegram get it too."""
    from careagents.worker import RunWorker
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    tenant = fake.tenants[-1]          # minted by the Fasten connect
    created, mid = fake.claim_inbound_message(
        tenant, "hi", agent_id, fake.conversation_id(agent_id),
        "imessage", "stale-1")
    fake.create_agent_run(tenant, mid)
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("stale consent hit a model"))
    RunWorker(cfg, fake, svc, "terms-worker").run_once()
    run_id = next(iter(fake.runs))
    assert beta.TERMS_TEXT in [e["payload"].get("text")
                               for e in fake.events[run_id]
                               if e["type"] == "agent.text"]


def test_accepting_again_restamps_the_connection(cfg, svc, monkeypatch):
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    approve_terms(monkeypatch, "2026-10-01")
    assert c.post(f"/api/connections/{conn_id}/consent",
                  json={}).status_code == 428
    r = c.post(f"/api/connections/{conn_id}/consent", json={"consent": True})
    assert r.status_code == 200
    assert r.get_json() == {"consent_version": "2026-10-01"}
    with svc.session() as s:
        assert s.get(Connection, conn_id).consent_version == "2026-10-01"


def test_another_accounts_connection_is_a_404(cfg, svc, monkeypatch):
    from careagents.app import create_app
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    other = create_app(config=cfg, client=fake, accounts=svc).test_client()
    _login(other, svc, monkeypatch, email="other@example.com")
    assert other.post(f"/api/connections/{conn_id}/consent",
                      json={"consent": True}).status_code == 404


def test_the_hub_lists_stale_connections_only_after_a_bump(
        cfg, svc, monkeypatch):
    c, fake, agent_id, conn_id = _real_agent(cfg, svc, monkeypatch)
    assert 'data-reconsent=' not in c.get("/home").get_data(as_text=True)
    approve_terms(monkeypatch, "2026-10-01")
    assert f'data-reconsent="{conn_id}"' in c.get("/home").get_data(
        as_text=True)
```

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_reconsent.py -v`
Expected: FAIL (`beta.TERMS_TEXT` missing, route 404/405).

- [ ] **Step 3: Implement**

In `careagents/beta.py`, add the text and replace `turn_block`'s body:

```python
#: What a real-record assistant answers while its connection's consent is
#: older than the current terms (spec section 4.3).
#: True before and after #565 is approved: a connection made before the
#: consent column existed holds NULL and is asked on the first deploy,
#: when no terms have changed yet.
TERMS_TEXT = ("Before we go on, please review and accept the current terms "
              "on your home page, then ask me again.")


def turn_block(connection: dict, paused: bool,
               consent_version: str) -> str | None:
    """The sentence a turn answers instead of reaching a model, or None.

    Checked in the run worker, the only caller of llm.complete, so it holds
    for web, iMessage and Telegram, and for runs queued before the change.
    Anything that is not the sample needs consent at the current version,
    so an older or unknown kind fails closed.
    """
    if paused:
        return PAUSED_TEXT
    if (connection.get("kind") != "sample"
            and connection.get("consent_version") != consent_version):
        return TERMS_TEXT
    return None
```

In `careagents/accounts.py`:

```python
    def record_consent(self, account_id: str, conn_id: str,
                       version: str) -> bool:
        """Stamp a fresh consent on one of this account's real connections."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            if c is None or c.kind == "sample":
                return False
            c.consented_at = now()
            c.consent_version = version
            return True
```

In `careagents/app.py`, next to `refresh_connection`:

```python
    @app.post("/api/connections/<conn_id>/consent")
    @login_required
    def reconsent_connection(conn_id):
        """Accept the current terms for an existing real connection."""
        acct = current_account()
        conn = svc.get_connection(acct.id, conn_id)
        if conn is None or conn["kind"] == "sample":
            return jsonify({"error": "unknown connection"}), 404
        body = request.get_json(silent=True) or {}
        if body.get("consent") is not True:
            return jsonify({"error": "consent_required",
                            "consent_version": tester_terms.CONSENT_VERSION}), 428
        svc.record_consent(acct.id, conn_id, tester_terms.CONSENT_VERSION)
        return jsonify({"consent_version": tester_terms.CONSENT_VERSION})
```

In `home()`, compute and pass:

```python
        stale_consent = [
            {"id": c["id"], "label": c["label"]}
            for c in data["connections"]
            if c["kind"] != "sample" and c["status"] != "revoked"
            and c.get("consent_version") != tester_terms.CONSENT_VERSION]
```

and add `stale_consent=stale_consent,` to `render_template`.

In `careagents/templates/home.html`, directly above the `switch_prompt`
block:

```html
  {% for s in stale_consent %}
  <section class="switch-prompt">
    <p>Please review and accept the current terms for {{ s.label }}.</p>
    <button type="button" class="btn-primary"
            data-reconsent="{{ s.id }}">Review and accept</button>
  </section>
  {% endfor %}
```

In `careagents/static/home.js`, after the consent-card function:

```javascript
  document.querySelectorAll("[data-reconsent]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agreed = await showConsentCard();
      if (!agreed) return;
      btn.disabled = true;
      const res = await post(
        `/api/connections/${btn.dataset.reconsent}/consent`,
        { consent: true });
      if (!res.ok) { btn.disabled = false; return; }
      location.reload();
    });
  });
```

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_reconsent.py tests/test_careagents*.py -q`
Expected: PASS. The #843 copy tests must stay green: run
`uv run python -m` on the prose check they use, if one exists, against the
new sentences.

Mutations: (1) change `!= "sample"` to `== "fasten"`; the unknown-kind
assertion must fail. (2) drop `account_id=account_id` from
`record_consent`'s filter and the ownership lookup in the route; the 404
test must fail.

- [ ] **Step 5: Commit**

```bash
git add careagents/beta.py careagents/accounts.py careagents/app.py careagents/templates/home.html careagents/static/home.js tests/test_careagents_beta_reconsent.py
git commit -m "CareAgents: a terms bump asks each real-record tester once more"
```

---

### Task 7: Activity counts and the weekly number

**Files:**
- Modify: `careagents/accounts.py` (`count_activity`)
- Modify: `careagents/worker.py` (count `asked` after the gate)
- Modify: `careagents/app.py` (`review_submit`, the `body["confirmed"] = True` branch)
- Modify: `careagents/beta.py` (`weekly_counts`)
- Test: `tests/test_careagents_beta_weekly.py`

**Interfaces:**
- Consumes: `ActivityDay` (Task 1), `get_worker_agent_context()["connection"]` (Task 5).
- Produces: `AccountService.count_activity(account_id: str, field: str) -> None`, `field` in `{"asked", "approved"}`, raising `ValueError` otherwise.
- Produces: `beta.weekly_counts(session_scope, weeks: int = 4, now: datetime | None = None) -> list[dict]`, newest week first. Each dict is exactly `{"week": "2026-W40", "signed_up": int, "real_connected": int, "asked": int, "approved": int}`.

Definitions, one line each in the docstring: **signed_up**, accounts created
that ISO week (UTC). **real_connected**, distinct accounts with a non-sample
connection consented that week. **asked**, distinct accounts with `asked > 0`
that week. **approved**, distinct accounts with `approved > 0` that week.
`asked` and `approved` count real-record assistants only, so they are the
stage 1 cohort. Stage 0 appears only as `signed_up`. A retried run can count
`asked` twice; the weekly number counts accounts, so that does not change it.
An approval that ends `confirmed: null` is not counted (an undercount, stated).

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import datetime as dt

import pytest

from careagents import beta
from careagents.models import Account, ActivityDay, Connection
from tests.test_careagents import cfg, svc  # noqa: F401

NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)   # 2026-W41


def _ts(y, m, d):
    return dt.datetime(y, m, d, 12, tzinfo=dt.timezone.utc).timestamp()


def test_counts_are_distinct_accounts_per_iso_week(svc):
    with svc.session() as s:
        s.add_all([
            Account(id="acct_a", email="a@example.com",
                    created_at=_ts(2026, 10, 6)),
            Account(id="acct_b", email="b@example.com",
                    created_at=_ts(2026, 9, 29)),
            Connection(account_id="acct_a", kind="fasten", tenant_id="t-a",
                       consented_at=_ts(2026, 10, 6),
                       consent_version="2026-08-01"),
            Connection(account_id="acct_b", kind="sample", tenant_id="t-b"),
            ActivityDay(account_id="acct_a", day="2026-10-06", asked=3),
            ActivityDay(account_id="acct_a", day="2026-10-07", asked=1,
                        approved=1),
        ])
    rows = beta.weekly_counts(svc.session, weeks=2, now=NOW)
    assert rows == [
        {"week": "2026-W41", "signed_up": 1, "real_connected": 1,
         "asked": 1, "approved": 1},
        {"week": "2026-W40", "signed_up": 1, "real_connected": 0,
         "asked": 0, "approved": 0},
    ]


def test_every_value_is_an_integer_or_the_week_label(svc):
    for row in beta.weekly_counts(svc.session, weeks=3, now=NOW):
        assert set(row) == {"week", "signed_up", "real_connected",
                            "asked", "approved"}
        assert all(isinstance(v, int) for k, v in row.items() if k != "week")


def test_count_activity_adds_and_refuses_unknown_fields(svc, monkeypatch):
    from tests.test_careagents import _make_account
    acct = _make_account(svc, monkeypatch, "counter@example.com")
    acct_id = getattr(acct, "id", acct)
    svc.count_activity(acct_id, "asked")
    svc.count_activity(acct_id, "asked")
    with svc.session() as s:
        assert s.query(ActivityDay).one().asked == 2
    with pytest.raises(ValueError):
        svc.count_activity(acct_id, "message_text")


def test_a_sample_turn_does_not_count(
        cfg, svc, monkeypatch):
    from careagents.worker import RunWorker
    from tests.test_careagents import _chat_app
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "sample-count"},
               buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "count-worker").run_once()
    with svc.session() as s:
        assert s.query(ActivityDay).count() == 0


def _real_agent(cfg, svc, monkeypatch):
    """A Fasten connection made active, with an assistant on it."""
    from careagents import agent as agent_mod
    from careagents.app import create_app
    from tests.test_careagents import FakeClient, _login

    class _Turn:
        text, tool_calls, raw_tool_calls = "ok", [], []
    monkeypatch.setattr(agent_mod.llm, "complete", lambda *a, **k: _Turn())
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    svc.set_connection_status(fake.tenants[-1], "active")
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    return c, fake, agent_id


def test_a_real_record_turn_counts_as_asked(cfg, svc, monkeypatch):
    from careagents.worker import RunWorker
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "hi",
                                  "request_id": "real-count"},
               buffered=False)
    next(iter(r.response))
    r.close()
    RunWorker(cfg, fake, svc, "count-worker").run_once()
    with svc.session() as s:
        assert s.query(ActivityDay).one().asked == 1


def test_an_approval_counts_on_a_real_assistant_only(cfg, svc, monkeypatch):
    c, fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    ok = c.post(f"/review/{agent_id}/act-1/submit",
                json={"med-0": "yes", "nka": "true"})
    assert ok.status_code == 200 and ok.get_json()["confirmed"] is True
    with svc.session() as s:
        assert s.query(ActivityDay).one().approved == 1

    sample = c.post("/api/connections/sample").get_json()
    sample_agent = sample.get("agent_id") or c.post("/api/agents", json={
        "name": "S", "persona": "calm",
        "connection_id": sample["id"]}).get_json()["id"]
    c.post(f"/review/{sample_agent}/act-1/submit",
           json={"med-0": "yes", "nka": "true"})
    with svc.session() as s:
        assert s.query(ActivityDay).one().approved == 1
```

The `nka` field in these fake review submissions is the synthetic review
form's attestation input, as in the existing relay test
(`tests/test_careagents.py`, `test_review_relay_is_agent_scoped_and_holds_the_gate`).
It drives the fake engine to a 200. It is not an inferred "no known
allergies": the attestation is the human's own input to the review.

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_weekly.py -v`
Expected: FAIL (`weekly_counts`, `count_activity` missing).

- [ ] **Step 3: Implement**

In `careagents/accounts.py` (import `update` from `sqlalchemy` and
`IntegrityError` from `sqlalchemy.exc`):

```python
    _ACTIVITY_FIELDS = ("asked", "approved")

    def count_activity(self, account_id: str, field: str) -> None:
        """Add one to today's `asked` or `approved` for this account. The
        same increment-then-insert shape as analytics.record_view."""
        if field not in self._ACTIVITY_FIELDS:
            raise ValueError(f"unknown activity field {field!r}")
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        col = getattr(ActivityDay, field)
        stmt = (update(ActivityDay)
                .where(ActivityDay.account_id == account_id,
                       ActivityDay.day == day)
                .values({field: col + 1}))
        with self.session() as s:
            if s.execute(stmt).rowcount:
                return
        try:
            with self.session() as s:
                s.add(ActivityDay(account_id=account_id, day=day,
                                  **{field: 1}))
        except IntegrityError:
            with self.session() as s:
                s.execute(stmt)
```

In `careagents/worker.py`, after the `turn_block` return and before
`system_prompt`:

```python
        if context["connection"].get("kind") != "sample":
            try:
                self.accounts.count_activity(context["account_id"], "asked")
            except Exception:  # noqa: BLE001 - a count never fails a turn
                logger.warning("could not count activity for run %s", run_id)
```

In `careagents/app.py`, `review_submit`, right after
`body["confirmed"] = True`:

```python
            ctx = svc.get_agent_context(current_account().id, agent_id)
            if ctx and ctx["connection"]["kind"] != "sample":
                try:
                    svc.count_activity(current_account().id, "approved")
                except Exception:  # noqa: BLE001 - a count never fails a review
                    logger.warning("could not count an approval")
```

Append to `careagents/beta.py`:

```python
import datetime as _dt


def _iso_week(moment: _dt.datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def weekly_counts(session_scope, weeks: int = 4,
                  now: _dt.datetime | None = None) -> list[dict]:
    """The spec's weekly number (section 4.5), newest week first.

    Integers only, one row per ISO week (UTC), nothing about any person.
    Reads whole small tables: stage 1 is at most 25 testers.
    """
    from careagents.models import Account, ActivityDay, Connection
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    labels = [_iso_week(moment - _dt.timedelta(weeks=i))
              for i in range(weeks)]
    rows = {w: {"week": w, "signed_up": set(), "real_connected": set(),
                "asked": set(), "approved": set()} for w in labels}

    def week_of_ts(ts):
        return _iso_week(_dt.datetime.fromtimestamp(ts, _dt.timezone.utc))

    with session_scope() as s:
        for a in s.query(Account.id, Account.created_at):
            if a.created_at and week_of_ts(a.created_at) in rows:
                rows[week_of_ts(a.created_at)]["signed_up"].add(a.id)
        for c in s.query(Connection.account_id, Connection.kind,
                         Connection.consented_at):
            if (c.kind != "sample" and c.consented_at
                    and week_of_ts(c.consented_at) in rows):
                rows[week_of_ts(c.consented_at)]["real_connected"].add(
                    c.account_id)
        for d in s.query(ActivityDay.account_id, ActivityDay.day,
                         ActivityDay.asked, ActivityDay.approved):
            w = _iso_week(_dt.datetime.strptime(d.day, "%Y-%m-%d"))
            if w not in rows:
                continue
            if d.asked:
                rows[w]["asked"].add(d.account_id)
            if d.approved:
                rows[w]["approved"].add(d.account_id)
    return [{k: (v if k == "week" else len(v)) for k, v in rows[w].items()}
            for w in labels]
```

Move `import datetime as _dt` to the top of `beta.py` with the other import.

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_weekly.py tests/test_careagents.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add careagents/accounts.py careagents/worker.py careagents/app.py careagents/beta.py tests/test_careagents_beta_weekly.py
git commit -m "CareAgents: count real-record questions and approvals per week"
```

---

### Task 8: Operator commands and the runbook

**Files:**
- Create: `careagents/operator_cli.py`
- Modify: `careagents/app.py` (register next to `page-views`)
- Modify: `docs/runbooks/careagents-durable-worker.md` (the table that has the `CARE_ANALYTICS` row at `:93`, plus a short "Stage 1 operator commands" section)
- Test: `tests/test_careagents_beta_cli.py`

**Interfaces:**
- Consumes: `add_invite`, `revoke_invite`, `list_invites` (Task 2); `set_paused` (Task 4); `weekly_counts` (Task 7); `tester_terms.approved()` (Task 3).
- Produces: `operator_cli.register(app, svc, cfg) -> None` and these commands, run as `flask --app careagents.wsgi <command>`:
  - `invites add EMAIL [--by HANDLE]`, `invites list`, `invites revoke EMAIL`
  - `records pause EMAIL`, `records resume EMAIL`
  - `weekly-counts [--weeks N]`

Commands, not routes, for the same reason `page-views` gives
(`careagents/app.py:2197`): a new endpoint is a new thing to authorise,
rate-limit and get wrong.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import re

from careagents.app import create_app
from tests.careagents_stage1_helpers import allowlist_cfg
from tests.test_careagents import FakeClient, _login


def _runner(monkeypatch):
    from careagents.accounts import AccountService
    cfg = allowlist_cfg()
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    return app, app.test_cli_runner(), svc


def test_invite_add_list_revoke(monkeypatch):
    app, run, svc = _runner(monkeypatch)
    r = run.invoke(args=["invites", "add", "T@Example.com", "--by", "ops-1"])
    assert r.exit_code == 0, r.output
    assert "invited: t@example.com" in r.output
    assert "not honoured until the tester terms are approved" in r.output
    assert "t@example.com" in run.invoke(args=["invites", "list"]).output
    assert run.invoke(args=["invites", "revoke", "t@example.com"]).exit_code == 0
    assert run.invoke(args=["invites", "revoke", "t@example.com"]).exit_code == 1


def test_a_full_cohort_is_an_error_exit(monkeypatch):
    app, run, svc = _runner(monkeypatch)
    for i in range(25):
        svc.add_invite(f"t{i}@example.com")
    r = run.invoke(args=["invites", "add", "late@example.com"])
    assert r.exit_code != 0 and "full" in r.output


def test_pause_and_resume_by_email(monkeypatch):
    app, run, svc = _runner(monkeypatch)
    _login(app.test_client(), svc, monkeypatch, email="p@example.com")
    from careagents.models import Account

    def paused_at():
        with svc.session() as s:
            return s.query(Account).filter_by(
                email="p@example.com").one().real_paused_at

    assert run.invoke(args=["records", "pause", "p@example.com"]).exit_code == 0
    assert paused_at() is not None
    assert run.invoke(args=["records", "pause", "no@example.com"]).exit_code == 1
    assert run.invoke(args=["records", "resume", "p@example.com"]).exit_code == 0
    assert paused_at() is None


def test_weekly_counts_prints_integers_and_no_identity(monkeypatch):
    app, run, svc = _runner(monkeypatch)
    _login(app.test_client(), svc, monkeypatch, email="w@example.com")
    out = run.invoke(args=["weekly-counts", "--weeks", "2"]).output
    assert "@" not in out and "acct_" not in out
    lines = out.strip().splitlines()
    assert lines[0].split() == ["week", "signed_up", "real_connected",
                                "asked", "approved"]
    for line in lines[1:]:
        assert re.fullmatch(r"\d{4}-W\d{2}(\s+\d+){4}", line.strip())
```

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_cli.py -v`
Expected: FAIL ("No such command 'invites'").

- [ ] **Step 3: Implement**

`careagents/operator_cli.py`:

```python
"""Operator commands for the invited-tester stage (beta spec 4.2, 4.5, 4.6).

Run as `flask --app careagents.wsgi <command>` where CARE_DATABASE_URL
points at the database to change. Output goes to the operator's terminal
only. Nothing here writes an email address to the application log.
"""

from __future__ import annotations

import datetime as _dt

import click

from careagents import beta, tester_terms


def _day(ts) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%d")


def register(app, svc, cfg) -> None:
    @app.cli.group("invites")
    def invites():
        """Invite testers to connect their own records (up to 25)."""

    @invites.command("add")
    @click.argument("email")
    @click.option("--by", "invited_by", default="operator", show_default=True,
                  help="A short operator handle, not a name.")
    def invites_add(email, invited_by):
        try:
            result = svc.add_invite(email, invited_by)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{result}: {email.strip().lower()}")
        if cfg.real_records != "allowlist":
            click.echo(f"note: CARE_REAL_RECORDS is {cfg.real_records}; "
                       "invites are read only in allowlist mode.")
        if not tester_terms.approved():
            click.echo("note: invites are not honoured until the tester "
                       "terms are approved (#565).")

    @invites.command("list")
    def invites_list():
        rows = svc.list_invites()
        if not rows:
            click.echo("no invites")
        for r in rows:
            state = "revoked " + _day(r["revoked_at"]) if r["revoked_at"] \
                else "active"
            click.echo(f"{r['email']:<40} {_day(r['invited_at'])}  "
                       f"{r['invited_by']:<16} {state}")

    @invites.command("revoke")
    @click.argument("email")
    def invites_revoke(email):
        """Blocks new real connections. Existing ones keep working; use
        `records pause` to stop them."""
        if not svc.revoke_invite(email):
            raise click.ClickException("no active invite for that email")
        click.echo("revoked")

    @app.cli.group("records")
    def records():
        """Pause or resume one account's records."""

    @records.command("pause")
    @click.argument("email")
    def records_pause(email):
        """Stops every chat turn and new real connection for the account.
        Does NOT stop engine-side ingest or an existing MCP grant."""
        if not svc.set_paused(email, True):
            raise click.ClickException("no account with that email")
        click.echo("paused")

    @records.command("resume")
    @click.argument("email")
    def records_resume(email):
        if not svc.set_paused(email, False):
            raise click.ClickException("no account with that email")
        click.echo("resumed")

    @app.cli.command("weekly-counts")
    @click.option("--weeks", default=4, show_default=True)
    def weekly(weeks):
        """The weekly number: counts only, one row per ISO week."""
        click.echo("week      signed_up  real_connected  asked  approved")
        for r in beta.weekly_counts(svc.session, weeks=weeks):
            click.echo(f"{r['week']}  {r['signed_up']:>9}  "
                       f"{r['real_connected']:>14}  {r['asked']:>5}  "
                       f"{r['approved']:>8}")
```

`click.ClickException` exits with code 1 and prints `Error: <message>`, which
the tests rely on.

In `careagents/app.py`, directly before `return app`:

```python
    from careagents import operator_cli
    operator_cli.register(app, svc, cfg)
```

In `docs/runbooks/careagents-durable-worker.md`, add a section
"Stage 1 operator commands" with one row per command, and these facts:
- invites are read only when `CARE_REAL_RECORDS=allowlist` **and** the tester terms are approved; the env allowlist keeps working and is not counted in the 25;
- revoke stops new connections only;
- pause stops chat turns (all surfaces), new real connections, new MCP grants, refresh and upload; it does not stop Fasten webhook ingest in the engine or an existing MCP grant; the full stop is `CARE_REAL_RECORDS=off` plus Disconnect or Delete;
- `weekly-counts` prints integers only; copy the numbers, never per-person data, into any report.

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add careagents/operator_cli.py careagents/app.py docs/runbooks/careagents-durable-worker.md tests/test_careagents_beta_cli.py
git commit -m "CareAgents: operator commands for invites, pause and the weekly number"
```

---

### Task 9: One pending Fasten row per account under concurrent connects (#847)

**Files:**
- Modify: `careagents/app.py` (`_start_connection`, the #843 Fasten reuse block)
- Modify: `careagents/accounts.py` (`claim_sample_start` docstring only)
- Test: `tests/test_careagents_beta_fasten_race.py`

**Interfaces:**
- Consumes: `svc.claim_sample_start(account_id) -> bool`, `svc.release_sample_start(account_id) -> None`, `svc.pending_connection(account_id, kind) -> dict | None` (#843), `_real_open` (Task 4).
- Produces: `_persist_connection(connector_id, acct, plan, consent_version)`, a closure holding what `_start_connection` did after the consent check (seed, `add_connection`, the sample answer, the connect URL answer). No behaviour change for other kinds.

- [ ] **Step 1: Write the failing tests**

```python
"""Two Fasten connects from one account at the same moment make one pending
row (#847). The second arrives while the first holds the lease."""

from __future__ import annotations

from careagents.models import Connection
from tests.careagents_stage1_helpers import allowlist_cfg, approve_terms
from tests.test_careagents import FakeClient, _login, cfg, svc  # noqa: F401


def _two_tabs(cfg, svc, monkeypatch, email="gene@example.com"):
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    first = app.test_client()
    _login(first, svc, monkeypatch, email=email)
    second = app.test_client()
    with first.session_transaction() as s:
        aid = s["account_id"]
    with second.session_transaction() as s:
        s["account_id"] = aid
    return first, second


def test_a_connect_that_arrives_mid_insert_does_not_add_a_row(
        cfg, svc, monkeypatch):
    first, second = _two_tabs(cfg, svc, monkeypatch)
    read = svc.pending_connection
    fired = {"r": None}

    def read_then_second_tab(aid, kind):
        seen = read(aid, kind)
        if fired["r"] is None:
            fired["r"] = second.post("/api/connections/fasten",
                                     json={"consent": True})
        return seen

    monkeypatch.setattr(svc, "pending_connection", read_then_second_tab)
    r = first.post("/api/connections/fasten", json={"consent": True})

    assert r.status_code == 200
    assert fired["r"].status_code == 409
    with svc.session() as s:
        assert s.query(Connection).filter_by(kind="fasten").count() == 1


def test_a_second_connect_after_the_first_reuses_the_pending_row(
        cfg, svc, monkeypatch):
    first, second = _two_tabs(cfg, svc, monkeypatch)
    a = first.post("/api/connections/fasten", json={"consent": True})
    b = second.post("/api/connections/fasten", json={"consent": True})
    assert a.get_json()["id"] == b.get_json()["id"]
    assert b.get_json()["existing"] is True


def test_a_revoked_invite_cannot_reuse_a_pending_row(monkeypatch):
    """Review Focus 3: the reuse path hands out a connect URL too."""
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg = allowlist_cfg()
    svc = AccountService(cfg)
    approve_terms(monkeypatch)
    svc.add_invite("gene@example.com")
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/fasten",
                  json={"consent": True}).status_code == 200
    svc.revoke_invite("gene@example.com")
    r = c.post("/api/connections/fasten", json={"consent": True})
    assert r.status_code == 503
    assert "connect_url" not in (r.get_json() or {})
```

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_fasten_race.py -v`
Expected: the first test FAILS with 2 rows and a 200 for the second tab.
The other two should already pass; they pin behaviour this task must keep.

- [ ] **Step 3: Implement**

In `_start_connection`, move everything after the consent block (the #843
Fasten reuse check through the final `return jsonify(out)`) into a new
closure, and remove the Fasten reuse check from it:

```python
    def _persist_connection(connector_id, acct, plan, consent_version):
        tenant = plan["tenant"]
        ...  # the existing seed / add_connection / sample / connect_url code,
             # unchanged, minus the Fasten pending_connection block
```

Then end `_start_connection` with:

```python
        if connector_id != "fasten":
            return _persist_connection(connector_id, acct, plan,
                                       consent_version)
        # Two tabs or devices connecting at once (#847): both passed the
        # pending check before either inserted. The account's connect lease
        # (the same one the sample tap uses) makes the check and the insert
        # one step. A pending row keeps the consent_version it was made
        # with; a stale one is asked again by the worker (beta spec 4.3).
        if not svc.claim_sample_start(acct.id):
            return jsonify({"status": "connecting",
                            "error": "Your records are already on their "
                                     "way. Try again in a moment."}), 409
        try:
            waiting = svc.pending_connection(acct.id, "fasten")
            if waiting:
                return jsonify({
                    "id": waiting["id"], "status": "pending",
                    "existing": True,
                    "connect_url": hc.fasten_connect_url(
                        waiting["tenant_id"])})
            return _persist_connection(connector_id, acct, plan,
                                       consent_version)
        finally:
            svc.release_sample_start(acct.id)
```

The sample route already holds this lease when it calls `_start_connection`,
and it never reaches the Fasten branch, so there is no double claim.

Update the `claim_sample_start` docstring's first line to: "Win the right to
mint a tenant for this account: the sample tap and the Fasten connect share
this lease (#847)."

Check the 409 sentence against the #843 copy rules (plain, no em dash).

- [ ] **Step 4: Run to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_careagents_beta_fasten_race.py tests/test_careagents_calm_hub_sample_race.py tests/test_careagents_calm_hub_sample.py tests/test_careagents.py -q`
Expected: PASS.

Mutation: remove the `claim_sample_start` guard (keep the `try` body). The
first test must fail with two rows. Revert.

- [ ] **Step 5: Commit**

```bash
git add careagents/app.py careagents/accounts.py tests/test_careagents_beta_fasten_race.py
git commit -m "CareAgents: one pending Fasten row under concurrent connects (#847)"
```

---

### Task 10: Verification and sign-off

No production code. This is the evidence the signers in
`docs/qa/sign-off-standard.md` §3 need.

- [ ] **Step 1: Full suite and lint (G2)**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
uv run ruff check .
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_guardrail_conformance.py -q
```

Quote the pass counts in the PR. If CI has a Postgres lane, also run the
CareAgents tests with `CARE_TEST_DATABASE_URL` pointed at a local Postgres,
because the new unique constraints and `ADD COLUMN` behave differently there.

- [ ] **Step 2: Mutation evidence (G3)**

Collect the mutation results from Tasks 3, 4, 5, 6 and 9 into
`docs/evidence/2026-MM-DD-careagents-beta-stage1-mutations.txt`, in the
format of `docs/evidence/2026-09-26-careagents-calm-hub-mutations.txt`.
Commit the work first so a restore returns to it, purge `__pycache__` before
each run, and assert each mutation's replacement count.

- [ ] **Step 3: Real run (G4)**

On a free port (not 5000): start CareAgents and the engine locally with
`CARE_REAL_RECORDS=allowlist`, a local SQLite `CARE_DATABASE_URL`, and a test
key. Then, with synthetic `@example.com` accounts:
1. `flask --app careagents.wsgi invites add tester@example.com` shows the "not honoured" note, and the account's menu shows "Coming soon".
2. With `TERMS_VERSION` set locally (do not commit), the same account's menu opens and the consent card shows the placeholder terms block.
3. `records pause tester@example.com`, then a chat turn answers the paused sentence. The worker log shows no provider call.
4. `weekly-counts` prints four integers per week.
Curl the running server after each change so a stale process cannot pass.

- [ ] **Step 4: Signers**

| Signer | Rows | What they exercise |
|---|---|---|
| QA verifier | G1 to G5 | This plan's tasks against the spec table at the top; suite counts; mutation file; the real run. |
| Security tester | V1, V2, V3, V6 | V2: no new record read or write path, so report it as not applicable, citing the worker gate that returns before `recent_messages`. A second account's connection id on `/api/connections/<id>/consent` and on the Fasten reuse path; a paused account on every surface; `weekly-counts` and `invites list` output carry no health data; no email in the application log (`grep -r "@example"` over the captured log). |
| Patient tester | V4, G7 | At 375px: the consent card with the terms block, the "review and accept the current terms" hub line and its accept flow, the paused chat answer, the 409 connect sentence. |
| CTO | Architecture | This plan (done in the design pass above) and any deviation from it. |

- [ ] **Step 5: Open the PR, do not merge**

Draft PR from the feature branch. Body lists the narrowed scope table, the
BYOK note, R4 (what pause does not stop), R7 (the `NULL` consent rollout
note), and links #838, #847, #565, #718. Check
`gh pr view <n> --json autoMergeRequest` before and after review.
Deploying CareAgents is a separate, owner-authorised `railway up` from a
staged directory (`docs/runbooks/careagents-durable-worker.md`); web and
worker from the same stage.

## Fast-follow issues to file when this plan's PR opens

1. Waitlist (spec §4.1) and in-app feedback (spec §4.4), with intake via #718 and contactus@healthclaw.io until then.
2. Pause also revokes MCP grants, before the connector leaves token-lock (R4).
3. Retention for revoked invites: purge rows revoked more than N days ago at the end of stage 1.
4. iMessage inbound does not call `claim_daily_turn`, so the daily cap does not bound it (found while reading `careagents/app.py:2010-2049`; not changed here).
