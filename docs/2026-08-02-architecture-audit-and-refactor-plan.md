# Architecture audit and refactor plan — 2026-08-02

Status: **awaiting approval — no code changes made.**
Evidence: five parallel read-only subsystem audits (r6 core, careagents,
agent-orchestrator, write/ingest rails, test architecture) plus 120-day git
churn analysis. Every claim below carries a file:line reference verified
against the working tree at this date.

## 0. The one-sentence diagnosis

The system's four core guarantees — tenant isolation, redaction, step-up,
audit — are implemented as **per-route conventions, not enforced invariants**:
in `r6/routes.py` alone there are 32 independent tenant-header reads, 41
audit calls, 11 redaction calls and 7 step-up gates, and the copies disagree
with each other. The retro's defect shape ("a control that looks like one
thing and quietly does two") is the emergent property of re-deciding every
guard at every site. The refactor's job is to make each property the output
of exactly one deep module, so the next change happens in one place.

Churn confirms where leverage lives (commits / fix-commits, 120 days):
`r6/routes.py` 38/7, `tools.ts` 32/8 (worst defect density), `r6/fasten/*`
19/11 (worst fix ratio), `careagents/app.py` 21/4.

## 1. Defects found during the audit (fix before/alongside, not "during")

These are findings, not refactor work. Each is a small standalone PR.

| # | Finding | Where | Severity |
|---|---------|-------|----------|
| S-1 | Ops reconciliation sweeps are not tenant-filtered: a step-up token for tenant A drives expiry/reconcile transitions and Telegram pushes across **every** tenant's actions | `r6/ops/routes.py:216-228` (queries), `:50-63` (auth), `:195-198` (push) | HIGH |
| S-2 | `/fasten/demo` writes 4 R6Resource rows + connection/job rows with **zero authentication** — its siblings (`/demo/agent-loop`, `/internal/seed`, `/internal/purge-tenant`) all require the mint secret | `r6/fasten/routes.py:501-697` | HIGH |
| S-3 | `$ingest-context` is the only write whose step-up gate is conditional on `READ_AUTH_ENABLED` — fails **open** when the flag is off | `r6/routes.py:1215-1219` | HIGH |
| S-4 | SHC ingest has no per-entry rollback (the exact 2026-07-08 session-poisoning defect the fasten path was patched for) **and** logs the raw exception (`%s', job_id, exc`) — the PHI-leak shape every other ingest path avoids | `r6/shc/routes.py:255-257` | HIGH |
| S-5 | `validate_step_up_token(...)[0]` — tuple indexed, not destructured; error discarded. Violates the repo non-negotiable | `r6/agent_runs/routes.py:53`; also discarded at `r6/command_center/routes.py:266` | MED |
| S-6 | `rx_transfer_request` entry in `adapters/tools.manifest.json` is missing `inputSchema`/`title`/`annotations` entirely → OpenAI/Gemini adapters emit an empty parameter schema, every model call fails Ajv. The manifest claims to be generated; **no generator exists** | `adapters/tools.manifest.json`; `adapters/healthclaw_bridge.py:51`; `tests/test_docs_tool_catalogue_drift.py:13-14` | MED |
| S-7 | `/mcp/rpc` `context/get` bypasses Ajv validation, tenant defaulting, the privileged check and the central timeout catch | `services/agent-orchestrator/src/index.ts:618-628` | MED |
| S-8 | `curatr_apply_fix` unconditionally injects `X-Human-Confirmed: true` upstream, while README v1.8.0 notes say the header is gone; it also runs its own step-up check with different header-casing rules than the central gate | `tools.ts:1882`, `:1864-1871` vs `:1153-1157`; `README.md:87` | MED (ties to #214) |
| S-9 | Local read 404s emit **no** audit event (upstream 404s do), contradicting `r6/audit.py:4` | `r6/routes.py:596-598`, `691-693`, `1268-1278`, `1811-1813`, `3104-3108` | MED |
| S-10 | Two GET routes mutate the store: `$curatr-evaluate` persists curation state (audited as `'read'`); SMBP PDF report inserts a DocumentReference | `r6/routes.py:2881-2935` + `r6/curatr.py:899-938`; `r6/smbp/routes.py:137-155` | MED |
| S-11 | `update_resource` returns the stored resource **unredacted** (`resource.to_fhir_json()`); read paths redact | `r6/routes.py:716-721` | MED |
| S-12 | Drift facts: README states three different tool counts (29 / 28 / 12) on one page; `server.json` pinned at 1.8.0 vs 1.9.0 everywhere else; `docs/development.md:75` says 27 | `README.md:224,187,554`; `server.json:14` | LOW |
| S-13 | `careagents` `turns` dict never evicted (unbounded per-account growth); `MAX_LIVE_CONVERSATIONS`/`CONVERSATION_IDLE_SECONDS` reference behavior that does not exist (#218 comment is false) | `careagents/app.py:115,632`, `:63-64` | LOW |
| S-14 | `/fasten/jobs/<id>/retry` emits no audit event | `r6/fasten/routes.py:172-199` | LOW |

## 2. Before → after

**Before** (today): guarantees enforced by convention at each of ~100 sites.

```
request ──► before_request hooks (r6_blueprint ONLY; 8 other blueprints get none)
        ──► each handler re-reads tenant (32 sites, 4 defaulting strategies)
        ──► each handler re-decides step-up (401 in 4 places, 403 in 3, for the
            same failure; (bool,str) tuple mis-used twice)
        ──► each handler re-picks a redaction profile (4 profiles, no selector,
            3 read paths apply none)
        ──► each handler re-emits audit (41 sites, 2 call conventions,
            2 transaction semantics, 5 unaudited 404 paths)
        ──► db.session / clock / os.environ touched directly (no seams)
```

**After**: one deep module per property; routes declare intent, the kernel
enforces it.

```
request ──► AccessKernel (r6/access.py, NEW)
              tenant_from_request()      ← the ONLY tenant reader
              require_write_grant(...)   ← the ONLY step-up gate (no tuple)
              audit(ctx, ...)            ← flush-only, joins the caller's txn
        ──► handler (thin: parse, delegate, respond)
        ──► fhir_response(payload, profile=...)  ← the ONLY FHIR exit;
              Profile.{STANDARD, PATIENT_CONTROLLED, INTAKE, RAW_INTERNAL}
        ──► seams: ResourceStore (db), Clock, HttpPort — injectable, defaulted
```

`r6/routes.py` (3,762 lines, 20 concerns) becomes a package of ~8 modules
registered onto the same blueprint via the **deps-dict pattern the file
already uses** at `r6/routes.py:3717-3762` — the seam exists; the parent
never adopted its own precedent. HTTP behavior does not change; the
conformance prober and `test_r6_routes.py` (94% HTTP-boundary) run
unmodified throughout.

## 3. Workstream A — the access kernel (highest leverage)

New file `r6/access.py` (~250 lines). Interfaces:

```python
# --- tenant: kills 8 copies of the extractor and 4 defaulting strategies ---
def tenant_from_request() -> str:
    """The only reader of X-Tenant-Id. Raises TenantRequired (=> 400
    OperationOutcome, code 'security') if absent/malformed. Never defaults,
    never reads the body or query string."""

# --- step-up: no tuple, no status-code choice at the call site ---
class StepUpDenied(Exception):
    """reason: str; http_status: 401 (absent/invalid) or 403 (present but
    insufficient scope/audience/operation). Decided HERE, once."""

@dataclass(frozen=True)
class WriteGrant:
    tenant_id: str
    audience: str | None
    operation: str | None
    nonce_consumed: bool

def require_write_grant(*, audience: str | None = None,
                        operation: str | None = None,
                        consume_nonce: bool = False) -> WriteGrant:
    """Wraps validate_step_up_token. Returns a grant or raises StepUpDenied.
    There is nothing to mis-destructure and nothing to coerce to bool."""

# --- audit: one function, one transaction semantic ---
def audit(*, tenant_id, event_type, resource_type, resource_id,
          agent_id=None, outcome="success", detail=None) -> None:
    """Flush-only; joins the caller's transaction (the r6/audit.py:49-54
    documented-but-unfollowed pattern becomes the only pattern).
    Read-misses audit too: event_type='read', outcome='not-found'."""
```

A blueprint-level `errorhandler(StepUpDenied)` renders the uniform
OperationOutcome — the 401/403 policy lives in one place. Registered on
**all** blueprints (actions, fasten, agent_runs, command_center, smbp, ops,
shc), closing the "hooks only cover r6_blueprint" gap.

Response shaping — new `r6/responses.py`:

```python
class Profile(Enum):
    STANDARD = auto()            # apply_redaction + disclaimer
    PATIENT_CONTROLLED = auto()  # apply_patient_controlled_redaction
    INTAKE = auto()              # today's private _intake_strip (routes.py:3316)
    RAW_INTERNAL = auto()        # explicit opt-out, spelled at the call site

def fhir_response(payload, *, profile: Profile, status=200):
    """The only exit for FHIR payloads. A path that applies no redaction now
    says Profile.RAW_INTERNAL out loud — greppable, reviewable (constitution
    rule 19: one control, one property)."""
```

Migration mechanics: `validate_step_up_token` and `record_audit_event` stay
as compat shims; call sites migrate per module. `record_audit_event`'s
ambient `db.session.commit()` (`r6/audit.py:93`, 41 call sites) is retired
last, after all writers use `audit()` + explicit commit.

Exact sites to migrate (from the audit): tenant reads
`r6/routes.py:218,297,463,549,628,848,1182,1210,1264,1316,1448,1501,1589,
1662,1703,1804,1845,2064,2145,2200,2342,2523,2589,2901,2972,3092,3245,3275,
3300,3365,3521,3598` + 8 sub-package copies (`r6/quality/routes.py:35`,
`r6/labs/routes.py:32`, `r6/caregaps/routes.py:36`, `r6/brief/routes.py:25`,
`r6/smbp/routes.py:28`, `r6/smbp/scheduler_routes.py:48`,
`r6/actions/routes.py:59-63`, `r6/fasten/routes.py:299-301`). Step-up:
`r6/routes.py:464,629,1216,2107,2973,3011,3368`,
`r6/actions/routes.py:255,468`, `r6/agent_runs/routes.py:53`,
`r6/command_center/routes.py:266`, `r6/read_auth.py:89`.

## 4. Workstream B — decompose `r6/routes.py`

Using the existing deps-dict registration (`routes.py:3717-3762`) as the
mechanism. Target package layout (concern → new module, line ranges from the
responsibility map):

| New module | Today's lines in routes.py |
|---|---|
| `r6/http/crud.py` | 440–726 (create/read/update, upstream+local branches) |
| `r6/http/search.py` | 94–197 (grammar) + 729–1160 |
| `r6/http/operations.py` | 1436–1831 ($stats, $lastn, $deidentify, …) |
| `r6/http/audit_api.py` | 143–160 + 1313–1432, 1835–1873, 2517–2560 |
| `r6/http/internal.py` | 1971–2513 (mint, purge, seed, ingest-bundle) |
| `r6/http/demo.py` | 2564–2877 |
| `r6/http/share.py` | 3316–3502 |
| `r6/http/panels.py` | 3221–3312 + 3507–3699 |
| (curatr stays) | 2881–3217 → thin wrappers over `r6/curatr.py` |

The search engine additionally extracts as a pure unit (430 lines →
~200-line engine + thin route):

```python
@dataclass(frozen=True)
class SearchQuery:
    resource_type: str
    params: Mapping[str, str]
    handling: Literal["strict", "lenient"]
    count: int
    sort: str | None
    summary: bool

def execute_search(q: SearchQuery, store: ResourceStore) -> SearchOutcome:
    """Pure: no request, no session, no clock. SearchOutcome carries entries,
    warnings, safe_ignored, has_unnamed — replacing the bare 3-tuple at
    routes.py:768 whose invariant only the caller holds."""
```

Known test edits this forces (planned, not incidental):
`tests/test_fhir_proxy.py:520,542,574,604,614,640,666,693,701` patch
`r6.routes.get_proxy*` by module path — the plan moves proxy dispatch into
`r6/http/crud.py` and updates these 9 patch targets in the same PR.
`tests/test_safe_modifier_tokens_drift.py:16` imports
`r6.routes._SAFE_MODIFIER_TOKENS` — the constant moves to
`r6/http/search.py` with a re-export shim until the drift test is updated.
`tests/test_ingest_bundle_endpoint.py:287` imports `_read_body_with_hard_cap`
— becomes the shared body-cap helper (Workstream D) and the import updates.

## 5. Workstream C — `tools.ts`: finish the registry it already is

The definition layer is already data-driven (29 registrations, one dispatch
map at `tools.ts:1130`); the execution layer is 27 hand-written methods with
three incompatible error idioms. Changes:

1. **Tier is the gate.** Delete the hardcoded name list at
   `tools.ts:1153-1157`; the central gate reads `tool.tier === "write"` for
   all nine write tools (today only 3 of 9 are gated centrally;
   `curatr_apply_fix` runs its own divergent check at `:1864-1871`, which is
   deleted).

   ```ts
   if (tool.tier === "write") {
     const stepUp = headerLookup(headers, "x-step-up-token");
     if (!stepUp) return stepUpRequiredResult(toolName);   // one shape
   }
   ```

2. **One error shape.** All 34 failure sites route through
   `backendFailureResult` (today 6 do; 17 discard the backend body entirely
   and bypass the sanitizer built to stop `FHIR_BASE_URL`/tenant leakage —
   idiom-B lines: `tools.ts:1397,1440,1469,1496,1519,1532,1668,1693,1749,
   1768,1798,1817,1888,1924,1950,1987,2010`).

3. **Registration gains `timeoutMs?: number`** — replaces the four bare
   positional overrides (`tools.ts:903,1794,1814,1885`).

4. **Shared `RESOURCE_TYPE_ENUM` const** — the 38-line enum is byte-identical
   at `tools.ts:259-296` and `:331-368` (md5-verified) and duplicated twice
   more in the manifest.

5. **Generate the manifest.** New `npm run manifest` emits
   `adapters/tools.manifest.json` from `getMCPToolSchemas()`; a jest test
   asserts deep equality so CI fails on drift. This makes the declared
   source of truth (`tests/test_docs_tool_catalogue_drift.py:13-14`) true,
   and fixes the nine existing content divergences including S-6.

6. **Close the `/mcp/rpc` bypasses** (S-7): `context/get` and `tools/call`
   on the RPC bridge route through `executeMCPTool` like every other path.

Refactor-survivability is unusually good here: `tools.test.ts:275-291`
("uses the listed registry as the single dispatch source") plus the ~90
URL-pinning execution tests verify the rewrite. Expected edits: the five
hardcoded 29/27 counts (`tools.test.ts:177,191,2103,2331,2556`) only if the
tool set changes (it does not in this plan).

## 6. Workstream D — one ingest engine

Three callers of `_ingest_one` use three failure strategies
(SAVEPOINT-per-entry at `r6/routes.py:2431-2454`; full-rollback-per-line at
`r6/fasten/ingester.py:140-153`; **nothing** at `r6/shc/routes.py:255-257`).
Extract:

```python
def ingest_entries(entries, *, tenant_id, provenance,
                   session) -> IngestReport:
    """SAVEPOINT per entry (the ingest-bundle pattern — the one that
    survived the 2026-07-08 incident review). Failures recorded as opaque
    codes, never str(exc). Audit is flush-only per resource (_ingest_one
    already composes correctly). Caller owns the final commit."""
```

Callers: `internal/ingest-bundle`, fasten `stream_ingest`, SHC
`_ingest_bundle`. This deletes the S-4 defect structurally rather than
patching it a third time. `_ingest_one`'s tombstone-revival
(`ingester.py:303-304`) gains an explicit `revived` flag in the report and
a distinct audit event type.

## 7. Workstream E — CareAgents boundary

Priority order inside this stream:

1. **Contract-pin the fake** (cheapest, highest leverage — do first):

   ```python
   def test_fake_client_matches_real_client_surface():
       for name, real in inspect.getmembers(HealthClawClient, inspect.isfunction):
           if name.startswith("_"): continue
           fake = getattr(FakeClient, name)  # missing method fails here
           assert inspect.signature(fake) == inspect.signature(real), name
   ```

   Today the fake has drifted (param-name drift `rtype` vs `resource_type`,
   widened `**kwargs` signatures, `record_count` semantics collapsed,
   class-body `purged = []` shared across the whole session —
   `tests/test_careagents.py:1389,1314,1427-1433`). 143 tests exercise the
   fake while the real client sits at 40% coverage.

2. **One transport-error policy**: a private `_request()` helper inside
   `HealthClawClient` wraps every call in `HealthClawError` (today 10 of 23
   methods wrap; the comment at `healthclaw.py:118-119` claiming all do is
   false; `delete_connection`'s "never claim a deletion that did not happen"
   guarantee currently fails on transport errors).

3. **Funnel the strays**: `careagents/healthcheck.py:15-23` (hand-built URL
   + `X-Internal-Secret` over urllib) delegates to
   `HealthClawClient.agent_worker_health`; `templates/landing.html:65` drops
   the hard-coded `https://app.healthclaw.io` for the configured base; one
   `review_url(agent_id, action_id)` builder replaces the four constructions
   (`agent.py:187` is malformed today — no agent segment — and its output is
   silently discarded by `chat.js:69`).

4. **Delete or wire the dead**: `run_turn`/`run_turn_to_message`,
   `HealthClawClient.log_message`, `conversation_locks.py` (85 lines,
   imported only by a test), `MAX_LIVE_CONVERSATIONS`/
   `CONVERSATION_IDLE_SECONDS` (S-13 — either implement the eviction the
   comment promises, or remove the comment and cap `turns`).

Splitting the 990-line `create_app` closure into a service layer is
**deliberately deferred** — `app.py` is 84% covered through HTTP and its
churn (21 commits) is feature-driven, not defect-driven. Boundary integrity
first; decomposition when a feature next forces it.

## 8. Seams to introduce (opportunistically, per workstream)

Ranked by monkeypatch pressure measured in the suite:

1. **HTTP port** — 26 `monkeypatch.setattr('requests.request')` +
   ~30 scattered `patch(...requests/httpx...)`. Generalize
   `r6/actions/rails/__init__.py:_safe_request` (the one good wrapper, with
   its `outcome_unknown` classification) into `r6/httpport.py`; adopt in
   `r6/fasten/api.py:37`, `r6/fhir_proxy.py`, `r6/curatr.py` terminology
   calls as each workstream touches them.
2. **Clock** — two incompatible conventions already exist
   (`r6/actions/models.py:44` naive-UTC vs `r6/agent_runs/models.py:12`
   aware-UTC) plus ~40 raw `datetime.now`/`time.time` sites. New
   `r6/clock.py` with `utcnow()` (aware) and a test override; the
   security-critical site is `r6/stepup.py:234` (token expiry), currently
   testable only by monkeypatching `time.time` globally
   (`tests/test_claimed_properties_are_pinned.py:160`).
3. **Settings** — config read from `os.environ` at call sites (31×
   `PUBLIC_TENANTS`, 23× `READ_AUTH_ENABLED`, 18× mint secret …).
   `main.create_app(settings)` already accepts a mapping; modules migrate to
   it per workstream. Not a big-bang.

## 9. TDD strategy — pin first, mutate to verify, then move

**The anchor that never changes:** `r6/conformance/probes.py` — pure HTTP
through a uniform adapter, graded A/…/F, enforced by
`tests/test_guardrail_conformance.py:17` and the `$conformance` endpoint.
It must stay Grade A after every PR in this plan. `compliance-gates` and
`compose-smoke` CI jobs are equally refactor-invariant.

**New pinning tests written BEFORE any move (Phase 0):**

1. `tests/test_write_guard_matrix.py` — the §1b guard matrix from this
   audit as a parametrized table: for every route that mutates the store,
   assert exactly which guards fire (tenant / step-up / HITL / internal
   secret / audit). Divergences that are *deliberate* get a comment citing
   the reason; divergences that are defects (S-1…S-4) get `xfail(strict=True)`
   markers that flip to passing as each fix lands. This is the retro's
   lesson operationalized: the matrix makes "which controls guard this
   write" a single reviewable artifact.
2. `tests/test_step_up_status_codes.py` — pins today's 401s (create,
   update, share-bundle) so the kernel migration cannot silently change
   wire behavior; the 403 sites are pinned separately and the plan
   normalizes them **only with an explicit decision** (see Open Questions).
3. Audit-on-404 pins (S-9), GET-mutation pins (S-10), unredacted-update
   pin (S-11) — each with a `MUTATION:` docstring line naming the edit that
   must turn it red, per `docs/constitution.md` rule 20.
4. FakeClient parity test (Workstream E.1) — lands before any careagents
   change.
5. Manifest generation test (Workstream C.5) — lands with the generator.

**Mutation verification per phase** (constitution rule 20): after each
extraction, re-run the named mutations —
`drop apply_redaction from the search loop`, `delete the exp check in
stepup`, `remove record_audit_event from read` (already pinned in
`tests/test_claimed_properties_are_pinned.py`) — plus new ones per
workstream: `make require_write_grant return a grant without validating`,
`route one write tool around the tier gate`, `swap Profile.STANDARD for
RAW_INTERNAL in one read path`.

**Characterization debt to pay before touching** (from the coverage audit):
`r6/curatr.py` terminology validators (lines 505-760 effectively untested —
tests cut them off via `patch.object(engine, '_lookup_code')` ×8);
`r6/fasten/ingester.py` main body (47%); `r6/fasten/routes.py:515-692`
(~180 contiguous uncovered lines). Rule: **no extraction from a region
below ~75% line coverage until an HTTP-level characterization test covers
the paths being moved.** `r6/agent_client.py` (441 lines, 0% coverage, no
importers) is investigated and deleted or ticketed — not refactored.

**Known suite hazards, handled explicitly:** module-path patches in
`test_fhir_proxy.py` (9 sites) and private-symbol imports (34 sites,
inventoried in the audit) are updated in the same PR as the move they
break, never left to fail. `tests/test_guardrail_conformance.py`'s 11
private-function unit tests constrain any future split of `probes.py` —
out of scope here.

## 10. Sequencing

| Phase | Content | Depends on |
|---|---|---|
| 0 | S-1…S-5 security fixes (individual PRs) + Phase-0 pinning tests | — |
| 1 | Workstream A: access kernel + errorhandlers on all blueprints; migrate `r6/routes.py` CRUD + the 8 sub-package tenant extractors | 0 |
| 2 | Workstream B: routes.py package split (one module per PR, deps-dict registration; search engine extraction last) | 1 |
| 3 | Workstream C: tools.ts tier gate + error unification + manifest generator (S-6, S-7, S-8 land here) | 0 (independent of 1–2) |
| 4 | Workstream D: ingest engine (S-4 lands here if not fixed in 0) | 1 |
| 5 | Workstream E: careagents boundary (parity test first) | 0 (independent) |
| — | Seams (§8) | introduced inside whichever phase first touches each site |

Phases 3 and 5 are parallel-safe with 1–2 (different trees). Every PR:
suite green on 3.11, `uv run ruff check .`, conformance Grade A, postgres
lane green; no PR mixes a move with a behavior change (constitution:
feature and hardening ship together, but *moves* ship alone).

## 11. Deliberately not doing

- **No framework change, no async rewrite, no service extraction.** The
  monolith's problem is internal shape, not deployment shape.
- **Not splitting `careagents/app.py` into a service layer yet** (§7).
- **Not touching `openclaw/bot.py`** (23% coverage, 991 lines) — it needs
  characterization tests before any refactor is honest; separate effort.
- **Not renaming public HTTP paths, headers, or error bodies** except where
  a divergence is itself the defect (S-list) and the change is the fix.
- **Not "fixing" the 403→401 divergence silently** — see Open Questions.

## 12. Open questions for the owner

1. **Step-up status normalization:** the same failure yields 401 at 4 sites
   and 403 at 3 (`routes.py:2107,2973,3011` +
   `r6/actions/routes.py`/`review.py`). The kernel wants one policy
   (proposal: 401 absent/invalid, 403 valid-but-insufficient). External
   clients may have learned today's mixed behavior. Approve the
   normalization, or pin the mixed behavior permanently?
2. **S-2 `/fasten/demo`:** gate it with the mint secret like its siblings,
   or delete it (the seeded demo path via `/internal/seed` covers the use)?
3. **S-10 GET-mutations:** convert `$curatr-evaluate` persistence to
   opt-in (`?persist=true` + POST) or accept and document?
4. **Ops sweep scope (S-1):** should `/r6/ops/*` become internal-secret
   auth (like `/internal/*`) instead of tenant step-up?
