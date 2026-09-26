# CareAgents calm hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person opens CareAgents and sees one assistant, the records it reads, and anything waiting for their approval. Connecting a source is one clear menu.

**Architecture:** The server stays authoritative. Two compare-and-set columns on `ca_accounts` make the sample connect and the first assistant happen once. A new pure module, `careagents/hub.py`, turns `list_home` output into the words the hub shows. Settings move to a new `/settings` page that reuses `home.js`.

**Tech Stack:** Flask, SQLAlchemy (SQLite locally, Postgres in CI), Jinja, vanilla JS and CSS, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-26-careagents-calm-hub-design.md`

## Global Constraints

- Plain patient copy: no operator words, sentence case, no em dashes in patient copy.
- CareAgents stores no PHI: accounts and pointers only. New columns hold timestamps, never record content.
- Synthetic data only in tests. Use `@example.com`, `@example.org` or `@example.test` addresses.
- Lint with `uv run ruff check .`, never `uvx` or `pipx`.
- Python 3.11. No 3.12-only syntax.
- New tests go in new files named `tests/test_careagents_calm_hub_*.py`.
- Mutation evidence is required for the sample dedupe and for the ownership check.

Repo rules that apply to every task:

- Before the first edit of a task, run `uv run python scripts/lane_check.py <paths>`. On 2026-09-26 it reports overlaps from leftover worktrees on every CareAgents file. Those are expected. An open PR or a claimed issue on a path is a stop.
- Commit with `git commit -s`. The last paragraph of every message is `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Never push to `main`, never merge, never deploy.
- Some existing pins live in `tests/test_careagents.py` and `tests/test_careagents_consent.py`. Tasks that change pinned behavior edit those pins. Each edit is listed in its task.

## Review Focus

| # | Failure mode | Test that pins it | Task |
|---|---|---|---|
| 1 | A double tap on the sample mints two tenants or two rows | `test_a_tap_during_seeding_mints_no_second_tenant` and `test_a_second_tap_returns_the_existing_sample` in `tests/test_careagents_calm_hub_sample.py` | 1 |
| 2 | A failed pending count renders as zero | `test_a_failed_count_is_503_with_no_count` in `tests/test_careagents_calm_hub_waiting.py`; `test_the_waiting_line_starts_as_checking_never_zero` in `tests/test_careagents_calm_hub_page.py`; e2e `a failed count says so` | 5B, 5D |
| 3 | Repeated ingest-complete callbacks create two assistants | `test_repeated_ingest_complete_callbacks_create_one_agent` in `tests/test_careagents_calm_hub_first_agent.py` | 2 |
| 4 | Change records points an assistant at a revoked or another account's connection | `test_change_records_refuses_another_accounts_connection`, `test_change_records_refuses_a_revoked_connection`, `test_move_agent_checks_ownership_in_the_service` in `tests/test_careagents_calm_hub_agent_menu.py` | 3 |
| 5 | An existing account with many agents and rows breaks or loses data after deploy | `test_an_account_with_old_duplicate_samples_keeps_them_all` (Task 1), `test_an_existing_account_with_agents_gets_no_new_one` (Task 2), `test_a_crowded_legacy_account_renders_every_row` (Task 5D) | 1, 2, 5D |

## Task order

Settings (Task 4) comes before the hub rewrite (Task 5). That way the hub template is written once, in its final form.

| Task | What | New test file |
|---|---|---|
| 1 | One sample per account; catalog states, plain copy, availability chips | `test_careagents_calm_hub_sample.py`, `test_careagents_calm_hub_catalog.py` |
| 2 | First assistant on first active connection; sample lands in chat, intake starter first | `test_careagents_calm_hub_first_agent.py` |
| 3 | Rename, change records, delete assistant, with ownership checks | `test_careagents_calm_hub_agent_menu.py` |
| 4 | `/settings`: passkeys, surfaces, grants, sign out, delete account | `test_careagents_calm_hub_settings.py` |
| 5A | `careagents/hub.py`: status words, counts, "Updated N days ago" | `test_careagents_calm_hub_view.py` |
| 5B | `GET /api/approvals/count` | `test_careagents_calm_hub_waiting.py` |
| 5C | "Switch Juniper to your records?" answer endpoint | `test_careagents_calm_hub_switch.py` |
| 5D | Hub template, JS and CSS rewrite; e2e spec replaced | `test_careagents_calm_hub_page.py` |
| 6 | Copy cleanup: banner, Telegram handler, iMessage line, Fasten step 2 | `test_careagents_calm_hub_copy.py`, `test_fasten_connect_step_two_plain.py` |
| 7 | Health Skillz guided two-step as an Available source | `test_careagents_calm_hub_health_skillz.py` |
| 8 | Browser run of the new-account journey at 375px, before and after | evidence doc |

---

### Task 1: One sample per account, and the catalog's plain states

**Files:**
- Modify: `careagents/models.py` (class `Account`, lines 36-52; `_ensure_columns`, lines 208-247)
- Modify: `careagents/accounts.py` (import line 19; new methods after `add_connection`, line 301)
- Modify: `careagents/connectors.py` (lines 19-123 catalog; `start` fasten branch lines 157-160; `refresh` lines 213-224)
- Modify: `careagents/app.py` (`add_connection`, lines 631-668)
- Modify: `tests/test_careagents.py` (line 3749, one line)
- Create: `tests/test_careagents_calm_hub_sample.py`
- Create: `tests/test_careagents_calm_hub_catalog.py`

**Interfaces:**
- Consumes: `AccountService.add_connection`, `connectors.start`, `HealthClawClient.seed`.
- Produces:
  - `careagents.accounts.SAMPLE_LEASE_SECONDS: int = 60`
  - `AccountService.active_sample(account_id: str) -> dict | None`
  - `AccountService.claim_sample_start(account_id: str) -> bool`
  - `AccountService.release_sample_start(account_id: str) -> None`
  - `Account.sample_claim_at` (nullable `Float`)
  - `connectors.GROUPS: tuple[tuple[str, str], ...]`
  - Each `connectors.catalog()` item gains `group: str` and `chip: "Available" | "Coming soon"`.
  - `POST /api/connections/sample` answers `{"id", "status": "active", "existing": bool}`, or 409 `{"status": "connecting", "error"}`.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/models.py careagents/accounts.py careagents/connectors.py careagents/app.py tests/test_careagents.py
```

Expected: leftover-worktree overlaps only. Stop on an open PR or claimed issue.

- [ ] **Step 2: Write the failing sample tests**

Create `tests/test_careagents_calm_hub_sample.py`:

```python
"""One sample per account (calm hub spec section 4 and 9)."""

from __future__ import annotations

from careagents.healthclaw import HealthClawError
from careagents.models import Connection
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _make_account, cfg, svc)


def _app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _samples(svc):
    with svc.session() as s:
        return s.query(Connection).filter_by(kind="sample").count()


def _account_id(client):
    with client.session_transaction() as s:
        return s["account_id"]


class _TapDuringSeed(FakeClient):
    """Fires a second tap from inside the first tap's seed call.

    That is the window a double tap lands in: the first request has minted
    a tenant and is seeding it, and no connection row exists yet.
    """

    def __init__(self):
        super().__init__()
        self.tap = None
        self.second_tap = None

    def seed(self, tenant):
        tap, self.tap = self.tap, None
        if tap is not None:
            self.second_tap = tap()
        return super().seed(tenant)


def test_a_second_tap_returns_the_existing_sample(cfg, svc, monkeypatch):
    """MUTATION M1: drop the first active_sample early return -> red."""
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    first = c.post("/api/connections/sample")
    second = c.post("/api/connections/sample")
    assert first.status_code == 200 and second.status_code == 200
    assert first.get_json()["existing"] is False
    assert second.get_json()["existing"] is True
    assert second.get_json()["id"] == first.get_json()["id"]
    assert len(fake.tenants) == 1
    assert _samples(svc) == 1


def test_a_tap_during_seeding_mints_no_second_tenant(cfg, svc, monkeypatch):
    """MUTATION M2: make claim_sample_start always return True -> red."""
    fake = _TapDuringSeed()
    app = _app(cfg, svc, fake)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    other = app.test_client()
    with other.session_transaction() as s:
        s["account_id"] = _account_id(c)
    fake.tap = lambda: other.post("/api/connections/sample")

    first = c.post("/api/connections/sample")

    assert first.status_code == 200, first.get_data(as_text=True)
    assert fake.second_tap.status_code == 409
    assert fake.second_tap.get_json()["status"] == "connecting"
    assert "—" not in fake.second_tap.get_json()["error"]
    assert len(fake.tenants) == 1
    assert _samples(svc) == 1


def test_a_failed_seed_releases_the_lease(cfg, svc, monkeypatch):
    class _Down(FakeClient):
        def seed(self, tenant):
            raise HealthClawError("seed failed", 503)

    c = _app(cfg, svc, _Down()).test_client()
    _login(c, svc, monkeypatch)
    assert c.post("/api/connections/sample").status_code == 503
    assert svc.claim_sample_start(_account_id(c)) is True


def test_the_sample_lease_is_single_winner_and_expires(svc, monkeypatch):
    from careagents import accounts
    acct = _make_account(svc, monkeypatch, "lease@example.com")
    assert svc.claim_sample_start(acct.id) is True
    assert svc.claim_sample_start(acct.id) is False
    later = accounts.now() + accounts.SAMPLE_LEASE_SECONDS + 1
    monkeypatch.setattr(accounts, "now", lambda: later)
    assert svc.claim_sample_start(acct.id) is True
    svc.release_sample_start(acct.id)
    assert svc.claim_sample_start(acct.id) is True


def test_an_account_with_old_duplicate_samples_keeps_them_all(
        cfg, svc, monkeypatch):
    """Nothing is merged or deleted. The oldest active sample is the one
    a tap opens."""
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    aid = _account_id(c)
    oldest = svc.add_connection(aid, "sample", "ca-legacy-1", "Sample records")
    svc.add_connection(aid, "sample", "ca-legacy-2", "Sample records")

    r = c.post("/api/connections/sample")

    assert r.get_json()["id"] == oldest
    assert _samples(svc) == 2
    assert fake.tenants == []
    assert c.get("/home").status_code == 200


def test_a_revoked_sample_does_not_block_a_new_one(cfg, svc, monkeypatch):
    fake = FakeClient()
    c = _app(cfg, svc, fake).test_client()
    _login(c, svc, monkeypatch)
    first = c.post("/api/connections/sample").get_json()["id"]
    c.post(f"/api/connections/{first}/disconnect")
    second = c.post("/api/connections/sample").get_json()
    assert second["id"] != first and second["existing"] is False
```

- [ ] **Step 3: Write the failing catalog tests**

Create `tests/test_careagents_calm_hub_catalog.py`:

```python
"""The connector menu's words (calm hub spec section 4 and 7)."""

from __future__ import annotations

import json

import pytest

from careagents import connectors
from careagents.config import Config
from tests.test_careagents import FakeClient

OPERATOR_PHRASES = ("not configured", "deployment", "sidecar", "wired")

CONFIGS = [
    {},
    {"FASTEN_PUBLIC_KEY": "pub"},
    {"CARE_WEARABLES_ENABLED": "1"},
    {"FASTEN_PUBLIC_KEY": "pub", "CARE_WEARABLES_ENABLED": "1"},
]


def _cfg(**env):
    base = {"CARE_DATABASE_URL": "sqlite:///:memory:",
            "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "m"}
    return Config(env={**base, **env})


def _by_id(cfg, real_records):
    return {m["id"]: m for m in
            connectors.catalog(cfg, real_records=real_records)}


@pytest.mark.parametrize("env", CONFIGS)
@pytest.mark.parametrize("real_records", [False, True])
def test_catalog_output_contains_no_operator_phrases(env, real_records):
    text = json.dumps(connectors.catalog(
        _cfg(**env), real_records=real_records)).lower()
    for phrase in OPERATOR_PHRASES:
        assert phrase not in text, phrase


def test_a_refused_start_or_refresh_names_no_operator_detail():
    cfg, fake = _cfg(), FakeClient()      # no Fasten key, wearables off
    said = [
        connectors.start("fasten", None, cfg, fake, real_records=True)["error"],
        connectors.refresh("fasten", "ca-1", None, cfg, fake)["error"],
        connectors.refresh("wearable", "ca-1", "apple", cfg, fake)["reason"],
    ]
    for line in said:
        for phrase in OPERATOR_PHRASES:
            assert phrase not in line.lower(), line


def test_the_closed_menu_offers_only_the_sample():
    items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=False)
    assert items["sample"]["chip"] == "Available"
    for sid in connectors.REAL_RECORD_SOURCES:
        assert items[sid]["chip"] == "Coming soon", sid


def test_the_open_menu_marks_phase_one_sources_available():
    items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=True)
    assert items["fasten"]["chip"] == "Available"
    assert items["direct"]["chip"] == "Available"
    for sid in ("hbo", "healthex", "shl", "wearable"):
        assert items[sid]["chip"] == "Coming soon", sid


def test_every_source_sits_in_its_named_group():
    groups = {m["id"]: m["group"] for m in
              connectors.catalog(_cfg(), real_records=True)}
    assert groups == {"sample": "sample", "fasten": "find",
                      "hbo": "services", "healthex": "services",
                      "direct": "file", "shl": "file",
                      "wearable": "devices"}
    names = dict(connectors.GROUPS)
    assert list(names) == ["find", "services", "file", "devices"]
    assert names["find"] == "Find my records"


def test_patient_copy_uses_the_spec_words_and_no_em_dash():
    open_items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=True)
    assert open_items["sample"]["label"] == "Explore with made-up records"
    assert open_items["sample"]["blurb"] == "Made-up records to explore safely."
    assert open_items["fasten"]["label"] == (
        "Find my records at my doctor or hospital")
    assert open_items["direct"]["label"] == (
        "Upload a file from your patient portal")
    everything = list(open_items.values()) + connectors.catalog(
        _cfg(), real_records=False)
    for m in everything:
        assert "—" not in m["label"] + m["blurb"], m["id"]
        assert "no signup" not in m["blurb"].lower(), m["id"]
```

- [ ] **Step 4: Run the new tests and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_sample.py tests/test_careagents_calm_hub_catalog.py -q
```

Expected: FAIL. `existing` is missing, `claim_sample_start` does not exist, and `chip`, `group` and `GROUPS` are absent.

- [ ] **Step 5: Add the lease column**

In `careagents/models.py`, inside `class Account` after `last_login_at` (line 41):

```python
    # Sample-connect lease (calm hub spec section 4). Set while this
    # account's sample tenant is minted and seeded, cleared after. A double
    # tap races on this row, not on the tenant mint. A timestamp, not PHI.
    sample_claim_at = Column(Float, nullable=True)
```

In `_ensure_columns`, after `tables = insp.get_table_names()` (line 216):

```python
    if "ca_accounts" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_accounts")}
        with engine.begin() as conn:
            if "sample_claim_at" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_accounts ADD COLUMN sample_claim_at FLOAT"))
```

- [ ] **Step 6: Add the service methods**

In `careagents/accounts.py`, change line 19 to `from sqlalchemy import or_, text`. After `CODE_MAX` (line 38) add:

```python
SAMPLE_LEASE_SECONDS = 60  # a sample seed that has not finished by then died
```

After `add_connection` (line 301) add:

```python
    def active_sample(self, account_id: str) -> dict | None:
        """The account's oldest active sample, if any. Older accounts can
        hold several; none is merged or removed (calm hub spec section 5)."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(account_id=account_id, kind="sample",
                            status="active")
                 .order_by(Connection.connected_at.asc()).first())
            return _conn_dict(c) if c else None

    def claim_sample_start(self, account_id: str) -> bool:
        """Win the right to mint this account's sample tenant.

        A compare-and-set on the account row: one caller sets the lease and
        every overlapping caller updates zero rows. A lease older than
        SAMPLE_LEASE_SECONDS counts as abandoned, so a crash mid-seed cannot
        lock the sample out for good.
        """
        t = now()
        with self.session() as s:
            won = (s.query(Account)
                   .filter(Account.id == account_id,
                           or_(Account.sample_claim_at.is_(None),
                               Account.sample_claim_at
                               < t - SAMPLE_LEASE_SECONDS))
                   .update({"sample_claim_at": t},
                           synchronize_session=False))
            return won == 1

    def release_sample_start(self, account_id: str) -> None:
        with self.session() as s:
            (s.query(Account).filter(Account.id == account_id)
             .update({"sample_claim_at": None}, synchronize_session=False))
```

- [ ] **Step 7: Rewrite the catalog**

In `careagents/connectors.py`, add after `from __future__ import annotations` (line 19):

```python
import logging

logger = logging.getLogger(__name__)
```

Replace `_CATALOG` (lines 32-65) with:

```python
# The menu's groups, in the order the hub shows them (calm hub spec
# section 4). The sample sits outside them: it is the closed state's one
# action and the open state's small link.
GROUPS = (("find", "Find my records"),
          ("services", "Record services"),
          ("file", "Bring a file"),
          ("devices", "Devices and apps"))

_CATALOG = [
    {"id": "sample", "tier": "live", "icon": "🧪", "group": "sample",
     "label": "Explore with made-up records",
     "blurb": "Made-up records to explore safely."},
    {"id": "fasten", "tier": "live", "icon": "🏥", "group": "find",
     "label": "Find my records at my doctor or hospital",
     "blurb": "Sign in to your patient portal. We never see your password."},
    {"id": "hbo", "tier": "soon", "icon": "🏦", "group": "services",
     "label": "Health Bank One",
     "blurb": "Connect your Health Bank One account."},
    {"id": "healthex", "tier": "soon", "icon": "🧬", "group": "services",
     "label": "HealthEx",
     "blurb": "Connect your HealthEx account."},
    # `direct` is the zero-integration ingest path (#227): the signed-in
    # patient posts a FHIR Bundle they exported from another app or portal.
    # `shl` stays coming soon until the encrypted-manifest decoder ships
    # (#225): ship the mechanism, then the copy.
    {"id": "direct", "tier": "import", "icon": "📄", "group": "file",
     "label": "Upload a file from your patient portal",
     "blurb": "A health record file you downloaded from your portal "
              "or another app."},
    {"id": "shl", "tier": "soon", "icon": "🔗", "group": "file",
     "label": "SMART Health Link",
     "blurb": "Import a record someone shared with you as a link."},
    {"id": "wearable", "tier": "live", "icon": "⌚️", "group": "devices",
     "label": "Apple Health and wearables",
     "blurb": "Oura, Whoop, Garmin, Fitbit, Strava and Apple Health.",
     "providers": WEARABLE_PROVIDERS},
]
```

Replace `catalog()` (lines 83-123) with:

```python
def _closed_reason(connector_id: str, cfg) -> str:
    """Why an open real-record source still cannot start here. For the
    server log only: a person sees "Coming soon" (calm hub spec section 4)."""
    if connector_id == "fasten" and not getattr(cfg, "fasten_public_key", ""):
        return "FASTEN_PUBLIC_KEY is unset"
    if connector_id == "wearable" and not getattr(cfg, "wearables_enabled",
                                                  False):
        return "CARE_WEARABLES_ENABLED is off"
    return ""


def catalog(cfg, real_records: bool = False) -> list[dict]:
    """The menu with per-account availability resolved.

    `real_records` is whether the viewing account may START a real-record
    connection (`cfg.real_records_open_for(email)`). The default is closed,
    so a caller that forgets fails safe. `chip` is the one status word each
    source shows; "Connected" is added by the hub, which knows the account.
    """
    out = []
    for c in _CATALOG:
        item = {k: c[k] for k in ("id", "label", "blurb", "icon", "tier",
                                  "group")}
        if "providers" in c:
            item["providers"] = c["providers"]
        if c["id"] in REAL_RECORD_SOURCES and not real_records:
            # No consent card (nothing to consent to), and the blurb says
            # what a tester can do instead.
            item["tier"] = "soon"
            item["note"] = "coming soon"
            item["blurb"] = ("Not open in this beta. Start with the sample "
                             "records.")
        else:
            # Every real-record source that can start gets the consent card.
            # `direct` is the patient's own PHI too.
            if c["id"] in REAL_RECORD_SOURCES:
                item["requires_consent"] = True
            reason = _closed_reason(c["id"], cfg)
            if reason:
                logger.info("connector %s shown as coming soon: %s",
                            c["id"], reason)
                item["tier"] = "soon"
                item["note"] = "coming soon"
        item["chip"] = "Coming soon" if item["tier"] == "soon" else "Available"
        out.append(item)
    return out
```

In `start()`, replace the fasten no-key refusal (lines 157-159) with:

```python
        if not getattr(cfg, "fasten_public_key", ""):
            logger.info("fasten start refused: FASTEN_PUBLIC_KEY is unset")
            return {"error": "Finding your records isn't available right "
                             "now.", "code": 503}
```

In `refresh()`, replace the fasten no-key refusal (lines 213-215) with the same three lines, using `"fasten refresh refused: FASTEN_PUBLIC_KEY is unset"`. Replace the wearable reason (lines 222-224) with:

```python
        if not getattr(cfg, "wearables_enabled", False):
            logger.info("wearable refresh refused: CARE_WEARABLES_ENABLED off")
            return {"unsupported": True,
                    "reason": "Refreshing this source isn't available yet."}
```

- [ ] **Step 8: Put the dedupe in the connect route**

In `careagents/app.py`, replace lines 631-668 (the whole `add_connection` route) with:

```python
    def _sample_answer(account_id, conn_id, existing):
        """What a sample tap answers. Task 2 adds the chat redirect."""
        return {"id": conn_id, "status": "active", "existing": existing}

    def _start_connection(connector_id, acct, body):
        # New connections only (D3): refresh, poll, upload and delete on an
        # existing connection never consult the real-records switch.
        plan = connectors.start(
            connector_id, body.get("provider"), cfg, hc,
            real_records=cfg.real_records_open_for(acct.email))
        if plan.get("error"):
            return jsonify({"error": plan["error"]}), plan.get("code", 400)
        if plan.get("soon"):
            # Not live yet — acknowledge intent (never a dead-end button).
            return jsonify({"soon": True, "connector": connector_id})
        # Real-record connections require informed consent, enforced here so
        # a client that skips the consent card is refused server-side (CARIN
        # CoC: proactive consent in advance of personal data disclosure).
        consent_version = None
        if plan.get("requires_consent"):
            if body.get("consent") is not True:
                return jsonify({"error": "consent_required",
                                "consent_version": CONSENT_VERSION}), 428
            consent_version = CONSENT_VERSION
        tenant = plan["tenant"]
        if plan.get("seed"):
            try:
                hc.seed(tenant)
            except HealthClawError:
                return jsonify({"error": "records service unavailable"}), 503
        cid = svc.add_connection(acct.id, connector_id, tenant, plan["label"],
                                 status=plan["status"],
                                 provider=plan.get("provider"),
                                 consent_version=consent_version)
        if connector_id == "sample":
            return jsonify(_sample_answer(acct.id, cid, False))
        out = {"id": cid, "status": plan["status"]}
        if plan.get("connect_url"):
            out["connect_url"] = plan["connect_url"]
        return jsonify(out)

    @app.post("/api/connections/<connector_id>")
    @login_required
    def add_connection(connector_id):
        acct = current_account()
        body = request.get_json(silent=True) or {}
        if connector_id != "sample":
            return _start_connection(connector_id, acct, body)
        # One sample per account (calm hub spec section 4). A second tap
        # opens the one that exists; a tap while another is still seeding
        # mints nothing.
        existing = svc.active_sample(acct.id)
        if existing:
            return jsonify(_sample_answer(acct.id, existing["id"], True))
        if not svc.claim_sample_start(acct.id):
            existing = svc.active_sample(acct.id)
            if existing:
                return jsonify(_sample_answer(acct.id, existing["id"], True))
            return jsonify({"status": "connecting",
                            "error": "Your sample records are on their way. "
                                     "Try again in a moment."}), 409
        try:
            return _start_connection(connector_id, acct, body)
        finally:
            svc.release_sample_start(acct.id)
```

- [ ] **Step 9: Update the one existing pin that made two samples**

In `tests/test_careagents.py`, `test_deleting_the_account_purges_every_tenant_then_removes_the_row`, replace line 3749:

```python
    conn_b = c.post("/api/connections/direct",
                    json={"consent": True}).get_json()["id"]
```

The test still asserts two purged connections. The second one is now a different kind.

- [ ] **Step 10: Run the new tests and the CareAgents suite**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_sample.py tests/test_careagents_calm_hub_catalog.py -q
uv run python -m pytest tests/ -q -k careagents
```

Expected: all pass. If a nested test-client request fails in `test_a_tap_during_seeding_mints_no_second_tenant`, stop and report NEEDS_CONTEXT.

- [ ] **Step 11: Commit**

```bash
git add careagents/models.py careagents/accounts.py careagents/connectors.py careagents/app.py tests/test_careagents.py tests/test_careagents_calm_hub_sample.py tests/test_careagents_calm_hub_catalog.py
git commit -s \
    -m "Open the existing sample on a second tap, and say plainly what each source can do" \
    -m "One sample per account: a lease on the account row stops a double tap minting a second tenant. Catalog items gain a group and an Available or Coming soon chip; operator reasons go to the log." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 12: Mutation evidence for the dedupe (M1, M2)**

Run after the commit, so the restore returns to the work.

```bash
mkdir -p docs/evidence
cat > /tmp/calmhub-mutate-t1.py <<'PY'
import pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
src = p.read_text()
n = src.count(old)
assert n == 1, f"expected 1 occurrence, found {n}"
p.write_text(src.replace(old, new))
PY
export PYTHONDONTWRITEBYTECODE=1
LOG=docs/evidence/2026-09-26-careagents-calm-hub-mutations.txt
echo "== M1: drop the first active_sample early return" >> $LOG
find careagents tests -name __pycache__ -type d -prune -exec rm -rf {} +
uv run python /tmp/calmhub-mutate-t1.py careagents/app.py $'        existing = svc.active_sample(acct.id)\n        if existing:\n            return jsonify(_sample_answer(acct.id, existing["id"], True))\n        if not svc.claim_sample_start(acct.id):' $'        if not svc.claim_sample_start(acct.id):'
uv run python -m pytest tests/test_careagents_calm_hub_sample.py -q 2>&1 | tail -5 >> $LOG
git checkout -- careagents/app.py
echo "== M2: claim_sample_start always wins" >> $LOG
find careagents tests -name __pycache__ -type d -prune -exec rm -rf {} +
uv run python /tmp/calmhub-mutate-t1.py careagents/accounts.py $'            return won == 1\n' $'            return True\n'
uv run python -m pytest tests/test_careagents_calm_hub_sample.py -q 2>&1 | tail -5 >> $LOG
git checkout -- careagents/accounts.py
git status --short careagents/
```

Expected: M1 fails `test_a_second_tap_returns_the_existing_sample`. M2 fails `test_a_tap_during_seeding_mints_no_second_tenant`. `git status` shows no changes under `careagents/`.

- [ ] **Step 13: Commit the evidence**

```bash
git add docs/evidence/2026-09-26-careagents-calm-hub-mutations.txt
git commit -s \
    -m "Record mutation runs for the sample dedupe" \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The first assistant, and straight into chat

**Files:**
- Modify: `careagents/models.py` (class `Account`; `_ensure_columns` block added in Task 1)
- Modify: `careagents/accounts.py` (new methods after `create_agent`, line 470 before Task 1's additions)
- Modify: `careagents/app.py` (`_sample_answer` and `_start_connection` from Task 1; upload flip at line 800; poll flip at line 1020)
- Modify: `careagents/static/home.js` (connector tile success branch, lines 49-51)
- Modify: `careagents/templates/chat.html` (lines 57-90)
- Create: `tests/test_careagents_calm_hub_first_agent.py`

**Interfaces:**
- Consumes: `Account`, `Agent`, `Connection`, `HealthClawClient.record_count`, `AccountService.mark_synced`.
- Produces:
  - `Account.first_agent_at` (nullable `Float`)
  - `AccountService.ensure_first_agent(account_id: str, connection_id: str) -> str | None`
  - `AccountService.activate_connection(tenant_id: str) -> list[str]`
  - `AccountService.agent_for_connection(account_id: str, connection_id: str) -> dict | None`
  - The sample answer gains `agent_id` and `redirect` (`/chat?agent=<id>`) when an assistant reads it.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/models.py careagents/accounts.py careagents/app.py careagents/static/home.js careagents/templates/chat.html
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_first_agent.py`:

```python
"""One assistant, straight into chat (calm hub spec section 5 and 9)."""

from __future__ import annotations

import json

from careagents.healthclaw import HealthClawError
from careagents.models import Account, Agent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _make_account, app, cfg, svc)

INTAKE = "Fill out my intake form for a new doctor"


def _agents(svc, account_id):
    with svc.session() as s:
        return [(a.name, a.persona, a.advisor, a.connection_id)
                for a in s.query(Agent).filter_by(account_id=account_id)]


def _account_id(client):
    with client.session_transaction() as s:
        return s["account_id"]


def test_the_sample_lands_in_chat_with_the_intake_starter_first(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    assert body["redirect"] == f"/chat?agent={body['agent_id']}"
    page = c.get(body["redirect"]).get_data(as_text=True)
    assert page.count('id="starters"') == 1
    first = page.split('class="starter">', 1)[1].split("<", 1)[0]
    assert first == INTAKE


def test_the_first_active_connection_creates_exactly_one_agent(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    c.post("/api/connections/sample")
    assert _agents(svc, _account_id(c)) == [("Juniper", "calm", None, conn)]


def test_the_sample_card_has_a_count_at_connect(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    assert svc.get_connection(_account_id(c), conn)["last_count"] == 100


def test_a_second_connection_does_not_create_another_agent(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")
    direct = c.post("/api/connections/direct",
                    json={"consent": True}).get_json()["id"]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p-1"}}]}
    r = c.post(f"/api/connections/{direct}/upload", data=json.dumps(bundle),
               content_type="application/fhir+json")
    assert r.status_code == 200
    assert len(_agents(svc, _account_id(c))) == 1


def test_repeated_ingest_complete_callbacks_create_one_agent(
        app, svc, monkeypatch):
    """Poll is the ingest-complete path for Fasten and runs every 5s."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    started = c.post("/api/connections/fasten", json={"consent": True})
    tenant = started.get_json()["connect_url"].rsplit("/connect/", 1)[1]
    for _ in range(3):
        assert c.get(f"/api/connections/{tenant}/poll").status_code == 200
    agents = _agents(svc, _account_id(c))
    assert len(agents) == 1
    assert agents[0][3] == started.get_json()["id"]


def test_an_existing_account_with_agents_gets_no_new_one(svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "legacy@example.com")
    conn = svc.add_connection(acct.id, "direct", "ca-legacy", "Uploaded",
                              status="empty")
    svc.set_connection_status("ca-legacy", "active")
    svc.create_agent(acct.id, "Ada", "calm", conn)
    svc.create_agent(acct.id, "Coach", "calm", conn)
    assert svc.activate_connection("ca-legacy") == []
    assert [a[0] for a in _agents(svc, acct.id)] == ["Ada", "Coach"]


def test_a_pending_connection_creates_no_agent_and_burns_no_stamp(
        svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "pending@example.com")
    conn = svc.add_connection(acct.id, "fasten", "ca-p", "Clinic",
                              status="pending")
    assert svc.ensure_first_agent(acct.id, conn) is None
    with svc.session() as s:
        assert s.get(Account, acct.id).first_agent_at is None
    assert len(svc.activate_connection("ca-p")) == 1


def test_the_stamp_is_what_stops_a_racing_second_create(svc, monkeypatch):
    acct = _make_account(svc, monkeypatch, "race@example.com")
    conn = svc.add_connection(acct.id, "direct", "ca-r", "Uploaded")
    with svc.session() as s:
        s.get(Account, acct.id).first_agent_at = 1.0
    assert svc.ensure_first_agent(acct.id, conn) is None
    assert _agents(svc, acct.id) == []


def test_starters_render_once_when_counts_are_unknown(cfg, svc, monkeypatch):
    from careagents.app import create_app

    class _NoCounts(FakeClient):
        def search(self, tenant, resource_type, params=None):
            raise HealthClawError("down", 503)

    a = create_app(config=cfg, client=_NoCounts(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    page = c.get(body["redirect"]).get_data(as_text=True)
    assert page.count('id="starters"') == 1
    assert page.count(INTAKE) == 1
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_first_agent.py -q
```

Expected: FAIL. `redirect`, `ensure_first_agent` and `activate_connection` do not exist, and `chat.html` renders `id="starters"` in two branches.

- [ ] **Step 4: Add the stamp column**

In `careagents/models.py`, under `sample_claim_at`:

```python
    # When this account was given its first assistant (calm hub spec
    # section 5). A compare-and-set target: it makes a repeated or racing
    # ingest-complete callback a no-op. A timestamp, not PHI.
    first_agent_at = Column(Float, nullable=True)
```

In the `ca_accounts` block of `_ensure_columns`, add:

```python
            if "first_agent_at" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_accounts ADD COLUMN first_agent_at FLOAT"))
```

- [ ] **Step 5: Add the service methods**

In `careagents/accounts.py`, after `create_agent`:

```python
    def ensure_first_agent(self, account_id: str,
                           connection_id: str) -> str | None:
        """The account's first assistant, created once (spec section 5).

        Returns the new agent id, or None when nothing was created. The
        connection must be the account's and active. `first_agent_at` is a
        compare-and-set on the account row, so a second callback, or one
        racing this one, updates zero rows. An account that already has
        agents is stamped and left alone.
        """
        with self.session() as s:
            conn = (s.query(Connection)
                    .filter_by(id=connection_id, account_id=account_id,
                               status="active").first())
            if conn is None:
                return None
            won = (s.query(Account)
                   .filter(Account.id == account_id,
                           Account.first_agent_at.is_(None))
                   .update({"first_agent_at": now()},
                           synchronize_session=False))
            if won != 1:
                return None
            if s.query(Agent).filter_by(account_id=account_id).first():
                return None
            a = Agent(account_id=account_id, connection_id=connection_id,
                      name="Juniper", persona="calm", advisor=None)
            s.add(a)
            s.flush()
            return a.id

    def activate_connection(self, tenant_id: str) -> list[str]:
        """Records landed on this tenant: mark it active, and give each
        owning account its first assistant if it has none. Safe to repeat."""
        self.set_connection_status(tenant_id, "active")
        with self.session() as s:
            owners = [(c.account_id, c.id) for c in
                      s.query(Connection).filter_by(tenant_id=tenant_id)]
        made = [self.ensure_first_agent(a, c) for a, c in owners]
        return [x for x in made if x]

    def agent_for_connection(self, account_id: str,
                             connection_id: str) -> dict | None:
        with self.session() as s:
            a = (s.query(Agent)
                 .filter_by(account_id=account_id, connection_id=connection_id)
                 .order_by(Agent.created_at.asc()).first())
            return _agent_dict(a) if a else None
```

- [ ] **Step 6: Wire the sample handler and the ingest-complete flips**

In `careagents/app.py`, replace `_sample_answer` with:

```python
    def _sample_answer(account_id, conn_id, existing):
        """What a sample tap answers: the connection, and the chat to open."""
        out = {"id": conn_id, "status": "active", "existing": existing}
        agent = svc.agent_for_connection(account_id, conn_id)
        if agent:
            out["agent_id"] = agent["id"]
            out["redirect"] = url_for("chat", agent=agent["id"])
        return out
```

In `_start_connection`, replace the two lines starting `if connector_id == "sample":` with:

```python
        if connector_id == "sample":
            # A count for the record card, and the first assistant. Neither
            # blocks the connect: an unknown count renders as no count.
            try:
                svc.mark_synced(cid, hc.record_count(tenant))
            except HealthClawError:
                logger.warning("record count after sample seed failed for %s",
                               cid)
            svc.ensure_first_agent(acct.id, cid)
            return jsonify(_sample_answer(acct.id, cid, False))
```

In `upload_connection`, replace `svc.set_connection_status(conn["tenant_id"], "active")` (line 800) with `svc.activate_connection(conn["tenant_id"])`. In `poll_connection`, replace `svc.set_connection_status(conn_tenant, "active")` (line 1020) with `svc.activate_connection(conn_tenant)`. Leave the chat-route flip (line 1161) alone: that account already has an assistant.

- [ ] **Step 7: Send the browser to chat**

In `careagents/static/home.js`, replace lines 50-51:

```js
      if (res.d.redirect) { location.assign(res.d.redirect); return; }
      if (res.d.connect_url) window.open(res.d.connect_url, "_blank", "noopener");
      location.reload();
```

- [ ] **Step 8: One starter block, intake first**

In `careagents/templates/chat.html`, delete both `<div class="starters" id="starters">…</div>` blocks (lines 70-75 and 84-89). After the `{% endif %}` on line 90, add:

```html
    {% if not past %}
    {# One block for every first visit. Intake comes first: that path ends
       in an approval, which is what the product is for (spec section 5). #}
    <div class="starters" id="starters">
      <button type="button" class="starter">Fill out my intake form for a new doctor</button>
      <button type="button" class="starter">What do my labs say?</button>
      <button type="button" class="starter">Any screenings I'm due for?</button>
      <button type="button" class="starter">What medications am I on?</button>
    </div>
    {% endif %}
```

- [ ] **Step 9: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_first_agent.py -q
uv run python -m pytest tests/ -q -k careagents
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add careagents/models.py careagents/accounts.py careagents/app.py careagents/static/home.js careagents/templates/chat.html tests/test_careagents_calm_hub_first_agent.py
git commit -s \
    -m "Give a new account one assistant and open the chat after the sample connects" \
    -m "The first active connection creates Juniper once, guarded by a compare-and-set on the account row. The sample answer carries the chat redirect, and the intake starter comes first." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Rename, change records and delete an assistant

**Files:**
- Modify: `careagents/accounts.py` (new methods after Task 2's `agent_for_connection`)
- Modify: `careagents/app.py` (new routes after `create_agent`, line 1089 before Task 1)
- Create: `tests/test_careagents_calm_hub_agent_menu.py`

**Interfaces:**
- Consumes: `AuthError`, `Agent`, `Connection`, `Surface`.
- Produces:
  - `AccountService.rename_agent(account_id: str, agent_id: str, name: str) -> bool`
  - `AccountService.move_agent(account_id: str, agent_id: str, connection_id: str) -> None` (raises `AuthError`)
  - `AccountService.delete_agent(account_id: str, agent_id: str) -> bool`
  - `POST /api/agents/<agent_id>/rename` with `{"name"}`: 200 `{"id", "name"}`, 400, or 404.
  - `POST /api/agents/<agent_id>/connection` with `{"connection_id"}`: 200 `{"id", "connection_id"}` or 404.
  - `DELETE /api/agents/<agent_id>`: 200 `{"deleted": true, "id"}` or 404.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/accounts.py careagents/app.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_agent_menu.py`:

```python
"""Rename, Change records and Delete (calm hub spec section 3, 5, 9)."""

from __future__ import annotations

import pytest

from careagents.accounts import AuthError
from careagents.models import Agent, Surface
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _make_account, cfg, svc)


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _person(app, svc, monkeypatch, email):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with c.session_transaction() as s:
        aid = s["account_id"]
    conn = svc.add_connection(aid, "direct", f"ca-{email[:4]}", "Uploaded",
                              status="active")
    agent = svc.create_agent(aid, "Juniper", "calm", conn)
    return c, aid, conn, agent


def _connection_of(svc, agent_id):
    with svc.session() as s:
        return s.get(Agent, agent_id).connection_id


def test_change_records_moves_to_another_active_connection(
        app, svc, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    other = svc.add_connection(aid, "sample", "ca-ann-s", "Sample records")
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": other})
    assert r.status_code == 200
    assert _connection_of(svc, agent) == other


def test_change_records_refuses_another_accounts_connection(
        app, svc, monkeypatch):
    c, _, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    _, _, bobs_conn, _ = _person(app, svc, monkeypatch, "bob@example.com")
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": bobs_conn})
    assert r.status_code == 404
    assert _connection_of(svc, agent) == conn


def test_change_records_refuses_a_revoked_connection(app, svc, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    gone = svc.add_connection(aid, "direct", "ca-gone", "Old", status="active")
    svc.revoke_connection(aid, gone)
    r = c.post(f"/api/agents/{agent}/connection",
               json={"connection_id": gone})
    assert r.status_code == 404
    assert _connection_of(svc, agent) == conn


def test_change_records_refuses_another_accounts_agent(app, svc, monkeypatch):
    c, _, conn, _ = _person(app, svc, monkeypatch, "ann@example.com")
    _, _, _, bobs_agent = _person(app, svc, monkeypatch, "bob@example.com")
    r = c.post(f"/api/agents/{bobs_agent}/connection",
               json={"connection_id": conn})
    assert r.status_code == 404


def test_move_agent_checks_ownership_in_the_service(svc, monkeypatch):
    """MUTATION M3: drop account_id from move_agent's connection lookup
    -> red."""
    ann = _make_account(svc, monkeypatch, "ann2@example.com")
    bob = _make_account(svc, monkeypatch, "bob2@example.com")
    mine = svc.add_connection(ann.id, "direct", "ca-a2", "Mine",
                              status="active")
    theirs = svc.add_connection(bob.id, "direct", "ca-b2", "Theirs",
                                status="active")
    agent = svc.create_agent(ann.id, "Juniper", "calm", mine)
    with pytest.raises(AuthError):
        svc.move_agent(ann.id, agent, theirs)
    assert _connection_of(svc, agent) == mine


def test_rename_is_owner_only_and_refuses_a_blank_name(app, svc, monkeypatch):
    c, _, _, agent = _person(app, svc, monkeypatch, "ann@example.com")
    bob, _, _, _ = _person(app, svc, monkeypatch, "bob@example.com")
    assert c.post(f"/api/agents/{agent}/rename",
                  json={"name": "  "}).status_code == 400
    assert bob.post(f"/api/agents/{agent}/rename",
                    json={"name": "Mine now"}).status_code == 404
    r = c.post(f"/api/agents/{agent}/rename", json={"name": "Ada"})
    assert r.status_code == 200 and r.get_json()["name"] == "Ada"
    with svc.session() as s:
        assert s.get(Agent, agent).name == "Ada"


def test_delete_removes_the_agent_only(app, svc, fake, monkeypatch):
    c, aid, conn, agent = _person(app, svc, monkeypatch, "ann@example.com")
    keep = svc.create_agent(aid, "Coach", "calm", conn)
    svc.add_surface(aid, agent, "imessage", "code-1")
    bob, _, _, _ = _person(app, svc, monkeypatch, "bob@example.com")
    assert bob.delete(f"/api/agents/{agent}").status_code == 404
    assert c.delete(f"/api/agents/{agent}").status_code == 200
    with svc.session() as s:
        assert s.get(Agent, agent) is None
        assert s.get(Agent, keep) is not None
        assert s.query(Surface).filter_by(agent_id=agent).count() == 0
    # The conversation stays in HealthClaw with the connection.
    assert fake.purged == []
    assert svc.get_connection(aid, conn) is not None
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_agent_menu.py -q
```

Expected: FAIL with 404 or 405 on the new routes, and `AttributeError` for `move_agent`.

- [ ] **Step 4: Add the service methods**

In `careagents/accounts.py`, after `agent_for_connection`:

```python
    def rename_agent(self, account_id: str, agent_id: str, name: str) -> bool:
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                return False
            a.name = name[:48]
            return True

    def move_agent(self, account_id: str, agent_id: str,
                   connection_id: str) -> None:
        """Point an agent at another of the account's ACTIVE connections.

        Raises AuthError for a foreign agent, a foreign connection, or one
        that is not active: the ownership rule create_agent applies, plus
        the revoked check, since a revoked connection is not a pathway to
        a tenant (#215).
        """
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                raise AuthError("That assistant isn't yours.")
            c = (s.query(Connection)
                 .filter_by(id=connection_id, account_id=account_id,
                            status="active").first())
            if c is None:
                raise AuthError("Those records aren't available.")
            a.connection_id = c.id

    def delete_agent(self, account_id: str, agent_id: str) -> bool:
        """Remove the agent and its surfaces. Its conversation stays in
        HealthClaw until the connection is deleted (spec section 5)."""
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                return False
            s.query(Surface).filter_by(agent_id=agent_id).delete()
            s.delete(a)
            return True
```

- [ ] **Step 5: Add the routes**

In `careagents/app.py`, after the `create_agent` route:

```python
    @app.post("/api/agents/<agent_id>/rename")
    @login_required
    def rename_agent(agent_id):
        acct = current_account()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()[:48]
        if not name:
            return jsonify({"error": "Give your assistant a name."}), 400
        if not svc.rename_agent(acct.id, agent_id, name):
            return jsonify({"error": "unknown agent"}), 404
        return jsonify({"id": agent_id, "name": name})

    @app.post("/api/agents/<agent_id>/connection")
    @login_required
    def move_agent(agent_id):
        """Change records. The service does the ownership check, so there
        is one place to get it right and one place to mutation-test."""
        acct = current_account()
        conn_id = str((request.get_json(silent=True) or {})
                      .get("connection_id") or "")
        try:
            svc.move_agent(acct.id, agent_id, conn_id)
        except AuthError:
            return jsonify({"error": "unknown agent or connection"}), 404
        return jsonify({"id": agent_id, "connection_id": conn_id})

    @app.delete("/api/agents/<agent_id>")
    @login_required
    def delete_agent(agent_id):
        acct = current_account()
        if not svc.delete_agent(acct.id, agent_id):
            return jsonify({"error": "unknown agent"}), 404
        return jsonify({"deleted": True, "id": agent_id})
```

- [ ] **Step 6: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_agent_menu.py -q
uv run python -m pytest tests/ -q -k careagents
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add careagents/accounts.py careagents/app.py tests/test_careagents_calm_hub_agent_menu.py
git commit -s \
    -m "Let a person rename, move or delete their assistant, and only their own" \
    -m "Change records accepts only the account's own active connections. Delete removes the assistant and its surfaces and leaves the conversation in HealthClaw." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Mutation evidence for the ownership check (M3)**

```bash
export PYTHONDONTWRITEBYTECODE=1
LOG=docs/evidence/2026-09-26-careagents-calm-hub-mutations.txt
echo "== M3: move_agent ignores the connection's owner" >> $LOG
find careagents tests -name __pycache__ -type d -prune -exec rm -rf {} +
uv run python /tmp/calmhub-mutate-t1.py careagents/accounts.py $'                 .filter_by(id=connection_id, account_id=account_id,\n                            status="active").first())\n            if c is None:\n                raise AuthError("Those records' $'                 .filter_by(id=connection_id,\n                            status="active").first())\n            if c is None:\n                raise AuthError("Those records'
uv run python -m pytest tests/test_careagents_calm_hub_agent_menu.py -q 2>&1 | tail -6 >> $LOG
git checkout -- careagents/accounts.py
git status --short careagents/
git add $LOG
git commit -s \
    -m "Record the mutation run for the change-records ownership check" \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Expected: M3 fails `test_change_records_refuses_another_accounts_connection` and `test_move_agent_checks_ownership_in_the_service`. If `/tmp/calmhub-mutate-t1.py` is gone, recreate it from Task 1 Step 12.

---

### Task 4: The settings page

**Files:**
- Create: `careagents/templates/settings.html`
- Create: `careagents/templates/_delete_modal.html`
- Modify: `careagents/templates/home.html` (header lines 8-16; grants lines 79-95; surfaces and leaving lines 127-157; code card lines 247-259; delete modal lines 261-279)
- Modify: `careagents/app.py` (`home`, lines 379-399; new `settings` route after it)
- Modify: `careagents/accounts.py` (new `list_passkeys` after `has_passkey`, line 274)
- Modify: `careagents/static/home.js` (agent modal lines 601-622; iMessage handler lines 643-657)
- Modify: `careagents/static/careagents.css` (append)
- Modify: `tests/test_careagents.py` (three pins listed in Step 9)
- Modify: `tests/test_careagents_consent.py` (one pin, Step 9)
- Create: `tests/test_careagents_calm_hub_settings.py`

**Interfaces:**
- Consumes: `svc.list_home`, `svc.list_grants`, `_grants_with_labels`, `cfg.imessage_handle`.
- Produces:
  - `AccountService.list_passkeys(account_id: str) -> list[dict]` with keys `id`, `name`, `created_at`.
  - `GET /settings` rendering `settings.html`.
  - `home.html` receives `has_grants: bool` in place of `grants`, `has_passkey`, `telegram_bot` and `imessage_handle`.
  - `#im-surface` carries `data-agent="<first agent id>"`, which `home.js` reads.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/templates/home.html careagents/templates/settings.html careagents/templates/_delete_modal.html careagents/app.py careagents/accounts.py careagents/static/home.js careagents/static/careagents.css tests/test_careagents.py tests/test_careagents_consent.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_settings.py`:

```python
"""The settings page (calm hub spec section 6)."""

from __future__ import annotations

from careagents.models import Passkey
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    with c.session_transaction() as s:
        return c, s["account_id"]


def test_settings_lists_passkeys_and_offers_to_add_one(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    with svc.session() as s:
        s.add(Passkey(account_id=aid, credential_id=b"cred-1",
                      public_key=b"pk", name="Kitchen iPad"))
    page = c.get("/settings").get_data(as_text=True)
    assert "Kitchen iPad" in page
    assert 'href="/auth?enroll=1"' in page


def test_settings_holds_surfaces_grants_sign_out_and_delete(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    tenant = svc.get_connection(aid, conn["id"])["tenant_id"]
    gid = svc.add_grant(aid, conn["id"], tenant, "cid-claude", "Claude",
                        "fhir.read", "consent_set")
    page = c.get("/settings").get_data(as_text=True)
    assert "Where you can reach your assistant" in page
    assert f'id="im-surface" data-agent="{conn["agent_id"]}"' in page
    assert "Apps you have shared records with" in page
    assert f'data-grant="{gid}"' in page
    assert 'action="/logout"' in page
    assert 'id="account-delete"' in page
    assert 'id="delete-modal"' in page
    assert "home.js" in page


def test_the_hub_no_longer_carries_what_moved(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    tenant = svc.get_connection(aid, conn["id"])["tenant_id"]
    svc.add_grant(aid, conn["id"], tenant, "cid-claude", "Claude",
                  "fhir.read", "consent_hub")
    hub = c.get("/home").get_data(as_text=True)
    for gone in ('id="account-delete"', 'id="im-surface"',
                 'class="hub-card grant-card"', 'action="/logout"',
                 'id="code-card"'):
        assert gone not in hub, gone
    assert 'href="/settings"' in hub
    assert 'href="/settings#grants-section"' in hub
    assert 'id="delete-modal"' in hub


def test_the_grants_link_is_absent_without_grants(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    assert "#grants-section" not in c.get("/home").get_data(as_text=True)


def test_settings_needs_a_session(app):
    r = app.test_client().get("/settings")
    assert r.status_code == 302 and "/auth" in r.headers["Location"]
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_settings.py -q
```

Expected: FAIL with 404 on `/settings`.

- [ ] **Step 4: Add `list_passkeys`**

In `careagents/accounts.py`, after `has_passkey`:

```python
    def list_passkeys(self, account_id: str) -> list[dict]:
        with self.session() as s:
            rows = (s.query(Passkey).filter_by(account_id=account_id)
                    .order_by(Passkey.created_at.asc()).all())
            return [{"id": p.id, "name": p.name, "created_at": p.created_at}
                    for p in rows]
```

- [ ] **Step 5: Move the delete dialog into a partial**

Create `careagents/templates/_delete_modal.html` with the exact markup of `home.html` lines 261-279 (the comment and the `#delete-modal` div). In `home.html`, replace those lines with `{% include "_delete_modal.html" %}`.

- [ ] **Step 6: Create the settings page**

Create `careagents/templates/settings.html`:

```html
{% extends "base.html" %}
{% block title %}CareAgents settings{% endblock %}
{% block bodyclass %}page-home{% endblock %}
{% block content %}
<main class="hub">
  <div class="hub-head">
    <div>
      <h1>Settings</h1>
      <p class="hub-sub">{{ me.email }}</p>
    </div>
    <a class="pill" href="/home">Back to your hub</a>
  </div>

  <section class="hub-section" id="passkeys-section">
    <div class="section-head"><h2>Passkeys</h2></div>
    {% if passkeys %}
    <ul class="settings-list">
      {% for p in passkeys %}<li>{{ p.name }}</li>{% endfor %}
    </ul>
    {% else %}
    <p class="empty">No passkey yet. Add one to sign in with your face or
      fingerprint.</p>
    {% endif %}
    <a class="pill" href="/auth?enroll=1">Add a passkey</a>
  </section>

  <section class="hub-section">
    <div class="section-head"><h2>Where you can reach your assistant</h2></div>
    <div class="surface-row">
      <div class="surface on"><b>Web</b><span>always on</span></div>
      {# Not serviced in the beta (#536, D6): no webhook, no poller. No id, so
         home.js has nothing to bind. The tile is a label, not a control. #}
      <div class="surface soon"><b>Telegram</b><span>coming soon</span></div>
      {% if imessage_handle %}
      <div class="surface" id="im-surface" data-agent="{{ first_agent }}"><b>iMessage</b>
        <span id="im-state">connect →</span></div>
      {% else %}
      <div class="surface soon"><b>iMessage</b><span>coming soon</span></div>
      {% endif %}
      <div class="surface soon"><b>Phone app</b><span>install from browser</span></div>
    </div>
    <p class="inline-msg" id="surfaces-msg" role="status" aria-live="polite"
       hidden></p>
  </section>

  <section class="hub-section" id="grants-section">
    <div class="section-head"><h2>Apps you have shared records with</h2></div>
    {% if grants %}
    <div class="card-grid" id="grants">
      {% for g in grants %}
      <div class="hub-card grant-card" data-grant="{{ g.id }}">
        <div class="hub-card-name">{{ g.client_name }}</div>
        <div class="hub-card-sub">reads {{ g.tenant_label or 'one connection' }} through HealthClaw</div>
        <span class="status status-{{ g.status }}">{{ g.status }}</span>
        {% if g.status != 'revoked' %}
        <button type="button" class="grant-revoke" data-grant="{{ g.id }}"
                title="Stop this app reading your records">Revoke</button>
        {% endif %}
        <span class="grant-msg" role="status" aria-live="polite" hidden></span>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <p class="empty">You haven't shared your records with any app.</p>
    {% endif %}
  </section>

  <section class="leaving">
    <h2>Sign out or leave</h2>
    <form method="post" action="/logout"><button class="pill">Sign out</button></form>
    <p>Deleting your account deletes the records behind every connection
      first, then your email, passkey and consents. The PHI-free audit trail
      stays as the record of who accessed what.</p>
    <button class="pill danger" id="account-delete" type="button">Delete my account</button>
    <p class="inline-msg" id="account-msg" role="status" aria-live="polite" hidden></p>
  </section>
</main>

<!-- Pairing code card. What is shown in #pair-code is byte-identical to what
     Copy writes, so the press-and-hold fallback can't copy the wrong string. -->
<div class="modal" id="code-card" role="dialog" aria-modal="true"
     aria-labelledby="code-title" hidden>
  <div class="modal-card">
    <h3 id="code-title">Your pairing code</h3>
    <p id="code-instructions"></p>
    <div id="pair-code"></div>
    <button type="button" class="btn-secondary btn-block" id="copy-code">
      <span id="copy-state">Copy</span></button>
    <button type="button" class="btn-primary btn-block" id="code-done">Done</button>
  </div>
</div>

{% include "_delete_modal.html" %}

{# The hub's script: its dialog, announce and delete primitives are shared,
   and every handler is guarded by the element it binds. #}
<script src="{{ url_for('static', filename='home.js') }}"></script>
{% endblock %}
```

- [ ] **Step 7: Trim the hub and add the route**

In `careagents/templates/home.html`:

1. Replace the header (lines 8-16) with:

```html
  <div class="hub-head">
    <div>
      <h1>Welcome back</h1>
      <p class="hub-sub">{{ me.email }}</p>
    </div>
    <a class="pill" href="/settings">Settings</a>
  </div>
```

2. Replace the grants block (lines 79-95) with:

```html
    {% if has_grants %}
    <p class="hub-link"><a href="/settings#grants-section">Apps you've shared records with</a></p>
    {% endif %}
```

3. Delete the `<!-- SURFACES -->` section and the `<section class="leaving">` block (lines 127-157).
4. Delete the pairing code card (lines 247-259).

In `careagents/app.py`, replace the `render_template` call in `home()` (lines 388-399) with:

```python
        return render_template(
            "home.html", me=acct, personas=PERSONAS,
            connections=data["connections"], agents=data["agents"],
            has_grants=bool(svc.list_grants(acct.id)),
            terms_url=f"{cfg.healthclaw_public_base}/terms",
            privacy_url=f"{cfg.healthclaw_public_base}/privacy",
            advisors=advisors.catalog(),
            catalog=connectors.catalog(
                cfg, real_records=cfg.real_records_open_for(acct.email)))

    @app.get("/settings")
    @login_required
    def settings():
        acct = current_account()
        data = svc.list_home(acct.id)
        return render_template(
            "settings.html", me=acct,
            passkeys=svc.list_passkeys(acct.id),
            grants=_grants_with_labels(svc.list_grants(acct.id),
                                       data["connections"]),
            first_agent=(data["agents"][0]["id"] if data["agents"] else ""),
            imessage_handle=cfg.imessage_handle)
```

- [ ] **Step 8: Guard `home.js` for the second page**

In `careagents/static/home.js`, wrap lines 601-622 (from `$("new-agent-btn").addEventListener` through the end of the `create-agent` listener) in `if (modal) {` … `}`. Re-indent the wrapped lines by two spaces. Task 5D deletes this block.

Replace the iMessage handler (lines 643-657) with:

```js
  // --- iMessage surface (settings page) ---
  // The page names the assistant to bind on the tile itself: settings has
  // no assistant cards to read one from.
  const im = $("im-surface");
  if (im) im.addEventListener("click", async () => {
    $("surfaces-msg").hidden = true;
    const agentId = im.dataset.agent;
    if (!agentId) {
      return say(im, $("surfaces-msg"),
        "Start a chat with your assistant first, then connect iMessage.");
    }
    const res = await post("/api/surfaces/imessage", { agent_id: agentId });
    if (!res.ok) return say(im, $("surfaces-msg"), res.d.error || "Failed");
    $("im-state").textContent = "pending — text to finish";
    // iMessage needs the whole "care <code>" line as the text body.
    showCodeCard("care " + res.d.code, res.d.instructions || "Text this code to connect:");
  });
```

Append to `careagents/static/careagents.css`:

```css
/* --- settings (calm hub, 2026-09-26) ------------------------------------- */
.settings-list { margin: 0 0 12px; padding-left: 20px; }
.settings-list li { margin: 4px 0; }
.hub-link { margin: 12px 0 0; }
```

- [ ] **Step 9: Update the existing pins that read the moved blocks from the hub**

In `tests/test_careagents.py`:

1. `test_the_hub_links_to_a_reachable_enrolment_page` (line 2828): change `c.get("/home")` to `c.get("/settings")`.
2. `test_the_telegram_surface_is_coming_soon_for_the_beta` (line 5982): change `c.get("/home")` to `c.get("/settings")`.
3. `test_hub_dialog_selector_contract` (line 5360): after `_CSS = …` (line 5233) add the two lines below. In the test body, change `assert sel in _HOME_HTML, sel` to `assert sel in _HUB_PAGES, sel`.

```python
_SETTINGS_HTML = (_CA / "templates" / "settings.html").read_text()
_HUB_PAGES = (_HOME_HTML + _SETTINGS_HTML
              + (_CA / "templates" / "_delete_modal.html").read_text())
```

In `tests/test_careagents_consent.py`, `test_the_hub_lists_grants_and_revokes_at_healthclaw_first` (lines 350-351), replace:

```python
    page = client.get("/settings").get_data(as_text=True)
    assert "Apps you have shared records with" in page and "Claude" in page
```

- [ ] **Step 10: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_settings.py -q
uv run python -m pytest tests/ -q -k "careagents or browser_js"
```

Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add careagents/templates/settings.html careagents/templates/_delete_modal.html careagents/templates/home.html careagents/app.py careagents/accounts.py careagents/static/home.js careagents/static/careagents.css tests/test_careagents.py tests/test_careagents_consent.py tests/test_careagents_calm_hub_settings.py
git commit -s \
    -m "Move passkeys, surfaces, sharing and account deletion to a settings page" \
    -m "The hub keeps a Settings link, and a link to shared apps when any exist. The settings page reuses home.js; the iMessage tile names its assistant itself." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5A: The hub's words, as pure functions

**Files:**
- Create: `careagents/hub.py`
- Create: `tests/test_careagents_calm_hub_view.py`

**Interfaces:**
- Consumes: the dict `AccountService.list_home` returns.
- Produces:
  - `hub.STATUS_WORDS: dict[str, str]`
  - `hub.status_word(status: str | None) -> str`
  - `hub.updated_line(ts: float | None, now: float) -> str`
  - `hub.count_line(n: int | None) -> str`
  - `hub.build(home: dict, now: float) -> dict` with keys `agents`, `records`, `active_records`, `move_choices`, `past`, `connected_kinds`, `has_real`.
  - `hub.switch_prompt(home: dict) -> dict | None` with keys `agent_id`, `agent_name`, `connection_id`, `label`.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/hub.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_view.py`:

```python
"""The hub's words (calm hub spec section 3 and 7)."""

from __future__ import annotations

from careagents import hub

DAY = 86400.0
NOW = 1_800_000_000.0


def _conn(cid, kind="direct", status="active", **kw):
    return {"id": cid, "kind": kind, "status": status, "label": f"L-{cid}",
            "tenant_id": f"ca-{cid}", "provider": None, "connected_at": NOW,
            "last_synced_at": None, "last_count": None,
            "last_uncounted": None, **kw}


def _agent(aid, conn, name="Juniper"):
    return {"id": aid, "name": name, "persona": "calm", "advisor": None,
            "connection_id": conn}


def test_every_status_word_is_plain():
    assert hub.status_word("active") == "Connected"
    assert hub.status_word("pending") == "Connecting…"
    assert hub.status_word("empty") == "No records yet"
    assert hub.status_word("revoked") == ""
    assert hub.status_word(None) == ""


def test_updated_line_counts_whole_days_and_never_says_zero_days():
    assert hub.updated_line(None, NOW) == ""
    assert hub.updated_line(NOW - 60, NOW) == "Updated today"
    assert hub.updated_line(NOW + 60, NOW) == "Updated today"
    assert hub.updated_line(NOW - DAY - 1, NOW) == "Updated yesterday"
    assert hub.updated_line(NOW - 3 * DAY - 1, NOW) == "Updated 3 days ago"


def test_an_unknown_count_is_no_count_never_zero():
    assert hub.count_line(None) == ""
    assert hub.count_line(0) == "0 records"
    assert hub.count_line(1) == "1 record"
    assert hub.count_line(52) == "52 records"


def test_build_splits_live_from_past_and_names_what_each_agent_reads():
    home = {"connections": [_conn("a"), _conn("s", kind="sample"),
                            _conn("p", status="pending"),
                            _conn("r", status="revoked")],
            "agents": [_agent("g1", "s"), _agent("g2", "gone")],
            "surfaces": []}
    view = hub.build(home, NOW)
    assert [r["id"] for r in view["records"]] == ["a", "s", "p"]
    assert [r["id"] for r in view["past"]] == ["r"]
    assert [r["id"] for r in view["active_records"]] == ["a", "s"]
    assert view["move_choices"] == [{"id": "a", "label": "L-a"},
                                    {"id": "s", "label": "L-s"}]
    assert view["agents"][0]["reads"] == "L-s"
    assert view["agents"][1]["reads"] == ""
    assert view["connected_kinds"] == ["direct", "sample"]
    assert view["has_real"] is True
    sample = next(r for r in view["records"] if r["id"] == "s")
    assert sample["is_sample"] is True and sample["updated"] == "Updated today"


def test_the_switch_prompt_needs_an_agent_on_the_sample_and_a_real_source():
    sample, real = _conn("s", kind="sample"), _conn("f", kind="fasten")
    only_sample = {"connections": [sample], "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(only_sample) is None
    both = {"connections": [sample, real], "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(both) == {"agent_id": "g", "agent_name": "Juniper",
                                       "connection_id": "f", "label": "L-f"}
    on_real = {"connections": [sample, real], "agents": [_agent("g", "f")]}
    assert hub.switch_prompt(on_real) is None
    pending = {"connections": [sample, _conn("f", "fasten", "pending")],
               "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(pending) is None
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_view.py -q
```

Expected: FAIL with `ImportError: cannot import name 'hub'`.

- [ ] **Step 4: Write the module**

Create `careagents/hub.py`:

```python
"""The hub's view of an account: the words a person reads on /home.

Pure functions over `AccountService.list_home` output. No network and no
PHI: labels, statuses, counts and timestamps only (calm hub spec 3 and 7).
"""

from __future__ import annotations

#: The one place a stored status becomes a word a person reads. `revoked`
#: has no word: a revoked connection appears only under Past connections,
#: whose heading already says what it is.
STATUS_WORDS = {"active": "Connected", "pending": "Connecting…",
                "empty": "No records yet"}

_DAY = 86400


def status_word(status: str | None) -> str:
    return STATUS_WORDS.get(status or "", "")


def updated_line(ts: float | None, now: float) -> str:
    if ts is None:
        return ""
    days = int(max(0.0, now - ts) // _DAY)
    if days == 0:
        return "Updated today"
    if days == 1:
        return "Updated yesterday"
    return f"Updated {days} days ago"


def count_line(n: int | None) -> str:
    # None is "not counted", which is never the same as zero (#403).
    if n is None:
        return ""
    return "1 record" if n == 1 else f"{n} records"


def _record(c: dict, now: float) -> dict:
    return {**c,
            "status_word": status_word(c.get("status")),
            "count_line": count_line(c.get("last_count")),
            "updated": updated_line(
                c.get("last_synced_at") or c.get("connected_at"), now),
            "is_sample": c.get("kind") == "sample"}


def build(home: dict, now: float) -> dict:
    conns = home["connections"]
    by_id = {c["id"]: c for c in conns}
    records = [_record(c, now) for c in conns if c["status"] != "revoked"]
    active = [r for r in records if r["status"] == "active"]
    return {
        "agents": [{**a, "reads": (by_id.get(a["connection_id"]) or {})
                    .get("label", "")} for a in home["agents"]],
        "records": records,
        "active_records": active,
        "move_choices": [{"id": r["id"], "label": r["label"]} for r in active],
        "past": [_record(c, now) for c in conns if c["status"] == "revoked"],
        "connected_kinds": sorted({r["kind"] for r in active}),
        "has_real": any(not r["is_sample"] for r in active),
    }


def switch_prompt(home: dict) -> dict | None:
    """The one "Switch Juniper to your records?" question, when it applies:
    an assistant reads the sample and a real connection is active."""
    by_id = {c["id"]: c for c in home["connections"]}
    real = [c for c in home["connections"]
            if c["kind"] != "sample" and c["status"] == "active"]
    if not real:
        return None
    for a in home["agents"]:
        c = by_id.get(a["connection_id"])
        if c and c["kind"] == "sample":
            return {"agent_id": a["id"], "agent_name": a["name"],
                    "connection_id": real[0]["id"], "label": real[0]["label"]}
    return None
```

- [ ] **Step 5: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_view.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add careagents/hub.py tests/test_careagents_calm_hub_view.py
git commit -s \
    -m "Add the hub's plain words as pure functions" \
    -m "Status words, record counts that never turn unknown into zero, Updated N days ago, and the one switch-to-your-records question." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5B: The pending-approval count

**Files:**
- Modify: `careagents/app.py` (new route after `approvals`, line 1522 before Task 1)
- Create: `tests/test_careagents_calm_hub_waiting.py`

**Interfaces:**
- Consumes: `hc.pending_actions(tenant) -> list[dict]` (raises `HealthClawError`), `svc.list_home`.
- Produces: `GET /api/approvals/count` answering 200 `{"count": int, "agent_id"?: str, "href"?: str}`, or 503 `{"error": "unavailable"}` with no `count` key.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/app.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_waiting.py`:

```python
"""Waiting for you (calm hub spec section 3 and 9)."""

from __future__ import annotations

import pytest

from careagents.healthclaw import HealthClawError
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def test_the_count_matches_the_approvals_page(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    r = c.get("/api/approvals/count")
    assert r.status_code == 200
    page = c.get(f"/agents/{agent}/approvals").get_data(as_text=True)
    assert r.get_json()["count"] == page.count(f'href="/review/{agent}/')
    assert r.get_json()["href"] == f"/agents/{agent}/approvals"


def test_two_agents_on_one_connection_count_its_requests_once(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    body = c.post("/api/connections/sample").get_json()
    c.post("/api/agents", json={"name": "Coach", "connection_id": body["id"]})
    assert c.get("/api/approvals/count").get_json()["count"] == 1


def test_no_assistant_is_an_honest_zero(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert c.get("/api/approvals/count").get_json() == {"count": 0}


def test_a_revoked_connection_is_not_counted(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    c.post(f"/api/connections/{conn}/disconnect")
    assert c.get("/api/approvals/count").get_json() == {"count": 0}


def test_a_failed_count_is_503_with_no_count(app, svc, fake, monkeypatch):
    """An unanswered question is not an empty inbox (#215)."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")

    def _down(tenant):
        raise HealthClawError("down", 503)
    monkeypatch.setattr(fake, "pending_actions", _down)

    r = c.get("/api/approvals/count")
    assert r.status_code == 503
    assert "count" not in r.get_json()
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_waiting.py -q
```

Expected: FAIL with 404 on `/api/approvals/count`.

- [ ] **Step 4: Add the route**

In `careagents/app.py`, after the `approvals` route:

```python
    @app.get("/api/approvals/count")
    @login_required
    def approvals_count():
        """How many requests wait for this person, for the hub's band.

        The same tenants the approvals page reads: each assistant's live
        connection, counted once. Any tenant that cannot be asked fails the
        whole count. A partial sum would read as a smaller inbox, and a
        failure must never render as zero (calm hub spec section 3).
        """
        acct = current_account()
        data = svc.list_home(acct.id)
        live = {c["id"]: c for c in data["connections"]
                if c["status"] != "revoked"}
        tenants: dict[str, str] = {}
        for a in data["agents"]:
            conn = live.get(a["connection_id"])
            if conn and conn["tenant_id"] not in tenants:
                tenants[conn["tenant_id"]] = a["id"]
        total, first = 0, None
        for tenant, agent_id in tenants.items():
            try:
                n = len(hc.pending_actions(tenant))
            except HealthClawError:
                logger.warning("pending count unavailable for account %s",
                               acct.id)
                return jsonify({"error": "unavailable"}), 503
            total += n
            if n and first is None:
                first = agent_id
        out = {"count": total}
        if first:
            out["agent_id"] = first
            out["href"] = url_for("approvals", agent_id=first)
        return jsonify(out)
```

- [ ] **Step 5: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_waiting.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add careagents/app.py tests/test_careagents_calm_hub_waiting.py
git commit -s \
    -m "Count the requests waiting for a person, and fail rather than say zero" \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5C: Answering "Switch Juniper to your records?"

**Files:**
- Modify: `careagents/models.py` (class `Account`; `ca_accounts` block of `_ensure_columns`)
- Modify: `careagents/accounts.py` (new methods after Task 3's `delete_agent`)
- Modify: `careagents/app.py` (new route after Task 3's routes)
- Create: `tests/test_careagents_calm_hub_switch.py`

**Interfaces:**
- Consumes: `AccountService.move_agent` (Task 3).
- Produces:
  - `Account.switch_prompted_at` (nullable `Float`)
  - `AccountService.switch_prompted_at(account_id: str) -> float | None`
  - `AccountService.stamp_switch_prompt(account_id: str) -> None`
  - `POST /api/hub/switch-prompt` with `{"answer": "switch" | "later", "agent_id", "connection_id"}`: 200 `{"answer"}`, 400, or 404.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/models.py careagents/accounts.py careagents/app.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_careagents_calm_hub_switch.py`:

```python
"""The hub asks once (calm hub spec section 5)."""

from __future__ import annotations

from careagents.models import Agent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)


def _setup(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    with c.session_transaction() as s:
        aid = s["account_id"]
    sample = c.post("/api/connections/sample").get_json()
    real = svc.add_connection(aid, "fasten", "ca-real", "Clinic",
                              status="active")
    return c, aid, sample["agent_id"], real


def test_switch_moves_the_agent_and_is_asked_once(app, svc, monkeypatch):
    c, aid, agent, real = _setup(app, svc, monkeypatch)
    assert svc.switch_prompted_at(aid) is None
    r = c.post("/api/hub/switch-prompt", json={
        "answer": "switch", "agent_id": agent, "connection_id": real})
    assert r.status_code == 200
    with svc.session() as s:
        assert s.get(Agent, agent).connection_id == real
    assert svc.switch_prompted_at(aid) is not None


def test_later_keeps_the_agent_and_stops_asking(app, svc, monkeypatch):
    c, aid, agent, real = _setup(app, svc, monkeypatch)
    before = svc.get_agent_context(aid, agent)["connection"]["id"]
    r = c.post("/api/hub/switch-prompt", json={"answer": "later"})
    assert r.status_code == 200
    assert svc.get_agent_context(aid, agent)["connection"]["id"] == before
    assert svc.switch_prompted_at(aid) is not None


def test_switch_uses_the_same_ownership_rule(app, svc, monkeypatch):
    c, aid, agent, _ = _setup(app, svc, monkeypatch)
    r = c.post("/api/hub/switch-prompt", json={
        "answer": "switch", "agent_id": agent, "connection_id": "conn_nope"})
    assert r.status_code == 404
    assert svc.switch_prompted_at(aid) is None


def test_an_unknown_answer_is_refused(app, svc, monkeypatch):
    c, aid, _, _ = _setup(app, svc, monkeypatch)
    assert c.post("/api/hub/switch-prompt",
                  json={"answer": "maybe"}).status_code == 400
    assert svc.switch_prompted_at(aid) is None
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_switch.py -q
```

Expected: FAIL with `AttributeError: 'AccountService' object has no attribute 'switch_prompted_at'`.

- [ ] **Step 4: Add the column and service methods**

In `careagents/models.py`, under `first_agent_at`:

```python
    # When the person answered "Switch Juniper to your records?" either
    # way (calm hub spec section 5). A timestamp, not PHI.
    switch_prompted_at = Column(Float, nullable=True)
```

In the `ca_accounts` block of `_ensure_columns`:

```python
            if "switch_prompted_at" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_accounts ADD COLUMN switch_prompted_at "
                    "FLOAT"))
```

In `careagents/accounts.py`, after `delete_agent`:

```python
    def switch_prompted_at(self, account_id: str) -> float | None:
        with self.session() as s:
            acct = s.get(Account, account_id)
            return acct.switch_prompted_at if acct else None

    def stamp_switch_prompt(self, account_id: str) -> None:
        with self.session() as s:
            acct = s.get(Account, account_id)
            if acct is not None and acct.switch_prompted_at is None:
                acct.switch_prompted_at = now()
```

- [ ] **Step 5: Add the route**

In `careagents/app.py`, after the `delete_agent` route:

```python
    @app.post("/api/hub/switch-prompt")
    @login_required
    def answer_switch_prompt():
        """Either answer ends the question; "switch" also moves the agent,
        through the same ownership check as Change records."""
        acct = current_account()
        body = request.get_json(silent=True) or {}
        answer = body.get("answer")
        if answer == "switch":
            try:
                svc.move_agent(acct.id, str(body.get("agent_id") or ""),
                               str(body.get("connection_id") or ""))
            except AuthError:
                return jsonify({"error": "unknown agent or connection"}), 404
        elif answer != "later":
            return jsonify({"error": "answer must be switch or later"}), 400
        svc.stamp_switch_prompt(acct.id)
        return jsonify({"answer": answer})
```

- [ ] **Step 6: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_switch.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add careagents/models.py careagents/accounts.py careagents/app.py tests/test_careagents_calm_hub_switch.py
git commit -s \
    -m "Record the answer to switch-to-your-records, and move the assistant on yes" \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5D: The hub, top to bottom

**Files:**
- Modify: `careagents/templates/home.html` (rewritten whole; content below)
- Modify: `careagents/app.py` (`home`, as changed in Task 4)
- Modify: `careagents/static/home.js` (header comment lines 1-2; connector handler lines 14-53; agent modal block from Task 4; new handlers)
- Modify: `careagents/static/careagents.css` (append)
- Modify: `tests/test_careagents.py` (three pins listed in Step 8)
- Delete: `e2e/tests/careagents-connect-tiles.spec.ts`
- Create: `e2e/tests/careagents-calm-hub.spec.ts`
- Create: `tests/test_careagents_calm_hub_page.py`

**Interfaces:**
- Consumes: `hub.build`, `hub.switch_prompt` (5A), `GET /api/approvals/count` (5B), `POST /api/hub/switch-prompt` (5C), Task 3's agent routes, `connectors.GROUPS` and `chip` (Task 1).
- Produces: `home.html` context `hub`, `switch_prompt`, `has_grants`, `menu_open`, `groups`, `catalog`, `terms_url`, `privacy_url`. DOM ids `#waiting`, `#explore-sample`, `#records-picker`, `#rename-modal`, `#agent-delete-modal`, `#past-connections`, `#start-chat`, `#switch-prompt`.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/templates/home.html careagents/app.py careagents/static/home.js careagents/static/careagents.css tests/test_careagents.py e2e/tests/careagents-connect-tiles.spec.ts e2e/tests/careagents-calm-hub.spec.ts
```

- [ ] **Step 2: Write the failing page tests**

Create `tests/test_careagents_calm_hub_page.py`:

```python
"""The calm hub page (spec section 3, 4, 7 and 9)."""

from __future__ import annotations

import pathlib
import re

from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _beta_app, _login, app, cfg, svc)

_HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
            / "careagents" / "static" / "home.js").read_text()


def _signed_in(app, svc, monkeypatch, email="gene@example.com"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with c.session_transaction() as s:
        return c, s["account_id"]


def _card(body, conn_id):
    at = body.index(f'data-conn="{conn_id}" data-kind')
    start = body.rindex('<div class="hub-card conn-card"', 0, at)
    return body[start:body.index("conn-refresh-msg", at)]


def _menu(body):
    start = body.index('id="connect-section"')
    return body[start:body.index("</section>", start)]


def test_the_waiting_line_starts_as_checking_never_zero(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    body = c.get("/home").get_data(as_text=True)
    assert 'id="waiting" data-state="checking"' in body
    assert "Checking for requests…" in body
    assert "Nothing yet" not in body
    # The browser keeps "unknown" apart from zero.
    assert "Couldn't check for requests." in _HOME_JS
    assert 'typeof d.count !== "number"' in _HOME_JS


def test_the_hub_shows_no_revoked_connection_outside_past_connections(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    live = svc.add_connection(aid, "direct", "ca-live", "Upload",
                              status="active")
    gone = svc.add_connection(aid, "direct", "ca-gone", "Old upload",
                              status="active")
    svc.revoke_connection(aid, gone)
    body = c.get("/home").get_data(as_text=True)
    past = body.index('id="past-connections"')
    assert live in body[:past]
    assert gone not in body[:past]
    assert gone in body[past:]


def test_every_status_word_renders_in_plain_language(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    for status in ("active", "pending", "empty"):
        svc.add_connection(aid, "direct", f"ca-{status}", f"R {status}",
                           status=status)
    body = c.get("/home").get_data(as_text=True)
    words = dict(re.findall(
        r'<span class="status status-(\w+)">([^<]*)</span>', body))
    assert words == {"active": "Connected", "pending": "Connecting…",
                     "empty": "No records yet"}
    assert ">revoked<" not in body and ">active<" not in body


def test_a_sample_card_offers_delete_only_with_badge_count_and_age(
        app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert "Made-up records" in card
    assert "100 records" in card and "Updated today" in card
    assert "conn-delete" in card
    assert "conn-disconnect" not in card and "conn-refresh" not in card


def test_the_assistant_card_has_chat_brief_and_a_menu(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    body = c.get("/home").get_data(as_text=True)
    assert f'href="/chat?agent={agent}">Chat</a>' in body
    assert f'href="/brief?agent={agent}">Visit brief</a>' in body
    assert "Reads Sample records" in body
    assert 'class="agent-rename"' in body and 'class="agent-delete"' in body
    # One active connection: nothing to change records to.
    assert 'class="agent-move"' not in body
    svc.add_connection(aid, "direct", "ca-two", "Upload", status="active")
    assert 'class="agent-move"' in c.get("/home").get_data(as_text=True)


def test_the_hub_has_no_add_another_assistant_control(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    c.post("/api/connections/sample")
    body = c.get("/home").get_data(as_text=True)
    assert 'id="new-agent-btn"' not in body
    assert 'id="agent-modal"' not in body
    assert 'id="start-chat"' not in body


def test_an_account_with_records_and_no_assistant_can_start_one(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    c.delete(f"/api/agents/{conn['agent_id']}")
    body = c.get("/home").get_data(as_text=True)
    assert f'id="start-chat" data-conn="{conn["id"]}"' in body


def test_the_closed_menu_is_one_action_and_one_line(svc, monkeypatch):
    c = _beta_app(svc).test_client()
    _login(c, svc, monkeypatch, email="tester@example.org")
    menu = _menu(c.get("/home").get_data(as_text=True))
    assert menu.count('id="explore-sample"') == 1
    assert "Explore with made-up records" in menu
    assert ("Coming for invited testers: your doctor's records, Apple "
            "Health and wearables, uploading a file from your patient "
            "portal.") in menu
    assert "Coming soon" not in menu and "menu-group" not in menu


def test_the_open_menu_groups_sources_with_one_chip_each(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    menu = _menu(c.get("/home").get_data(as_text=True))
    assert "Start with sample records, or find your own." in menu
    for name in ("Find my records", "Record services", "Bring a file",
                 "Devices and apps"):
        assert f"<h3>{name}</h3>" in menu, name
    assert 'class="linkish sample-link' in menu
    assert 'id="explore-sample"' not in menu
    svc.add_connection(aid, "direct", "ca-up", "Upload", status="active")
    menu = _menu(c.get("/home").get_data(as_text=True))
    for cid, chip in (("direct", "Connected"), ("fasten", "Available"),
                      ("hbo", "Coming soon")):
        row = menu[menu.index(f'data-connector="{cid}"'):]
        assert re.search(r'<span class="chip[^"]*">([^<]+)</span>',
                         row).group(1) == chip, cid


def test_a_crowded_legacy_account_renders_every_row(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    samples = [svc.add_connection(aid, "sample", f"ca-s{i}", "Sample records")
               for i in range(3)]
    pending = svc.add_connection(aid, "fasten", "ca-f", "Clinic",
                                 status="pending")
    names = ["Juniper", "Ada", "Coach", "Scout"]
    for i, name in enumerate(names):
        svc.create_agent(aid, name, "calm", samples[i % 2])
    r = c.get("/home")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for name in names:
        assert f'<div class="hub-card-name">{name}</div>' in body
    for cid in samples + [pending]:
        assert f'data-conn="{cid}" data-kind' in body
```

- [ ] **Step 3: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_page.py -q
```

Expected: FAIL. `#waiting`, `#past-connections` and the plain status words are not rendered yet.

- [ ] **Step 4: Rewrite `home.html`**

Replace `careagents/templates/home.html` with:

```html
{% extends "base.html" %}
{% block title %}Your hub — CareAgents{% endblock %}
{% block bodyclass %}page-home{% endblock %}
{% block head %}<link rel="manifest" href="/manifest.webmanifest">{% endblock %}
{% block content %}
{% include "_beta_banner.html" %}
<main class="hub">
  <div class="hub-head">
    <div>
      <h1>Welcome back</h1>
      <p class="hub-sub">{{ me.email }}</p>
    </div>
    <a class="pill" href="/settings">Settings</a>
  </div>

  {# 1. Waiting for you. Always shown. It starts as "Checking" and home.js
     fills it from /api/approvals/count. A failed count says so and is
     never rendered as zero (calm hub spec section 3). #}
  <section class="waiting" id="waiting" data-state="checking" aria-live="polite">
    <h2>Waiting for you</h2>
    <p class="waiting-line">Checking for requests…</p>
  </section>

  {% if switch_prompt %}
  <section class="switch-prompt" id="switch-prompt"
           data-agent="{{ switch_prompt.agent_id }}"
           data-conn="{{ switch_prompt.connection_id }}">
    <p>Switch {{ switch_prompt.agent_name }} to your records?</p>
    <button type="button" class="btn-primary" id="switch-yes">Switch</button>
    <button type="button" class="linkish" id="switch-later">Not now</button>
    <span class="inline-msg" id="switch-msg" role="status" aria-live="polite" hidden></span>
  </section>
  {% endif %}

  <!-- 2. YOUR ASSISTANT -->
  <section class="hub-section">
    <div class="section-head"><h2>Your assistant</h2></div>
    <div class="card-grid" id="agents" data-records='{{ hub.move_choices | tojson }}'>
      {% for a in hub.agents %}
      <div class="hub-card agent-card" data-agent="{{ a.id }}">
        <div class="hub-card-name">{{ a.name }}</div>
        <div class="hub-card-sub">{% if a.reads %}Reads {{ a.reads }}{% endif %}</div>
        <div class="card-actions">
          <a class="pill pill-cta" href="/chat?agent={{ a.id }}">Chat</a>
          <a class="pill" href="/brief?agent={{ a.id }}">Visit brief</a>
          <details class="card-menu">
            <summary>More</summary>
            <button type="button" class="agent-rename" data-agent="{{ a.id }}"
                    data-name="{{ a.name }}">Rename</button>
            {% if hub.active_records | length > 1 %}
            <button type="button" class="agent-move" data-agent="{{ a.id }}"
                    data-conn="{{ a.connection_id }}">Change records</button>
            {% endif %}
            <button type="button" class="agent-delete" data-agent="{{ a.id }}"
                    data-name="{{ a.name }}">Delete</button>
          </details>
        </div>
        <span class="agent-msg inline-msg" role="status" aria-live="polite" hidden></span>
      </div>
      {% endfor %}
      {% if not hub.agents %}
        {% if hub.active_records %}
        <div class="empty">Your records are ready.
          <button type="button" class="linkish" id="start-chat" data-conn="{{ hub.active_records[0].id }}">Start a chat</button></div>
        {% else %}
        <div class="empty">Your assistant appears here once records are connected.</div>
        {% endif %}
      {% endif %}
    </div>
  </section>

  <!-- 3. YOUR RECORDS -->
  <section class="hub-section" id="records-section">
    <div class="section-head"><h2>Your records</h2></div>
    <div class="card-grid" id="connections">
      {% for c in hub.records %}
      <div class="hub-card conn-card" data-tenant="{{ c.tenant_id }}"
           data-conn="{{ c.id }}" data-kind="{{ c.kind }}" data-status="{{ c.status }}">
        <div class="hub-card-name">{{ c.label }}</div>
        <div class="hub-card-sub">{{ c.provider or '' }}</div>
        {% if c.is_sample %}<span class="badge-sample">Made-up records</span>{% endif %}
        <span class="status status-{{ c.status }}">{{ c.status_word }}</span>
        <p class="conn-facts">{{ c.count_line }}{% if c.count_line and c.updated %} · {% endif %}{{ c.updated }}</p>
        {% if c.kind == 'direct' %}
        <button type="button" class="conn-upload" data-conn="{{ c.id }}">Upload a file</button>
        {% elif not c.is_sample %}
        <button type="button" class="conn-refresh" data-conn="{{ c.id }}">Refresh</button>
        {% endif %}
        {% if not c.is_sample %}
        <button type="button" class="conn-disconnect" data-conn="{{ c.id }}"
                title="Stop new records arriving; keep what's already here">Disconnect</button>
        {% endif %}
        <button type="button" class="conn-delete" data-conn="{{ c.id }}"
                data-label="{{ c.label }}">Delete</button>
        <span class="conn-refresh-msg" role="status" aria-live="polite" hidden></span>
      </div>
      {% endfor %}
      {% if not hub.records %}
      <div class="empty">No records yet. Add some below.</div>
      {% endif %}
    </div>
    {% if has_grants %}
    <p class="hub-link"><a href="/settings#grants-section">Apps you've shared records with</a></p>
    {% endif %}
  </section>

  <!-- 4. ADD RECORDS -->
  <section class="hub-section" id="connect-section">
    <div class="section-head"><h2>Add records</h2></div>
    {% if not menu_open %}
    <button type="button" class="btn-primary btn-block connector-tile"
            id="explore-sample" data-connector="sample">Explore with made-up records</button>
    <p class="menu-closed-line">Coming for invited testers: your doctor's records, Apple Health and wearables, uploading a file from your patient portal.</p>
    {% else %}
    {% if not hub.records %}<p class="onboard-lead">Start with sample records, or find your own.</p>{% endif %}
    {% for gid, gname in groups %}
    {% set items = catalog | selectattr('group', 'equalto', gid) | list %}
    {% if items %}
    <div class="menu-group" data-group="{{ gid }}">
      <h3>{{ gname }}</h3>
      {% for m in items %}
      {% set chip = 'Connected' if m.id in hub.connected_kinds else m.chip %}
      {% if m.tier == 'soon' %}
      <div class="connector-row tier-soon" data-connector="{{ m.id }}">
        <span class="connector-label">{{ m.label }}</span>
        <span class="connector-blurb">{{ m.blurb }}</span>
        <span class="chip chip-soon">{{ chip }}</span>
      </div>
      {% else %}
      <button type="button" class="connector-row connector-tile tier-{{ m.tier }}"
              data-connector="{{ m.id }}"
              {% if m.get('providers') %}data-providers='{{ m.providers|tojson }}'{% endif %}
              {% if m.get('requires_consent') %}data-consent="1"{% endif %}>
        <span class="connector-label">{{ m.label }}</span>
        <span class="connector-blurb">{{ m.blurb }}</span>
        <span class="chip">{{ chip }}</span>
      </button>
      {% endif %}
      {% endfor %}
    </div>
    {% endif %}
    {% endfor %}
    <button type="button" class="linkish sample-link connector-tile"
            data-connector="sample">Explore with made-up records</button>
    {% if catalog | selectattr('id', 'in', ['fasten', 'wearable'])
                  | rejectattr('tier', 'equalto', 'soon') | list %}
    <p class="add-note">Logins to your doctor or your devices happen on their
      own site. We never see your password.</p>
    {% endif %}
    {% endif %}
    <p class="inline-msg" id="connect-msg" role="status" aria-live="polite" hidden></p>
  </section>

  <!-- 5. PAST CONNECTIONS -->
  {% if hub.past %}
  <details class="hub-section past" id="past-connections">
    <summary>Past connections ({{ hub.past | length }})</summary>
    <div class="card-grid">
      {% for c in hub.past %}
      <div class="hub-card conn-card" data-tenant="{{ c.tenant_id }}"
           data-conn="{{ c.id }}" data-kind="{{ c.kind }}" data-status="{{ c.status }}">
        <div class="hub-card-name">{{ c.label }}</div>
        <div class="hub-card-sub">No new records arrive from this one.</div>
        <button type="button" class="conn-delete" data-conn="{{ c.id }}"
                data-label="{{ c.label }}">Delete</button>
        <span class="conn-refresh-msg" role="status" aria-live="polite" hidden></span>
      </div>
      {% endfor %}
    </div>
  </details>
  {% endif %}
</main>
```

Keep, below `</main>` and unchanged from the current file: the consent modal (`#consent-modal`), the provider picker (`#provider-picker`), `{% include "_delete_modal.html" %}`, the hidden `#upload-file` input and the `home.js` script tag. Delete the `#agent-modal` block. Add these three dialogs before the script tag:

```html
<!-- Change records: one row per other active connection, from #agents'
     data-records. Same sheet pattern as the provider picker. -->
<div class="modal sheet" id="records-picker" role="dialog" aria-modal="true"
     aria-labelledby="records-title" hidden>
  <div class="modal-card">
    <h3 id="records-title">Which records should it read?</h3>
    <div id="records-rows"></div>
    <button type="button" class="linkish" id="records-cancel">Not now</button>
  </div>
</div>

<div class="modal" id="rename-modal" role="dialog" aria-modal="true"
     aria-labelledby="rename-title" hidden>
  <div class="modal-card">
    <h3 id="rename-title">Rename your assistant</h3>
    <label class="field-label" for="rename-input">Name</label>
    <input class="field" id="rename-input" maxlength="48" autocomplete="off">
    <button type="button" class="btn-primary btn-block" id="rename-save">Save</button>
    <button type="button" class="linkish" id="rename-cancel">Cancel</button>
  </div>
</div>

<div class="modal" id="agent-delete-modal" role="dialog" aria-modal="true"
     aria-labelledby="agent-delete-title" hidden>
  <div class="modal-card">
    <h3 id="agent-delete-title">Delete <span id="agent-delete-name"></span>?</h3>
    <p>Your records and your conversation stay. Only the assistant goes.</p>
    <button type="button" class="btn-primary btn-block" id="agent-delete-confirm">Delete</button>
    <button type="button" class="linkish" id="agent-delete-cancel">Cancel</button>
  </div>
</div>
```

- [ ] **Step 5: Pass the view from `home()`**

In `careagents/app.py`, add `from careagents import hub as hub_view` next to the other `careagents` imports (line 31). Replace the `render_template` call in `home()` with:

```python
        real_open = cfg.real_records_open_for(acct.email)
        return render_template(
            "home.html", me=acct,
            hub=hub_view.build(data, time.time()),
            switch_prompt=(None if svc.switch_prompted_at(acct.id)
                           else hub_view.switch_prompt(data)),
            has_grants=bool(svc.list_grants(acct.id)),
            menu_open=real_open, groups=connectors.GROUPS,
            terms_url=f"{cfg.healthclaw_public_base}/terms",
            privacy_url=f"{cfg.healthclaw_public_base}/privacy",
            catalog=connectors.catalog(cfg, real_records=real_open))
```

- [ ] **Step 6: Wire the page in `home.js`**

Replace lines 1-2:

```js
/* CareAgents hub and settings: add records, the assistant's menu, the
   waiting band. Small vanilla JS; the server is authoritative. */
```

In the connector tile handler, delete the `if (tile.dataset.soon) { … }` branch (lines 17-25) and the `if (res.d.soon) …` line (line 49). Coming-soon rows are not buttons now.

Delete the `// --- new agent modal ---` block, including Task 4's `if (modal) { … }` wrapper. Put this in its place:

```js
  // --- waiting for you (spec section 3) ---
  // "Checking" until the count answers. A failed or malformed answer says
  // so; it is never rendered as zero (#215, #403).
  const waiting = $("waiting");
  function showWaiting(state, d) {
    const line = waiting.querySelector(".waiting-line");
    waiting.dataset.state = state;
    if (state === "fail") { line.textContent = "Couldn't check for requests."; return; }
    if (state === "none") {
      line.textContent = "Nothing yet. Anything your assistant prepares, " +
        "like a form or a reminder, waits here for your OK.";
      return;
    }
    const a = document.createElement("a");
    a.href = d.href;
    a.textContent = d.count + (d.count === 1 ? " request" : " requests") +
      " waiting for your approval";
    line.replaceChildren(a);
  }
  if (waiting) {
    fetch("/api/approvals/count").then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok || typeof d.count !== "number") return showWaiting("fail", d);
      showWaiting(d.count > 0 && d.href ? "pending" : "none", d);
    }).catch(() => showWaiting("fail", {}));
  }

  // --- switch to your records (spec section 5) ---
  const sw = $("switch-prompt");
  if (sw) {
    const answer = async (a) => {
      const res = await post("/api/hub/switch-prompt", {
        answer: a, agent_id: sw.dataset.agent, connection_id: sw.dataset.conn });
      if (!res.ok) return announce($("switch-msg"), "That didn't work. Try again.");
      location.reload();
    };
    $("switch-yes").addEventListener("click", () => answer("switch"));
    $("switch-later").addEventListener("click", () => answer("later"));
  }

  // --- your assistant: start, rename, change records, delete ---
  const startChat = $("start-chat");
  if (startChat) startChat.addEventListener("click", async () => {
    startChat.disabled = true;
    const res = await post("/api/agents", {
      name: "Juniper", persona: "calm", connection_id: startChat.dataset.conn });
    if (res.ok) { location.href = "/chat?agent=" + res.d.id; return; }
    startChat.disabled = false;
    say(startChat, $("connect-msg"), res.d.error || "Couldn't start a chat.");
  });

  const agentMsg = (btn) => btn.closest(".agent-card").querySelector(".agent-msg");

  function askForName(current) {
    const input = $("rename-input");
    input.value = current || "";
    const dlg = openDialog($("rename-modal"));
    $("rename-save").onclick = () => dlg.close(input.value.trim() || null);
    input.onkeydown = (e) => { if (e.key === "Enter") $("rename-save").onclick(); };
    $("rename-cancel").onclick = () => dlg.close(null);
    input.focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-rename").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = await askForName(btn.dataset.name);
      if (!name) return;
      const res = await post(`/api/agents/${btn.dataset.agent}/rename`, { name });
      if (!res.ok) return announce(agentMsg(btn), res.d.error || "Couldn't rename.");
      location.reload();
    });
  });

  let moveChoices = [];
  try { moveChoices = JSON.parse(($("agents") && $("agents").dataset.records) || "[]"); }
  catch (e) { moveChoices = []; }
  function pickRecords(currentId) {
    const rows = $("records-rows");
    rows.textContent = "";
    const dlg = openDialog($("records-picker"));
    moveChoices.filter((c) => c.id !== currentId).forEach((c) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "picker-row";
      row.textContent = c.label;   // server-supplied label: text, never markup
      row.addEventListener("click", () => dlg.close(c.id));
      rows.appendChild(row);
    });
    $("records-cancel").onclick = () => dlg.close(null);
    const first = rows.querySelector(".picker-row");
    if (first) first.focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-move").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const conn = await pickRecords(btn.dataset.conn);
      if (!conn) return;
      const res = await post(`/api/agents/${btn.dataset.agent}/connection`,
                             { connection_id: conn });
      if (!res.ok) {
        return announce(agentMsg(btn), "Those records aren't available. Refresh and try again.");
      }
      location.reload();
    });
  });

  function askToRemoveAgent(name) {
    $("agent-delete-name").textContent = name;
    const dlg = openDialog($("agent-delete-modal"));
    $("agent-delete-confirm").onclick = () => dlg.close(true);
    $("agent-delete-cancel").onclick = () => dlg.close(false);
    $("agent-delete-cancel").focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!(await askToRemoveAgent(btn.dataset.name))) return;
      const r = await fetch(`/api/agents/${btn.dataset.agent}`, { method: "DELETE" });
      if (!r.ok) return announce(agentMsg(btn), "Couldn't delete. Try again.");
      location.reload();
    });
  });
```

- [ ] **Step 7: Style it**

Append to `careagents/static/careagents.css`:

```css
/* --- calm hub (spec 2026-09-26) ------------------------------------------ */
.waiting { margin: 0 0 24px; padding: 16px 18px; border-radius: var(--radius);
  background: var(--paper); border: 1px solid var(--hairline); }
.waiting h2 { font-size: 16px; margin: 0 0 4px; }
.waiting-line { margin: 0; color: var(--ink-soft); }
.waiting[data-state="pending"] { background: #FBEFD9; border-color: #E9CF9E; }
.waiting[data-state="pending"] .waiting-line a { color: var(--clay-deep);
  font-weight: 700; }
.waiting[data-state="fail"] .waiting-line { color: var(--clay-deep); }
.switch-prompt { margin: 0 0 24px; padding: 16px 18px; border-radius: var(--radius);
  background: var(--card); border: 1px solid var(--hairline); }
.card-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-top: 12px; }
.card-menu summary { cursor: pointer; min-height: 44px; display: inline-flex;
  align-items: center; padding: 0 12px; }
.card-menu button { display: block; width: 100%; min-height: 44px;
  text-align: left; background: none; border: 0; font: inherit; color: inherit; }
.badge-sample { display: inline-block; margin-top: 6px; padding: 2px 8px;
  border-radius: 999px; font-size: 12px; font-weight: 700;
  background: var(--paper); color: var(--ink-soft); }
.conn-facts { margin: 6px 0 0; font-size: 14px; color: var(--ink-soft); }
.menu-group { margin: 0 0 18px; }
.menu-group h3 { font-size: 15px; margin: 0 0 8px; }
.connector-row { display: grid; grid-template-columns: 1fr auto;
  gap: 2px 12px; width: 100%; min-height: 44px; margin-bottom: 8px;
  padding: 14px 16px; text-align: left; font: inherit; color: inherit;
  background: var(--card); border: 1px solid var(--hairline); border-radius: 14px; }
.connector-row .connector-label { grid-column: 1; font-weight: 700; }
.connector-row .connector-blurb { grid-column: 1; font-size: 14px;
  color: var(--ink-soft); }
.connector-row .chip { grid-column: 2; grid-row: 1 / span 2; align-self: center; }
.chip { padding: 3px 10px; border-radius: 999px; font-size: 12px;
  font-weight: 700; background: #E9F0E7; color: var(--sage); }
.chip-soon { background: var(--paper); color: var(--ink-soft); }
.menu-closed-line { margin: 12px 0 0; color: var(--ink-soft); }
.past summary { cursor: pointer; min-height: 44px; display: flex;
  align-items: center; font-weight: 700; }
```

- [ ] **Step 8: Update the existing pins this task replaces**

In `tests/test_careagents.py`:

1. `test_fresh_home_gates_agent_modal_and_shows_onboarding` (lines 2567-2579): replace the body after `_login(c, svc, monkeypatch)` with:

```python
    html = c.get("/home").data.decode()
    # The add-assistant modal is gone (calm hub spec section 5).
    assert 'id="agent-modal"' not in html
    # First run, real records open in this fixture: the plain first line.
    assert "Start with sample records, or find your own." in html
```

2. `test_real_record_tiles_are_coming_soon_when_the_switch_is_off` (lines 5881-5888): replace from `body = c.get("/home")…` to the end with:

```python
    body = c.get("/home").get_data(as_text=True)
    # Closed state: no tiles for these sources at all (calm hub spec
    # section 4), one primary action and one line instead.
    for tile in _REAL_RECORD_TILES:
        assert f'data-connector="{tile}"' not in body, tile
    assert 'id="explore-sample"' in body
    assert "Coming for invited testers" in body
```

3. `test_the_password_reassurance_is_absent_while_those_logins_are_closed` (line 6340): replace `assert "Not open in this beta" in body` with `assert "Coming for invited testers" in body`.

- [ ] **Step 9: Replace the e2e tile spec**

Delete `e2e/tests/careagents-connect-tiles.spec.ts`. Every assertion in it pins the tile grid this task removes. Create `e2e/tests/careagents-calm-hub.spec.ts`:

```ts
import { test, expect } from '@playwright/test';
import {
  CARE_ALLOW_BASE_URL, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG,
  CARE_BASE_URL, CARE_LOG,
} from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * The calm hub a beta tester first sees, on a phone
 * (docs/superpowers/specs/2026-09-26-careagents-calm-hub-design.md).
 *
 * Proves what the browser renders. HEALTHCLAW_BASE is a dead port here,
 * so nothing about records being accepted is proven.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

const CLOSED_LINE =
  "Coming for invited testers: your doctor's records, Apple Health and " +
  'wearables, uploading a file from your patient portal.';

test.describe('calm hub, real records closed', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, uniqueEmail(), CARE_LOG);
  });

  test('one primary action and one line, no coming-soon rows', async ({ page }) => {
    await expect(page.locator('#explore-sample'))
      .toHaveText('Explore with made-up records');
    await expect(page.locator('.menu-closed-line')).toHaveText(CLOSED_LINE);
    await expect(page.locator('.connector-row')).toHaveCount(0);
    await expect(page.locator('.chip')).toHaveCount(0);
  });

  test('a new account is told honestly that nothing waits', async ({ page }) => {
    await expect(page.locator('#waiting .waiting-line'))
      .toHaveText(/^Nothing yet\./);
  });

  test('a failed count says so', async ({ page }) => {
    await page.route('**/api/approvals/count', (route) => route.fulfill({
      status: 503, contentType: 'application/json',
      body: '{"error":"unavailable"}',
    }));
    await page.reload();
    await expect(page.locator('#waiting .waiting-line'))
      .toHaveText("Couldn't check for requests.");
    await expect(page.locator('#waiting')).toHaveAttribute('data-state', 'fail');
  });

  test('the hub has no horizontal scroll at 375px', async ({ page }) => {
    const wide = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth);
    expect(wide).toBe(false);
  });
});

test.describe('calm hub, real records open (allowlisted)', () => {
  test.use({ baseURL: CARE_ALLOW_BASE_URL, ...PHONE });

  test('grouped menu with one chip per source', async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG);
    for (const g of ['Find my records', 'Record services', 'Bring a file',
                     'Devices and apps']) {
      await expect(page.locator('.menu-group h3', { hasText: g })).toBeVisible();
    }
    await expect(page.locator('.connector-row[data-connector="direct"] .chip'))
      .toHaveText('Available');
    await expect(page.locator('.connector-row[data-connector="hbo"] .chip'))
      .toHaveText('Coming soon');
    await expect(page.locator('#explore-sample')).toHaveCount(0);
    await expect(page.locator('.sample-link')).toBeVisible();
  });
});
```

- [ ] **Step 10: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_page.py -q
uv run python -m pytest tests/ -q -k "careagents or browser_js"
cd e2e && E2E_PORT=5098 CARE_E2E_PORT=5111 CARE_ALLOW_E2E_PORT=5112 npx playwright test tests/careagents-calm-hub.spec.ts tests/careagents.spec.ts; cd ..
```

Pick free ports first with `lsof -iTCP:<port> -sTCP:LISTEN`, and say which you used. Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add careagents/templates/home.html careagents/app.py careagents/static/home.js careagents/static/careagents.css tests/test_careagents.py tests/test_careagents_calm_hub_page.py e2e/tests/careagents-calm-hub.spec.ts
git rm e2e/tests/careagents-connect-tiles.spec.ts
git commit -s \
    -m "Lay the hub out as waiting, assistant, records, add records, past connections" \
    -m "The waiting band never renders a failed count as zero. Assistant cards carry Chat, Visit brief and a menu. Record cards show a count and age, and the menu has a closed and an open state." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Copy cleanup

The duplicated starter block in `chat.html` was removed in Task 2, Step 8, on the same lines.

**Files:**
- Modify: `careagents/templates/_beta_banner.html`
- Modify: `careagents/app.py` (`home` render call; iMessage instructions line 1856; fasten label in `connectors.start`)
- Modify: `careagents/connectors.py` (`start` fasten plan, lines 160-164)
- Modify: `careagents/static/home.js` (Telegram handler, lines 624-640)
- Modify: `templates/fasten_connect.html` (Step 2 paragraph, lines 260-266)
- Modify: `tests/test_careagents.py` (`test_the_beta_banner_is_on_the_landing_page_and_the_hub`, line 6004)
- Modify: `e2e/tests/careagents-calm-hub.spec.ts` (append one test)
- Create: `tests/test_careagents_calm_hub_copy.py`
- Create: `tests/test_fasten_connect_step_two_plain.py`

**Interfaces:**
- Consumes: `hub_view.build(...)["has_real"]` (5A).
- Produces: `_beta_banner.html` reads an optional `banner_records: str`.

- [ ] **Step 1: Sequence behind PR #837, then check the lane**

PR #837 changes `templates/fasten_connect.html` and has auto-merge armed. Do not touch that PR. Wait until it merges, then rebase:

```bash
gh pr view 837 --json state,mergedAt
git fetch origin && git rebase origin/main
uv run python scripts/lane_check.py careagents/templates/_beta_banner.html careagents/app.py careagents/connectors.py careagents/static/home.js templates/fasten_connect.html tests/test_careagents.py
```

Expected: #837 `MERGED`. If it is still open, report NEEDS_CONTEXT. Do not edit a file under an armed auto-merge.

- [ ] **Step 2: Write the failing CareAgents copy tests**

Create `tests/test_careagents_calm_hub_copy.py`:

```python
"""Plain words (calm hub spec section 7)."""

from __future__ import annotations

import pathlib
import re

from careagents import connectors
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

_CA = pathlib.Path(__file__).resolve().parents[1] / "careagents"


def _banner(html):
    sentence = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', html,
                         re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", "", sentence).split())


def test_the_hub_banner_matches_the_account(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: sample records. Things will break, tell us.")
    with c.session_transaction() as s:
        aid = s["account_id"]
    svc.add_connection(aid, "direct", "ca-real", "Upload", status="active")
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: your records are connected. Things will break, tell us.")


def test_the_landing_banner_keeps_its_words_without_the_em_dash(app):
    assert _banner(app.test_client().get("/").get_data(as_text=True)) == (
        "Beta: synthetic records only. Things will break, tell us.")


def test_the_stale_telegram_handler_is_gone():
    js = (_CA / "static" / "home.js").read_text()
    assert "tg-surface" not in js and "tg-state" not in js


def test_imessage_instructions_name_no_deployment():
    src = (_CA / "app.py").read_text()
    assert "iMessage isn't configured on this deployment" not in src


def test_a_new_provider_connection_is_named_in_plain_words(cfg):
    plan = connectors.start("fasten", None, cfg, FakeClient(),
                            real_records=True)
    assert plan["label"] == "Records from your doctor"
    # chat.html reads "Your records from {{ intake.provider }} haven't
    # arrived yet", so the provider must read after "from".
    assert plan["provider"] == "your doctor"


def test_the_waiting_chat_notice_reads_as_a_sentence(cfg, svc, monkeypatch):
    class _NotYet(FakeClient):
        def search(self, tenant, resource_type, params=None):
            return {"total": 0, "entry": []}

    from careagents.app import create_app
    a = create_app(config=cfg, client=_NotYet(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    with c.session_transaction() as s:
        aid = s["account_id"]
    agent = svc.create_agent(aid, "Juniper", "calm", conn["id"])
    page = " ".join(c.get(f"/chat?agent={agent}").get_data(as_text=True).split())
    assert "Your records from your doctor haven't arrived yet" in page
    assert "from Records from" not in page
```

- [ ] **Step 3: Write the failing Fasten page test**

Create `tests/test_fasten_connect_step_two_plain.py`:

```python
"""Step 2 of the connect page names no internals (calm hub spec section 7)."""

from __future__ import annotations

import re

_INTERNALS = ("org_connection_id", "/fasten/webhook", "Curatr")


def _visible(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", html,
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def test_step_two_names_no_internals(client, monkeypatch):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_step2")
    r = client.get("/connect/test-tenant")
    assert r.status_code == 200
    text = " ".join(_visible(r.get_data(as_text=True)).split())
    for word in _INTERNALS:
        assert word not in text, word
    assert "private space that only you control" in text
```

- [ ] **Step 4: Run them and watch them fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_copy.py tests/test_fasten_connect_step_two_plain.py -q
```

Expected: FAIL on every test.

- [ ] **Step 5: The banner**

Replace `careagents/templates/_beta_banner.html` with:

```html
{# Beta posture (council ruling 2026-09-02, D3): one line, on the landing
   page and the hub. On the hub it names the account's own records (calm
   hub spec section 7). The landing page has no account. Tests pin it under
   fifteen words. #}
<p class="beta-banner"><b>Beta:</b> {{ banner_records | default("synthetic records only") }}. Things will break,
  <a href="https://github.com/aks129/HealthClawGuardrails/issues"
     target="_blank" rel="noopener">tell us</a>.</p>
```

In `home()` in `careagents/app.py`, add this line before `real_open = …`:

```python
        view = hub_view.build(data, time.time())
```

In the `render_template` call, replace `hub=hub_view.build(data, time.time()),` with `hub=view,` and add:

```python
            banner_records=("your records are connected" if view["has_real"]
                            else "sample records"),
```

In `tests/test_careagents.py`, `test_the_beta_banner_is_on_the_landing_page_and_the_hub` (line 6004), replace `assert "Beta" in sentence and "synthetic" in sentence` with:

```python
    assert "Beta" in sentence and "sample records" in sentence
```

- [ ] **Step 6: Remove the Telegram handler, fix the iMessage line and the Fasten label**

In `careagents/static/home.js`, delete the `// --- Telegram surface ---` block (lines 624-640). Its element was removed for the beta (#536).

In `careagents/app.py`, replace `"iMessage isn't configured on this deployment yet."` (line 1856) with `"iMessage isn't available yet."`.

In `careagents/connectors.py`, in the `fasten` branch of `start()`, replace `"label": "My health provider", "provider": "Connecting…",` with:

```python
        return {"tenant": tenant, "status": "pending",
                "label": "Records from your doctor", "provider": "your doctor",
```

The provider must read after "from": `chat.html` line 29 renders "Your records from {{ intake.provider }} haven't arrived yet". The hub card shows it as its sub-line.

Keep the remaining keys of that dict unchanged.

- [ ] **Step 7: The Fasten page, Step 2**

In `templates/fasten_connect.html`, replace the `<p>` under `<h3>Step 2 — Records flow in the background</h3>` (lines 260-266) with:

```html
    <p>
      Once you finish, your records are copied into a private space that only
      you control. They arrive over the next 5–45 minutes, and each one is
      checked for obvious gaps or errors as it lands.
    </p>
```

- [ ] **Step 8: Add the banner to the e2e spec**

Append inside `test.describe('calm hub, real records closed', …)` in `e2e/tests/careagents-calm-hub.spec.ts`:

```ts
  test('the banner names the sample for a new account', async ({ page }) => {
    await expect(page.locator('.beta-banner'))
      .toHaveText('Beta: sample records. Things will break, tell us.');
  });
```

- [ ] **Step 9: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_copy.py tests/test_fasten_connect_step_two_plain.py -q
uv run python -m pytest tests/ -q -k "careagents or fasten"
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add careagents/templates/_beta_banner.html careagents/app.py careagents/connectors.py careagents/static/home.js templates/fasten_connect.html tests/test_careagents.py e2e/tests/careagents-calm-hub.spec.ts tests/test_careagents_calm_hub_copy.py tests/test_fasten_connect_step_two_plain.py
git commit -s \
    -m "Say what the person has, in plain words, on the hub and the connect page" \
    -m "The banner names the account's records. The Telegram handler bound to a removed element goes. The connect page's Step 2 names no internals." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Health Skillz as a guided two-step

**Files:**
- Modify: `careagents/connectors.py` (`_CATALOG`, `REAL_RECORD_SOURCES`, `catalog()` item keys, `start()`)
- Modify: `careagents/app.py` (`_start_connection`: the kind persisted)
- Modify: `careagents/templates/home.html` (menu loop; one new dialog)
- Modify: `careagents/static/home.js` (upload `change` handler; new two-step handler)
- Modify: `tests/test_careagents_calm_hub_catalog.py` (two tests, Step 7)
- Create: `tests/fixtures/health_skillz_export_synthetic.json`
- Create: `tests/test_careagents_calm_hub_health_skillz.py`

**Interfaces:**
- Consumes: `POST /api/connections/<id>/upload` (unchanged), `HealthClawClient.ingest_bundle`.
- Produces:
  - `connectors.HEALTH_SKILLZ_URL = "https://health-skillz.joshuamandel.com"`
  - Catalog item `healthskillz` in group `find`, with a `link` key.
  - `start("healthskillz", …)` returns a plan with `"kind": "direct"`. The app persists `plan.get("kind", connector_id)`.

- [ ] **Step 1: Check the lane**

```bash
uv run python scripts/lane_check.py careagents/connectors.py careagents/app.py careagents/templates/home.html careagents/static/home.js
```

- [ ] **Step 2: Capture a synthetic export (the gate)**

The spec's rule: Available only when a test runs the full import. The export shape is not documented, so capture a real one.

1. Open https://health-skillz.joshuamandel.com in a browser.
2. Connect to the Epic public sandbox and sign in as its published synthetic test patient. Never use a real account.
3. Download the records as JSON. Save it as `tests/fixtures/health_skillz_export_synthetic.json`.
4. Check the shape:

```bash
uv run python - <<'PY'
import json, pathlib
p = pathlib.Path("tests/fixtures/health_skillz_export_synthetic.json")
b = json.loads(p.read_text())
print("resourceType:", b.get("resourceType"), "entries:", len(b.get("entry") or []),
      "bytes:", p.stat().st_size)
assert b.get("resourceType") == "Bundle", "not a FHIR Bundle"
assert len(b["entry"]) <= 500, "over the engine's 500-entry cap"
assert p.stat().st_size <= 5 * 1024 * 1024, "over the 5 MB upload cap"
PY
```

If any assertion fails, STOP. Report BLOCKED: the export needs a converter, and the tile stays Coming soon. Do not trim the file to make it pass.

- [ ] **Step 3: Write the failing test**

Create `tests/test_careagents_calm_hub_health_skillz.py`:

```python
"""Health Skillz: the portal file lands in the person's own tenant.

Runs the full import through the real engine WSGI, the availability rule
in calm hub spec section 4.
"""

from __future__ import annotations

import json
import pathlib

import requests as _requests

from careagents.models import Agent, Connection
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, cfg, svc)

FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
           / "health_skillz_export_synthetic.json")
TENANT = "ca-skillz"


def _engine(monkeypatch):
    from main import create_app as engine_create_app
    from models import db
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{TENANT}")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    engine_app = engine_create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False})
    with engine_app.app_context():
        db.create_all()
    return engine_app.test_client()


def _real_client(engine):
    from careagents.healthclaw import HealthClawClient

    def _to_requests(resp):
        r = _requests.Response()
        r.status_code = resp.status_code
        r._content = resp.get_data() or b""
        r.headers.update(resp.headers.to_wsgi_list())
        return r

    class _Relay:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(engine.post(url.replace("http://engine", ""),
                                            json=json, data=data,
                                            headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            return _to_requests(engine.get(url.replace("http://engine", ""),
                                           query_string=params or {},
                                           headers=headers or {}))

    client = HealthClawClient(base="http://engine",
                              mint_secret="not-needed-for-public")
    client.http = _Relay()
    client.new_tenant_id = lambda: TENANT
    return client


def test_a_health_skillz_export_lands_in_the_persons_own_tenant(
        cfg, svc, monkeypatch):
    from careagents.app import create_app
    engine = _engine(monkeypatch)
    app = create_app(config=cfg, client=_real_client(engine), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)

    assert c.post("/api/connections/healthskillz").status_code == 428
    made = c.post("/api/connections/healthskillz", json={"consent": True})
    assert made.status_code == 200, made.get_data(as_text=True)
    conn_id = made.get_json()["id"]

    r = c.post(f"/api/connections/{conn_id}/upload",
               data=FIXTURE.read_text(),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ingested"] > 0
    assert r.get_json()["failed"] == 0

    probe = engine.get("/r6/fhir/Patient", query_string={"_summary": "count"},
                       headers={"X-Tenant-Id": TENANT})
    assert probe.status_code == 200 and probe.get_json()["total"] >= 1
    with svc.session() as s:
        conn = s.get(Connection, conn_id)
        assert (conn.kind, conn.status, conn.tenant_id) == (
            "direct", "active", TENANT)
        assert s.query(Agent).filter_by(connection_id=conn_id).count() == 1


def test_the_fixture_is_the_synthetic_sandbox_export():
    bundle = json.loads(FIXTURE.read_text())
    assert bundle["resourceType"] == "Bundle"
```

- [ ] **Step 4: Run it and watch it fail**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_health_skillz.py -q
```

Expected: FAIL. `POST /api/connections/healthskillz` answers 404 `unknown connector`.

- [ ] **Step 5: Add the source**

In `careagents/connectors.py`, after `WEARABLE_PROVIDERS`:

```python
# Health Skillz signs a person in to Epic MyChart and other SMART portals
# and lets them download their records. We link out and take the file
# through the ordinary upload (calm hub spec section 4).
HEALTH_SKILLZ_URL = "https://health-skillz.joshuamandel.com"
```

In `_CATALOG`, after the `fasten` entry:

```python
    {"id": "healthskillz", "tier": "import", "icon": "🗂️", "group": "find",
     "label": "Epic MyChart and other portals",
     "blurb": "Sign in at Health Skillz, download your file, then upload "
              "it here.",
     "link": HEALTH_SKILLZ_URL},
```

Change `REAL_RECORD_SOURCES` to `("fasten", "wearable", "direct", "healthskillz")`.

In `catalog()`, after `if "providers" in c: item["providers"] = c["providers"]`, add:

```python
        if "link" in c:
            item["link"] = c["link"]
```

In `start()`, before the `direct` branch:

```python
    if connector_id == "healthskillz":
        # The file lands through the `direct` upload, so the connection is
        # a `direct` one; only the label says where the file came from.
        tenant = client.new_tenant_id()
        return {"tenant": tenant, "status": "empty", "kind": "direct",
                "label": "Records from your patient portal",
                "provider": "Health Skillz", "requires_consent": True}
```

In `careagents/app.py`, `_start_connection`, change the first argument pair of `svc.add_connection(acct.id, connector_id, …)` to `svc.add_connection(acct.id, plan.get("kind", connector_id), …)`.

- [ ] **Step 6: The two-step sheet**

In `careagents/templates/home.html`, in the menu loop, find the `{% else %}` directly before `<button type="button" class="connector-row connector-tile`. Replace it with:

```html
      {% elif m.id == 'healthskillz' %}
      <button type="button" class="connector-row" id="skillz-row"
              data-connector="healthskillz">
        <span class="connector-label">{{ m.label }}</span>
        <span class="connector-blurb">{{ m.blurb }}</span>
        <span class="chip">{{ chip }}</span>
      </button>
      {% else %}
```

Before the script tag, add:

```html
{% set skillz = catalog | selectattr('id', 'equalto', 'healthskillz') | first %}
{% if menu_open and skillz and skillz.tier != 'soon' %}
<div class="modal sheet" id="skillz-sheet" role="dialog" aria-modal="true"
     aria-labelledby="skillz-title" hidden>
  <div class="modal-card">
    <h3 id="skillz-title">Get your file from your patient portal</h3>
    <ol class="skillz-steps">
      <li>Open Health Skillz, sign in to your portal, and download your
        records as a file.</li>
      <li>Come back here and upload that file.</li>
    </ol>
    <a class="btn-secondary btn-block" id="skillz-open" href="{{ skillz.link }}"
       target="_blank" rel="noopener">Open Health Skillz</a>
    <button type="button" class="btn-primary btn-block" id="skillz-upload">I have my file. Upload it</button>
    <button type="button" class="linkish" id="skillz-cancel">Not now</button>
  </div>
</div>
{% endif %}
```

In `careagents/static/home.js`, add after the `fileInput` declarations:

```js
  // --- Health Skillz: open it, then upload the file it gave you ---
  // The file is picked first, inside the tap, because a picker opened after
  // an await can be blocked. Consent, the connection and the upload follow.
  let skillzPending = false;
  const skillzRow = $("skillz-row");
  if (skillzRow && fileInput) skillzRow.addEventListener("click", () => {
    const dlg = openDialog($("skillz-sheet"));
    $("skillz-cancel").onclick = () => dlg.close(null);
    $("skillz-upload").onclick = () => {
      dlg.close(null);
      skillzPending = true;
      fileInput.value = "";
      fileInput.click();
    };
  });

  async function uploadPortalFile(file) {
    const msg = $("connect-msg");
    if (file.size > 5 * 1024 * 1024) {
      return say(skillzRow, msg, messageForError("payload_too_large"));
    }
    if (!(await showConsentCard())) return;
    const made = await post("/api/connections/healthskillz", { consent: true });
    if (!made.ok) return say(skillzRow, msg, made.d.error || "Couldn't start that upload.");
    say(skillzRow, msg, "Uploading " + file.name + "…");
    let r, d;
    try {
      r = await fetch(`/api/connections/${made.d.id}/upload`, {
        method: "POST", headers: { "Content-Type": "application/fhir+json" },
        body: await file.text() });
      d = await r.json().catch(() => ({}));
    } catch (e) {
      return say(skillzRow, msg, messageForError("ingest_failed"));
    }
    if (!r.ok) return say(skillzRow, msg, messageForError(d.error));
    location.reload();
  }
```

At the top of the `fileInput` `change` listener, before `const owner = currentUploadCard;`:

```js
      if (skillzPending) {
        skillzPending = false;
        const picked = fileInput.files && fileInput.files[0];
        if (picked) uploadPortalFile(picked);
        return;
      }
```

- [ ] **Step 7: Update the catalog tests for the new source**

In `tests/test_careagents_calm_hub_catalog.py`:

1. In `test_every_source_sits_in_its_named_group`, add `"healthskillz": "find",` to the expected dict.
2. In `test_the_open_menu_marks_phase_one_sources_available`, add `assert items["healthskillz"]["chip"] == "Available"`.

- [ ] **Step 8: Run the tests**

```bash
uv run python -m pytest tests/test_careagents_calm_hub_health_skillz.py tests/test_careagents_calm_hub_catalog.py -q
uv run python -m pytest tests/ -q -k careagents
```

Expected: all pass. The existing `test_no_connector_tile_advertises_a_flow_that_does_not_exist` must stay green.

- [ ] **Step 9: Commit**

```bash
git add careagents/connectors.py careagents/app.py careagents/templates/home.html careagents/static/home.js tests/test_careagents_calm_hub_catalog.py tests/test_careagents_calm_hub_health_skillz.py tests/fixtures/health_skillz_export_synthetic.json
git commit -s \
    -m "Offer Epic MyChart and other portals through Health Skillz, then the ordinary upload" \
    -m "Available because a test runs a synthetic sandbox export through the real engine and finds it in the person's own tenant." \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: The new-account journey in a browser, before and after

**Files:**
- Create: `docs/evidence/2026-09-26-careagents-calm-hub.md`
- Create: `docs/evidence/2026-09-26-careagents-calm-hub/` (PNG screenshots)
- Local only, never committed: `e2e/tests-local/calmhub.config.ts` and `e2e/tests-local/calmhub-journey.spec.ts`. They sit under `e2e/` so `@playwright/test` resolves. Delete the folder in Step 7.

**Interfaces:**
- Consumes: the running engine (`main.py`) and CareAgents (`careagents.wsgi`), and `signIn` from `e2e/tests/careagents-fixtures.ts`.
- Produces: eight screenshots (four before, four after) and one evidence page.

- [ ] **Step 1: Pick free ports**

```bash
for p in 5099 5103 5104 5105; do lsof -iTCP:$p -sTCP:LISTEN >/dev/null && echo "$p busy" || echo "$p free"; done
```

Use 5099 for the engine and 5103 for CareAgents after the change. Use 5104 and 5105 for the "before" pair. Replace any busy port and record the ports in the evidence page.

- [ ] **Step 2: Start the "after" pair on synthetic data**

```bash
S=/tmp/calmhub-run && mkdir -p $S && rm -f $S/*.db $S/*.log
( APP_ENV=testing FLASK_ENV=testing STEP_UP_SECRET=calmhub-local-step-up \
  INTERNAL_TOKEN_MINT_SECRET=calmhub-local SQLALCHEMY_DATABASE_URI=sqlite:///$S/engine.db PORT=5099 \
  sh -c 'uv run flask --app main init-db && uv run python main.py' > $S/engine.log 2>&1 & )
( CARE_ENV=development CARE_DATABASE_URL=sqlite:///$S/care.db \
  HEALTHCLAW_BASE=http://127.0.0.1:5099 HEALTHCLAW_MINT_SECRET=calmhub-local \
  CARE_ORIGIN=http://localhost:5103 CARE_RP_ID=localhost RESEND_API_KEY= CARE_REAL_RECORDS= \
  uv run flask --app careagents.wsgi run --port 5103 2>> $S/care.log & )
curl -sf http://127.0.0.1:5099/r6/fhir/health && curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:5103/
```

Expected: the engine health JSON, then `200`. Confirm the server is new code: `curl -s http://localhost:5103/home` must redirect, and a signed-in `/home` must contain `id="waiting"`. A stale process on the port is a false result.

- [ ] **Step 3: Write the scratch journey**

Save as `e2e/tests-local/calmhub-journey.spec.ts`. Save `e2e/tests-local/calmhub.config.ts` next to it: `testDir: '.'`, `workers: 1`, `use: { baseURL: process.env.CARE_URL }`, no `webServer`.

```ts
import { test, expect } from '@playwright/test';
import { signIn, uniqueEmail } from '../tests/careagents-fixtures';

const MODE = process.env.MODE || 'after';
const OUT = process.env.OUT!;
const LOG = process.env.CARE_LOG!;

test.use({ viewport: { width: 375, height: 812 }, isMobile: true,
           hasTouch: true, deviceScaleFactor: 2 });

test(`new-account journey (${MODE})`, async ({ page }) => {
  await signIn(page, uniqueEmail(), LOG);
  await page.screenshot({ path: `${OUT}/${MODE}-1-hub-new.png`, fullPage: true });

  if (MODE === 'after') {
    await page.locator('#explore-sample').click();
    await expect(page).toHaveURL(/\/chat\?agent=/);
    await expect(page.locator('.starter').first())
      .toHaveText('Fill out my intake form for a new doctor');
  } else {
    await page.locator('.connector-tile[data-connector="sample"]').click();
    await page.waitForLoadState('networkidle');
  }
  await page.screenshot({ path: `${OUT}/${MODE}-2-after-sample.png`, fullPage: true });

  await page.goto('/home');
  await page.screenshot({ path: `${OUT}/${MODE}-3-hub-with-sample.png`, fullPage: true });

  if (MODE === 'after') {
    const chatUrl = await page.evaluate(async () => {
      const r = await fetch('/api/connections/sample', { method: 'POST' });
      return (await r.json()).redirect;
    });
    expect(chatUrl).toMatch(/^\/chat\?agent=/);
    await page.goto('/home');
    await expect(page.locator('.conn-card[data-kind="sample"]')).toHaveCount(1);
    await expect(page.locator('#waiting .waiting-line')).not.toHaveText(/Checking/);
    const wide = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth);
    expect(wide).toBe(false);
  } else {
    await page.locator('.connector-tile[data-connector="sample"]').click();
    await page.waitForLoadState('networkidle');
  }
  await page.screenshot({ path: `${OUT}/${MODE}-4-second-tap.png`, fullPage: true });
});
```

- [ ] **Step 4: Run "after"**

```bash
OUT=docs/evidence/2026-09-26-careagents-calm-hub && mkdir -p $OUT
cd e2e && MODE=after OUT=../$OUT CARE_LOG=/tmp/calmhub-run/care.log CARE_URL=http://localhost:5103 \
  npx playwright test --config tests-local/calmhub.config.ts; cd ..
```

Expected: pass, and four `after-*.png` files.

- [ ] **Step 5: Run "before" against `origin/main`**

```bash
git worktree add /tmp/calmhub-before-wt origin/main
```

In that worktree, start the same pair on ports 5104 (engine) and 5105 (CareAgents), with fresh `.db` files under `/tmp/calmhub-before`. From this branch's `e2e/`, run the journey with `MODE=before`, `CARE_URL=http://localhost:5105` and `CARE_LOG=/tmp/calmhub-before/care.log`. Then stop both pairs and remove the worktree:

```bash
git worktree remove /tmp/calmhub-before-wt
```

Expected: four `before-*.png` files. Before, the second tap adds a second "Sample records" card.

- [ ] **Step 6: Write the evidence page**

Create `docs/evidence/2026-09-26-careagents-calm-hub.md`. Record the commit tested, the ports used, and that all data was the synthetic sample. Put each before and after screenshot side by side. Quote the pass line from Step 4. Link `2026-09-26-careagents-calm-hub-mutations.txt`. State what is not proven: a real portal, a real Fasten import, and a chat turn (no worker ran).

- [ ] **Step 7: Run everything, then commit**

```bash
rm -rf e2e/tests-local
uv run python -m pytest tests/ -q
uv run ruff check .
uv run python scripts/check_table_stakes.py --base origin/main
git add docs/evidence/2026-09-26-careagents-calm-hub.md docs/evidence/2026-09-26-careagents-calm-hub
git commit -s \
    -m "Record the new-account journey at phone width, before and after the calm hub" \
    -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Expected: the full suite passes, ruff is clean and the table-stakes check passes. Report the pass and fail counts verbatim.

---

## Spec coverage

| Spec section | Requirement | Task |
|---|---|---|
| 1 | No duplicate row for a new account | 1 |
| 1 | Every tile works or says why in one line | 1, 5D |
| 1 | Approval queue visible from the hub | 5B, 5D |
| 3.1 | Waiting for you: line, band, failure state | 5B, 5D |
| 3.2 | Assistant card: name, records, Chat, Visit brief, menu | 3, 5D |
| 3.3 | Record card: count, Updated N days ago, sample badge | 2 (count stored), 5A, 5D |
| 3.4 | Add records menu | 1, 5D, 7 |
| 3.5 | Past connections collapsed, with Delete | 5A, 5D |
| 3 | Surfaces, grants, account deletion to settings; grants link | 4 |
| 4 | Closed state: one action, one line | 1, 5D |
| 4 | Open state: groups, chips, availability rule | 1, 5D, 7 |
| 4 | No operator words; reason to the log | 1, 6 |
| 4 | Sample one per account | 1 |
| 4 | HBO, HealthEx, SHL wiring | Not in this plan. They render Coming soon; each needs its own wiring task. |
| 5 | First assistant, sample and ingest-complete paths | 2 |
| 5 | Sample lands in chat, intake starter first | 2 |
| 5 | Switch Juniper to your records | 5A, 5C, 5D |
| 5 | Change records with ownership check | 3 |
| 5 | Delete removes the assistant only | 3 |
| 5 | No add-another-assistant control | 5D |
| 5 | Nothing merged or deleted for existing accounts | 1, 2, 5D |
| 6 | Settings page | 4 |
| 7 | Every copy row | 1, 5A, 5D, 6 |
| 7 | Duplicate starters in `chat.html` | 2 |
| 7 | Stale Telegram handler in `home.js` | 6 |
| 9 | Every testing row | 1, 2, 3, 5B, 5D, 6, 8 |
