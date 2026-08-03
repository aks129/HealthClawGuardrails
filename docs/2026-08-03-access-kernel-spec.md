# Access kernel — interface spec and migration order

Status: **design spec. No production code written.** Supersedes §3 of
[2026-08-02-architecture-audit-and-refactor-plan.md](2026-08-02-architecture-audit-and-refactor-plan.md)
where the two disagree.

Context: the founder overruled the "defer past Aug 18" sequencing. The
refactor proceeds now, on one condition he stated himself — carefully and
intentionally, not break-fix. This document answers *how*, and resolves the
two design objections that blocked the kernel as originally specified.

Every file:line below was verified against `origin/main` at `516cf5b`.

---

## 0. Blocking precondition: the safety net is red

`tests/test_write_guard_matrix.py` is the characterization baseline the whole
plan rests on. It does not pass on `main`:

```text
$ uv run python -m pytest tests/test_write_guard_matrix.py -q
10 failed, 162 passed, 6 skipped, 4 xfailed
```

CI has been red on `main` since the matrix merged (`cd3f4dc`, run
30814856634, and every push since). The cause is the drift shape from the
retro, not a bad test:

| Failure | Cause |
|---|---|
| 8 × `[ops-reap]` | #308 changed `/r6/ops/*` from tenant step-up to `internal_secret_authorized()` (`r6/ops/routes.py:49-56`). The matrix row still declares `TENANT_HEADER, TENANT_FORMAT, STEP_UP` (`tests/test_write_guard_matrix.py:531-541`). |
| `test_wearables_sync_only_touches_the_authenticated_tenant` | #315 scoped the sweep (`r6/wearables/routes.py:267`). The `xfail(strict=True)` at `:1226` now XPASSes. |
| `test_shc_ingest_never_logs_a_raw_exception` | #309 fixed the log line (`r6/shc/routes.py:260`). The `xfail(strict=True)` now XPASSes. |

Three security fixes landed and the artifact that describes their behavior
was not updated in the same PR. That is the same failure that made the
Playwright gate meaningless for a day: a gate that cannot tell "failed" from
"never ran" is not a gate.

**Nothing in section 2 may start until this is green.** A red baseline
cannot detect a regression, and "the matrix stays green" is the only rule
that makes the migration un-rushable.

---

## 1. Final interface signatures

One new module: `r6/access.py`. It is the only importer of `r6.stepup`,
`r6.redaction` and `r6.audit` once migration completes.

### 1.0 A hard rule the kernel must follow internally

The kernel resolves its collaborators by **module attribute**, never by
`from X import name`:

```python
from r6 import audit as _audit_mod
from r6 import redaction as _redaction_mod
from r6 import stepup as _stepup_mod
```

Reason: 33 test references target `r6.stepup` and 13 target
`r6.routes.get_proxy_for_request` by module path. A `from r6.stepup import
validate_step_up_token` inside `r6/access.py` binds at import time, and
`monkeypatch.setattr('r6.stepup.validate_step_up_token', ...)` then patches
an object the kernel no longer consults. The tests stay green and stop
testing anything. This is the quietest way the migration can fail, and it
costs one line to avoid.

### 1.1 Tenant

```python
class TenantSource(Enum):
    """Where an endpoint is willing to learn its tenant from.

    Not a ranking and not a security level. Naming the source is what makes
    "this endpoint reads the tenant from the body" a reviewable fact instead
    of a line of code someone has to find.
    """
    SESSION = "session"   # command-center signed-link cookie (TENANT_SESSION_KEY)
    HEADER  = "header"    # X-Tenant-Id
    QUERY   = "query"     # ?tenant_id= / ?tenant= / ?t=
    BODY    = "body"      # {"tenant_id": ...}
    SHARP   = "sharp"     # synthesized from X-FHIR-Server-URL (routes.py:223-228)
    DEFAULT = "default"   # a literal fallback the endpoint supplies


@dataclass(frozen=True)
class Tenant:
    """A tenant id that passed format validation, plus where it came from.

    Carrying the source lets an audit row and a log line say which input
    selected the tenant. It does NOT mean the caller is authorized to act on
    that tenant — see require_grant and r6.read_auth.
    """
    id: str
    source: TenantSource


class TenantRejected(Exception):
    """The request did not supply a usable tenant id.

    reason: 'absent' | 'malformed'. The two map to different FHIR
    OperationOutcome codes ('security' vs 'invalid'), both at HTTP 400,
    matching r6/routes.py:230-247 exactly.
    """
    def __init__(self, reason: str): ...


def tenant_from_request(
    *,
    sources: tuple[TenantSource, ...] = (TenantSource.HEADER,),
    default: str | None = None,
    query_keys: tuple[str, ...] = ("tenant_id",),
    body_key: str = "tenant_id",
) -> Tenant:
    """Return the tenant this request names, in the caller's declared order.

    THE ONE PROPERTY: the returned id matched ``[A-Za-z0-9_-]{1,64}`` and
    came from a source this endpoint declared it accepts. Nothing else.

    ``sources`` is an ORDERED tuple, not a set. The first source in the tuple
    that yields a non-empty value wins. A set would force the kernel to
    invent a precedence, and the invented one differs from
    r6/command_center/routes.py:76-82 (session, then query, then header) —
    adopting it would silently change which tenant that page renders when a
    caller sends both. Ordering is per endpoint because today it already is.
    (This corrects the ``allow=frozenset`` shape proposed in the CTO review.)

    Raises TenantRejected('absent') when no declared source yields a value
    and ``default`` is None. Raises TenantRejected('malformed') when a source
    yields a value that fails the pattern — including a value that came from
    ``default``, so a typo'd literal fails loudly rather than at query time.

    TenantSource.DEFAULT is only consulted when ``default`` is not None, and
    it is always last regardless of its position in ``sources``.
    """
```

### 1.2 Scope, Grant, require_grant

```python
class Scope(Enum):
    """What a step-up token must be able to authorize.

    TENANT_BOUND maps to validate_step_up_token(require_scope=None) and
    accepts a read-scoped token. WRITE maps to require_scope='write' and
    rejects one. The names say what the check does, not what the caller
    wishes it did — the exact mistake behind _RESOURCE_ID_PATTERN.
    """
    TENANT_BOUND = "tenant-bound"   # r6/read_auth.py:88-93 needs this
    WRITE        = "write"          # every write gate today


@dataclass(frozen=True)
class Grant:
    """Proof that a step-up token authorized THIS tenant for THIS scope.

    Frozen and carrying tenant_id so a handler scopes its query to
    grant.tenant_id rather than re-reading a header. It does not make the
    handler do that — see §3(e).
    """
    tenant_id: str
    scope: Scope
    audience: str | None
    operation: str | None
    nonce_consumed: bool


class StepUpDenied(Exception):
    """A step-up check ran and refused. Rendered by the app errorhandler.

    THE ONE PROPERTY: ``checked is True`` means require_grant decided this,
    on the single line in this module that passes checked=True. Any other
    StepUpDenied — raised by a helper, a test double, a copy-paste, or a
    future bug — carries checked=False, and the errorhandler RE-RAISES it so
    it surfaces as a 500.

    Without that flag the errorhandler is a hazard: it would convert an
    unexpected raise from anywhere in the request into a clean 401, which
    reads to a client exactly like a working guard. That is the retro's
    defect shape with an HTTP status on it.
    """
    def __init__(self, reason: str, *, http_status: int, checked: bool = False):
        ...


def require_grant(
    *,
    scope: Scope,
    tenant: Tenant,
    audience: str | None = None,
    operation: str | None = None,
    consume_nonce: bool = False,
    also_bearer: bool = False,
    also_body_field: str | None = None,
    absent_status: int = 401,
    rejected_status: int = 401,
) -> Grant:
    """Require a step-up token bound to ``tenant`` and return the Grant.

    THE ONE PROPERTY: a Grant exists only if validate_step_up_token returned
    True for this tenant, scope, audience and operation. There is no tuple to
    mis-destructure, no truthy object to test, and no status-code choice at
    the call site.

    Token sources are read in this order. First X-Step-Up-Token. Then
    Authorization: Bearer when ``also_bearer`` (r6/read_auth.py:83-86,
    r6/actions/routes.py:256-260). Then the named body field when
    ``also_body_field`` (r6/routes.py:2097-2098). Each is opt-in for the same
    reason TenantSource is: a reader must be able to see which inputs an
    endpoint trusts without reading the helper.

    ``absent_status`` / ``rejected_status`` exist because the same failure
    answers 401 at 9 sites and 403 at 3 (r6/routes.py:2976-2994, 3020-3023,
    r6/wearables/routes.py:257-261). Normalizing them is open question 1 in
    the plan and it is the founder's call, not this migration's. Each
    migrated site passes the status it answers TODAY. After every site is
    migrated, normalization becomes a one-line default change plus deleting
    the overrides — one reviewable PR, made deliberately.

    Raises StepUpDenied(checked=True). Never returns None, never returns
    False, never returns a tuple.
    """
```

Enforcement of the `checked` flag is a test, not a convention:
`tests/test_access_kernel.py` asserts the literal `checked=True` appears
exactly once in the repository and that its line number falls inside
`require_grant`. Greppable, and it fails the moment someone copies it.

`StepUpDenied` must subclass `Exception`, not `BaseException`. Flask calls
`handle_user_exception` from inside `except Exception` in
`full_dispatch_request`, so a `BaseException` subclass would bypass the
errorhandler entirely and reach the WSGI server. That is why the broad-except
AST guard in §4.1 is mandatory rather than nice to have.

```python
def register_error_handlers(app) -> None:
    """Register the StepUpDenied and TenantRejected renderers app-wide.

    HANDLERS ONLY. This registers no before_request hook and changes no
    blueprint's request pipeline. r6/sdc/delivery.py:8-11 is deliberately off
    r6_blueprint because the HMAC signature in the URL is the credential and
    the route must work with no headers. Extending the hooks would break it.
    An errorhandler is inert until something raises.

    A StepUpDenied with checked=False is re-raised here, unhandled, and
    becomes a 500.
    """
```

### 1.3 Audit

```python
def audit(
    *,
    tenant: Tenant | str,
    event_type: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    agent_id: str | None = None,
    context_id: str | None = None,
    outcome: str = "success",
    detail: str | None = None,
    outcome_detail_code: str | None = None,
) -> None:
    """Add and flush an AuditEvent inside the CALLER's transaction.

    THE ONE PROPERTY: when this returns, an AuditEvent row is flushed in the
    caller's unit of work, and it will be committed or rolled back with the
    state change it describes. It never commits.

    This is r6/audit.py:45-59 (add_audit_event) becoming the only pattern.
    r6/audit.py:62-113 (record_audit_event) opens a SAVEPOINT and then calls
    db.session.commit() at :92 — an ambient commit at 41 call sites, which
    means a caller cannot know whether its own pending work was committed as
    a side effect of auditing.

    ``detail`` stays PHI-free. The kernel does not sanitize it, because a
    sanitizer that silently drops PHI is worse than a reviewer who sees the
    string. Composition of the detail line stays where the facts are.

    Records the flush on ``g`` for assert_audit_committed (§1.3.1).
    """
```

#### 1.3.1 The flush-only hazard needs its own control

```python
def install_audit_assertions(app) -> None:
    """Testing-mode only: fail a request that flushed audit rows and never
    resolved the transaction.

    THE ONE PROPERTY: no request leaves an audit row flushed-but-uncommitted.

    Installed as a teardown_request, active only when app.config['TESTING']
    or HC_ASSERT_AUDIT_COMMITTED is set. It fires when g carries a flush
    record AND db.session.new or db.session.dirty is non-empty at teardown —
    the signature of a handler that called audit() and then returned without
    commit() or rollback(). A rollback path leaves both empty and passes,
    correctly: a refused write should carry no audit row.

    Without this, moving 41 sites from an ambient commit to a caller-owned
    commit is 41 chances to drop an audit row behind a 201.
    """


def install_read_audit_assertion(app) -> None:
    """Testing-mode only: fail a FHIR read that returned without auditing.

    THE ONE PROPERTY: a request to a /r6/fhir/ resource route that answered
    200 or 404 flushed at least one AuditEvent.

    This is the ONLY structural answer to S-9, the five unaudited 404 paths
    (r6/routes.py:596-598, 692-694, 1268-1270, 1810-1812, 3106-3110). audit()
    is a function you must call, so the kernel alone cannot make an early
    ``return 404`` audit itself.

    It lands in its own slice and it goes red on those five paths
    immediately. That redness IS the S-9 pin. Do not land it in the same PR
    as the fix.
    """
```

### 1.4 Response shaping

```python
class Profile(Enum):
    """Which redaction policy produced this payload.

    There is deliberately NO member meaning "no redaction". An enum member
    for the absence of a property makes the absence look like a policy, and
    a reviewer scanning for the risky case sees a valid-looking constant. The
    opt-out is a different function with a different signature — see
    unredacted_response.
    """
    STANDARD           = "standard"            # r6/redaction.py:22 + add_disclaimer
    PATIENT_CONTROLLED = "patient-controlled"  # r6/redaction.py:180
    INTAKE             = "intake"              # r6/routes.py:3318 _intake_strip


def fhir_response(payload, *, profile: Profile, status: int = 200,
                  resource_type: str | None = None, etag: str | None = None):
    """The only exit for a FHIR payload that carries patient data.

    THE ONE PROPERTY: the body was produced by the named redaction profile.

    Profile.STANDARD calls apply_redaction, which strips every upstream
    ``display`` and ``CodeableConcept.text`` and then re-applies labels from
    r6/terminology.py keyed by code (r6/redaction.py:22-38). The kernel does
    not reorder that. It only removes the choice of whether to call it.
    """


#: Endpoint names permitted to answer with an unredacted payload. Adding a
#: name here is a deliberate two-file change: this frozenset, and the literal
#: expected set in tests/test_unredacted_exits.py.
_UNREDACTED_EXITS: frozenset[str] = frozenset({
    # populated slice by slice; each entry names its reason in the test
    "r6.subscription_topics",   # SubscriptionTopic is server metadata (routes.py:1671)
    "r6.audit_search",          # AuditEventRecord is PHI-free by construction
})


def unredacted_response(payload, *, endpoint: str, reason: str,
                        status: int = 200):
    """Answer with a payload no redaction profile touched.

    THE ONE PROPERTY: ``endpoint`` appears in _UNREDACTED_EXITS. Raises
    RuntimeError otherwise, at request time, in every environment.

    ``reason`` is a required sentence for the reviewer and is never sent to
    the client. A required argument nobody can leave blank is what turns
    "this path applies no redaction" from an omission into a claim.

    r6/routes.py:719-723 (update_resource returning the stored resource) is
    NOT on the allowlist. It is S-11, a defect, and it migrates to
    fhir_response(profile=Profile.STANDARD) with its own PR and its own pin.
    """


def outcome_response(severity: str, code: str, diagnostics: str, *,
                     status: int):
    """Build a FHIR OperationOutcome. This is r6/routes.py:3705-3716 with a
    status attached.

    Separate from fhir_response because an OperationOutcome carries text this
    system authored, never stored patient data. Running it through a
    redaction profile would be a no-op that teaches a reader the wrong thing
    about what fhir_response protects.

    ``diagnostics`` must not contain a token, an id from another tenant, or a
    driver exception string.
    """
```

---

## 2. Migration order

The founder's constraint is no break-fix. The order below is built so that
reverting any single PR restores the previous behavior with no other change,
and so that no PR can be "finished" by editing the test that caught it.

### 2.0 The per-PR contract

Every slice from 1 onward obeys all five. A PR that cannot is split.

1. `uv run python -m pytest tests/test_write_guard_matrix.py -q` reports
   **0 failed and 0 xpassed**. An XPASS is a failure here: it means a defect
   was fixed and its row was not updated, which is exactly how `main` got red.
2. **The diff to `tests/` in a migration PR is empty.** If a test must
   change, the migration is wrong or the test was pinning an accident. Split
   it: land the test change first, against the OLD code, and show it still
   passes. Then migrate. This single rule is what makes break-fix visible as
   a scope violation rather than a judgement call.
3. **One guard, one blueprint.** A diff touching two blueprints is two PRs.
4. The PR body pastes a mutation result. Break the migrated line, name the
   test that went red, restore it. Constitution rule 20.
5. `git revert <sha>` restores the prior behavior with no follow-up edit.
   State this in the PR body, having tried it.

Full gates on every PR, unchanged: `uv run python -m pytest tests/ -q`,
`uv run ruff check .`, conformance Grade A, the Postgres lane.

### 2.1 Slice 0 — turn the matrix green (FIRST, and it is not optional)

**This is the safest possible starting point because it touches no
production code at all.** It edits one test file to match three fixes that
already merged.

- `tests/test_write_guard_matrix.py:531-541` — the `ops-reap` row becomes
  `frozenset({INTERNAL_SECRET})`, `anon_refusal=(403,)`,
  `internal_secret_status=403`, `step_up_missing_status=None`, and the
  `defect_issue="#304"` is removed. Per `r6/ops/routes.py:49-56`.
- `tests/test_write_guard_matrix.py:1183-1229` — remove both `#304`
  `xfail(strict=True)` markers. `r6/wearables/routes.py:267` now passes
  `tenant_id=`.
- `tests/test_write_guard_matrix.py:1264-1305` — remove the `#306`
  `xfail(strict=True)`. `r6/shc/routes.py:260` now logs
  `type(exc).__name__`.
- `tests/test_write_guard_matrix.py:186-187` — update the ASCII table rows.

Verification is mechanical: 172 passed, 0 failed, 0 xpassed. Confirm the
next `main` CI run is green before opening slice 1.

Then file the process issue: three security PRs (#308, #309, #315) changed
guard behavior without touching the artifact that describes it. Rule 2 above
is the fix, and it needs to be in `docs/agent-task-guide.md`, not only here.

### 2.2 Slice 0.5 — put the safety net in the Postgres lane

`.github/workflows/ci.yml:119-131` runs an explicit subset against Postgres.
`tests/test_write_guard_matrix.py` is not in it, and neither is
`tests/test_audit_transactions.py`. Add both.

Reason: slices 12 and 13 change transaction semantics at 41 sites. SQLite's
in-memory engine and Postgres differ on savepoint and flush visibility, and
this repo has shipped three Postgres-only defects that SQLite CI could never
see (`ci.yml:69-80`). Migrating audit semantics while the matrix runs on
SQLite only is measuring the wrong lane.

Zero production code. Reverting is deleting two lines.

### 2.3 Slice 1 — the kernel, adopted by nothing

Add `r6/access.py` with every signature in §1, and
`tests/test_access_kernel.py`. Import it from **no** production module.

Risk is zero by construction, and a test proves it:

```python
def test_the_kernel_is_not_adopted_yet():
    """MUTATION: import r6.access from r6/routes.py -> red."""
```

Ships in the same PR, because they are the same unit:

- The broad-except AST guard (§4.1). It must exist before the first
  adoption, so it can never be "added later" to a codebase that already
  violates it.
- The `checked=True` uniqueness test (§1.2).
- `tests/test_unredacted_exits.py` pinning `_UNREDACTED_EXITS` as empty-plus-
  the-two-metadata-endpoints.

### 2.4 Slice 2 — register the handlers, still inert

`main.py:267 _register_request_hooks` calls `register_error_handlers(app)`
and `install_audit_assertions(app)`.

Behaviorally inert: nothing raises `StepUpDenied` or calls `audit()` yet.
Proven by two tests, one of which registers a throwaway route that raises
`StepUpDenied(checked=False)` and asserts 500 — the property that makes the
errorhandler safe to have at all.

Handlers only. No `before_request` hook is added to any blueprint.
`r6/sdc/delivery.py:93` stays on its own blueprint.

### 2.5 Slices 3–8 — `require_grant`, one blueprint at a time

Ordering rule: **migrate matrix-covered sites first, uncovered sites last.**
A site with no row in `MATRIX` has no safety net, so it needs a
characterization test written first — which by rule 2 is a separate PR.

| # | Sites | Why here |
|---|---|---|
| 3 | `r6/smbp/routes.py:70-76` (`reading`) | **The first real migration.** One call site, one blueprint, the majority 401/401 dialect so no status override is needed, two matrix rows (`smbp-reading`, `smbp-enroll`) plus `test_smbp_reading_emits_an_audit_event:1075`. And smbp is NOT on `r6_blueprint`, so it proves the app-wide errorhandler reaches the blueprints the hooks never covered — the gap the whole plan exists to close. |
| 4 | `r6/wearables/routes.py:256-261` | One site. Carries `rejected_status=403`, proving the minority dialect survives migration without being normalized. Row: `wearables-sync-now`. |
| 5 | `r6/actions/routes.py:256-265`, `:354`, `:464-471`, `r6/actions/review.py:66` | Exercises `also_bearer`, `audience`, `operation`, `consume_nonce`. Four matrix rows. Note the matrix already found a site the audit's line list missed (`commit`, `routes.py:354`) — migrate from the matrix, not from the plan. |
| 6 | `r6/routes.py:464-472` (create), `:629-637` (update), `:3370-3382` (share-bundle) | Three rows, all 401, all `parses_body_before_step_up` pinned. |
| 7 | `r6/routes.py:1215-1231` (`$ingest-context`), `:2976-2994` + `:3010-3023` (curatr) | The flag-conditional gate (S-3) and the 403 dialect with a two-phase nonce consume. Migrate the SHAPE only. S-3's fail-open stays fail-open in this PR and is fixed in its own, so the diff shows one thing. |
| 8 | `r6/read_auth.py:88-93` → `Scope.TENANT_BOUND` | **Last, deliberately.** It is called from the `before_request` hook at `r6/routes.py:269-270` on every GET and from `authenticate_tenant_read` at `:250`. Widest blast radius in the file. It is also the site that proved `require_write_grant` needed a scope parameter: `require_scope=None` here is load-bearing, and `tests/test_patient_connect_token.py:126-130` goes red if a write-grant replaces it. |

Deferred out of this group with a reason: `r6/agent_runs/routes.py:53`
(`validate_step_up_token(...)[0]`, S-5) and
`r6/command_center/routes.py:266`. Both are in `NON_CLINICAL_MUTATORS`
(`tests/test_write_guard_matrix.py:569-585`) and have no matrix row. They
also turn a boolean helper into an exception, which changes control flow.
They need a characterization test first. Their current pin is
`test_no_write_path_indexes_the_step_up_tuple:1307`, which stops the idiom
spreading but does not describe these two handlers' behavior.

### 2.6 Slices 9–11 — `tenant_from_request`

| # | Sites | Notes |
|---|---|---|
| 9 | `r6/smbp/routes.py:27-28`, `r6/wearables/routes.py:253`, `r6/shc/routes.py:201`, `r6/fasten/routes.py:180,299,350` | HEADER-only, and every one of them accepts any string today. This slice is where "four blueprints accept any string as a tenant id" stops being true. Highest value per line in the whole kernel. |
| 10 | The 30 header-only reads in `r6/routes.py` (`:297, 463, 549, 628, 848, 1182, 1210, 1264, 1316, 1448, 1501, 1589, 1662, 1703, 1804, 1845, 2344, 2525, 2903, 2974, 3094, 3367, 3523, 3600` and the sub-package copies at `r6/quality/routes.py:35`, `r6/labs/routes.py:32`, `r6/caregaps/routes.py:36`, `r6/brief/routes.py:25`, `r6/smbp/scheduler_routes.py:48`, `r6/actions/routes.py:60`) | Mechanical. Split by blueprint per rule 3. `r6/routes.py` itself is one blueprint, so split it by route group instead — six PRs of five sites each, not one of thirty. |
| 11 | The multi-source sites, ONE PER PR | These are the 13 that make the "never defaults, never reads the body or query" contract false. Each has a different order and each is a behavior risk. |

Slice 11, itemized:

| PR | Site | Declared sources |
|---|---|---|
| 11a | `r6/routes.py:3246-3250, 3276-3280, 3301-3305` (mcp-apps) | `(HEADER, QUERY)`, no default, empty string tolerated today |
| 11b | `r6/routes.py:2066` (`issue_step_up_token`) | `(BODY, HEADER, DEFAULT)`, `default='default'` |
| 11c | `r6/routes.py:2147` (`purge_tenant_route`) | `(BODY, HEADER)`, no default |
| 11d | `r6/routes.py:2202` (`seed_tenant`) | `(BODY, HEADER, DEFAULT)`, `default='desktop-demo'` |
| 11e | `r6/routes.py:2094` (`bind_telegram_chat`) | `(BODY,)` |
| 11f | `r6/routes.py:2591` (`demo`) | `(HEADER, DEFAULT)`, `default='demo-tenant'` |
| 11g | `r6/command_center/routes.py:76-82, 129-133, 262-265` | `(SESSION, QUERY, HEADER, DEFAULT)`, `default='desktop-demo'` — the site that forced `sources` to be a tuple |
| 11h | `r6/sdc/delivery.py:99` | `(QUERY,)`, `query_keys=('t',)`. Reachable with no headers by design. |
| 11i | `r6/wearables/routes.py:120, 232-234` | `(QUERY,)` and `(QUERY, HEADER)` |
| 11j | `r6/agent_runs/routes.py:84` | `(BODY, HEADER)` |
| 11k | `r6/routes.py:218-228` (the `before_request` hook, incl. SHARP synthesis) | `(HEADER, SHARP)`. **Last of the eleven.** It runs on every request to the largest blueprint and it mutates `request.environ` at `:228`. |

After 11k, and only then: add the grep guard that fails if
`request.headers.get('X-Tenant-Id')` appears outside `r6/access.py`.

### 2.7 Slices 12–14 — audit, then responses

| # | Content | Notes |
|---|---|---|
| 12 | `audit()` adopted per blueprint, `record_audit_event` kept as a shim | `install_audit_assertions` is already registered (slice 2) and is inert for unmigrated sites, because only `audit()` writes the `g` marker. So the guard arrives with the first migrated site automatically. |
| 12x | `install_read_audit_assertion` | Its own PR. Goes red on the five S-9 paths on arrival. That redness is the pin, and the fix is a separate PR after it. |
| 13 | Retire `record_audit_event`'s ambient commit (`r6/audit.py:92`) | Last of the audit work, after every writer owns its commit. Postgres lane matters most here. |
| 14 | `fhir_response` / `outcome_response` / `unredacted_response` | Read paths first, per blueprint. S-11 (`r6/routes.py:719-723`) is a behavior FIX, so it ships alone with its own pin, not inside a shaping migration. |

Response shaping goes last on purpose. Step-up and tenant are what the write-
guard matrix observes at the HTTP boundary. Redaction is observed by a
different test population (`tests/test_r6_routes.py`, the conformance
prober). Do the matrix-covered work while the matrix is the freshest and
best-understood artifact in the repo.

---

## 3. Stop the bleeding — what the kernel actually fixes

Assessed honestly. Two of these six are not access-kernel problems and
building the kernel will not stop them.

**(a) Four step-up status dialects — DETECTABLE, NOT IMPOSSIBLE.**
`require_grant` makes the status a parameter of one function instead of a
literal at 13 sites (`r6/routes.py:466, 471, 631, 636, 1228, 2111, 2114,
2979, 2993, 3022, 3380`, `r6/wearables/routes.py:258, 261`,
`r6/smbp/routes.py:72, 76`, `r6/actions/routes.py:262, 265`). It does not
stop a handler writing `return jsonify(...), 418`. What catches a fifth
dialect: the matrix's per-row `step_up_missing_status`, plus an AST guard
that fails when a 401 or 403 literal appears in the same function body as a
`require_grant` call. Add that guard in slice 3.

**(b) Five unaudited 404 paths — THE KERNEL DOES NOT FIX THIS.**
`audit()` is a function you must remember to call. An early `return 404`
above it stays unaudited forever. The structural answer is
`install_read_audit_assertion` (§1.3.1), which is a request-lifecycle
assertion, not a kernel primitive. Say this out loud in the PR: the kernel
standardizes how you audit, not whether you did.

**(c) Four tenant-defaulting strategies — IMPOSSIBLE, after slice 11k.**
`tenant_from_request(sources=...)` makes the strategy a declared argument. A
new endpoint that wants a fifth input has to add a `TenantSource` member,
which is a one-line diff in `r6/access.py` that appears in every review. The
grep guard on `request.headers.get('X-Tenant-Id')` closes the bypass. This is
the one class where "structurally impossible" is the honest word, and it is
only true once every one of the 13 multi-source sites has migrated. Until
then it is exactly as true as it is today.

**(d) A clinical write with no HITL gate (`/r6/smbp/reading`) — THE KERNEL
DOES NOT FIX THIS, AND MUST NOT PRETEND TO.**
`enforce_human_in_loop` is a `before_request` on `r6_blueprint`
(`r6/routes.py:321-326`, `r6/health_compliance.py:91-131`). smbp is a
different blueprint, so `POST /r6/smbp/reading` writes a clinical
Observation with a step-up token and nothing else (`r6/smbp/routes.py:64-98`).
The fix is not to extend the hook — `r6/sdc/delivery.py:8-11` must stay off
it, and the header the hook checks is the known-weak `X-Human-Confirmed`
(#214) that CLAUDE.md forbids building new paths on. The real control is the
action rail's approval endpoint (`actions-confirm`, matrix row at
`tests/test_write_guard_matrix.py:390-403`). **#214 stays the gap after this
refactor ships.** Interim control: extend the matrix so any row writing a
type in `CLINICAL_RESOURCE_TYPES` must carry `HITL`. That is a test change
and it belongs to whoever takes #214.

**(e) The #304/#311 class, "auth scoped one way, action scoped another" —
THE KERNEL DOES NOT FIX THIS.**
`require_grant` returns `Grant.tenant_id`. Nothing forces the handler to put
it in the query. `r6/ops/routes.py` and `r6/wearables/routes.py:267` were
both fixed by hand, and a third site would be too. Partial help: `Grant` is
frozen and carries the tenant, so `run_once(app, tenant_id=grant.tenant_id)`
is the shortest path rather than a re-read. The real control is the matrix's
`TENANT_FILTER` guard, which today is asserted by two hand-written tests
(`:1191`, `:1230`) for two rows out of 22 that claim it. Recommend a QA
slice: turn `TENANT_FILTER` into a parametrized assertion across every row
that declares it. That is worth more than any kernel primitive here.

**(f) Four blueprints accepting any string as a tenant id — IMPOSSIBLE,
after slice 9.** `tenant_from_request` validates the format unconditionally
and there is no path through it that returns an unvalidated id. Today
`r6/smbp/routes.py:27-28`, `r6/fasten/routes.py:180`, `r6/shc/routes.py:201`
and `r6/wearables/routes.py:253` return whatever the header said. The matrix
already records this: "Four rows carry TENANT_HEADER without TENANT_FORMAT"
(`tests/test_write_guard_matrix.py:190-191`). One slice, four files, and the
claim becomes true.

Summary the founder should carry into the next planning conversation: the
kernel structurally closes **two** of six recurring classes (c and f), makes
one detectable (a), and leaves three (b, d, e) needing controls that are not
kernel primitives. That is still worth doing. It is not "the refactor stops
the bleeding".

---

## 4. What could still go wrong

### 4.1 The 99 broad-except blocks

`grep -rn "except Exception" --include='*.py' r6/` returns 99, twelve of them
in `r6/routes.py`. Any one wrapped around a `require_grant` call swallows
`StepUpDenied` and lets the handler fall through into the write. The guard
would look like it fired. The response would be a 200.

Guard, shipped in slice 1 before any adoption:

```python
def test_no_guard_call_sits_inside_a_swallowing_try():
    """AST walk of r6/**/*.py. Fail when a Call to require_grant, audit,
    tenant_from_request or unredacted_response is a descendant of a Try whose
    ExceptHandler catches Exception or BaseException and whose body contains
    no bare `raise`.

    MUTATION: wrap the require_grant call in r6/smbp/routes.py in
    `try: ... except Exception: pass` -> red.
    """
```

It must land before the first adoption. A guard added after the violations
exist gets an allowlist, and an allowlist is where this class comes back.

### 4.2 The flush-only commit hazard

Moving 41 sites off `record_audit_event`'s ambient `db.session.commit()`
(`r6/audit.py:92`) is 41 chances to flush a row and never commit it. The
response is still 201. The test still passes.

Guards: `install_audit_assertions` (§1.3.1), plus the Postgres lane from
slice 0.5. Also do not delete `record_audit_event` until slice 13 — a shim
that still works means a half-migrated blueprint is never in a broken state.

### 4.3 Tests coupled to module paths and private symbols

63 `monkeypatch`/`patch` calls target `r6.*` module paths, and 28 test
imports reach for a private symbol (`r6.fasten.ingester._ingest_one`,
`r6.rate_limit._rate_limits`, `r6.actions.registry._clear`,
`r6.fasten.ingester._RESOURCE_ID_PATTERN`).

Two distinct risks:

1. **The quiet one.** If `r6/access.py` does `from r6.stepup import
   validate_step_up_token`, the 33 tests that patch `r6.stepup.*` keep
   passing while patching nothing. §1.0 is the rule that prevents it, and
   slice 1 should carry a test that proves the patch still reaches the
   kernel.
2. **The loud one.** Nothing in slices 0–14 MOVES a symbol. `r6/routes.py`
   stays one file. The audit's Workstream B is what breaks these 63 patch
   targets, and it is deferred (§5). Keep it that way until the kernel is
   done, so a red test during migration always means a behavior change and
   never a moved file.

`tests/test_audit_transactions.py:4` imports `_ingest_one` directly and is
the closest test to slice 13's semantics. Add it to the Postgres lane in
slice 0.5.

### 4.4 The SQLite / Postgres lane split

The full suite runs on `sqlite:///:memory:`. Only ten files run against
Postgres (`ci.yml:119-131`), and `tests/test_write_guard_matrix.py` is not
one of them. Every transaction-semantics change in slices 12 and 13 is
therefore verified in the wrong engine.

Guard: slice 0.5, before any kernel adoption. If the Postgres lane's runtime
becomes the objection, the answer is to drop something else from it, not to
skip the matrix.

### 4.5 The one I cannot guard

`require_grant` is a call the handler must make. Nothing in Flask forces a
handler to call it, and a new route with no gate at all is still one line of
code. The only control for that is the matrix's census
(`test_every_mutating_route_is_classified:1407`), which forces a new
mutating endpoint into `MATRIX` or into `NON_CLINICAL_MUTATORS` with a
stated reason. That test is more important than anything in `r6/access.py`.
Protect it.

---

## 5. Scope boundary

### What the kernel deliberately does NOT do

- **Authorization policy.** Who may read a tenant stays in
  `r6/read_auth.py:56-99`. The kernel asks "does a valid token exist for
  this tenant and scope", never "should this person see this".
- **Human-in-the-loop.** #214 is untouched. See §3(d).
- **Internal-secret gates.** `_internal_mint_authorized` and
  `_internal_ingest_authorized` stay two helpers with two policies, on
  purpose (`tests/test_write_guard_matrix.py:265-286`). One of them exempts
  public tenants and the other must not. Collapsing them into the kernel
  would be exactly the "one control, two behaviors" defect.
- **Webhook HMAC.** `/fasten/webhook` and `/r6/actions/callback/<provider>`
  authenticate differently, and the callback's secret arrives as a query
  parameter (`tests/test_write_guard_matrix.py:433-437`). A header-oriented
  kernel must not assume it can absorb them.
- **Tenant filtering of queries.** See §3(e).
- **Status-code normalization.** Open question 1. Founder's call, one PR,
  after every site is migrated.
- **Seams.** No clock, no HTTP port, no settings object. Those are §8 of the
  plan and they are a different kind of change: they alter what a module
  depends on, not what a guard guarantees.

### What waits, and why

- **Workstream B, the `r6/routes.py` split.** Wait. It moves 3,762 lines,
  breaks the 63 module-path patches in §4.3, and buys readability rather
  than a safety property. Doing it during the kernel migration means every
  red test has two possible causes. Do it after, and only in regions above
  75% line coverage — `r6/fasten/routes.py:515-692` and
  `r6/fasten/ingester.py` fail that bar today.
- **Workstream C, `tools.ts`.** Wait *in this queue*, but not in calendar
  time. It has the worst defect density in the repo (32 commits, 8 fixes),
  S-6 makes every OpenAI and Gemini model call fail Ajv today, and #296 has
  four tool descriptions instructing a model to call a withheld tool on the
  write path. It is a different tree with a different test suite and it does
  not depend on the kernel. Recommendation: run it in parallel, with someone
  else, starting with the manifest generator. Not part of this spec.
- **Workstream D, the ingest engine.** Wait until after slice 13. One shared
  `ingest_entries` is only safe once `audit()`'s flush-only contract is the
  single pattern. Building it first means building it twice.
- **Workstream E, the CareAgents boundary.** The FakeClient parity test
  (plan §7.1) should land **now**, out of band. It is test-only, it is zero
  product risk, and it makes the constitution's "a fake proves a call is
  made, not accepted" mechanical instead of aspirational. Everything else in
  that workstream waits — CareAgents is a separate app with a separate
  database and it shares no code with `r6/access.py`.

### What "scope for vision impact" buys here

The claim in the webinar deck is that the safety properties are measured,
not asserted. Today that claim rests on the conformance prober plus a
write-guard matrix that has been red since it merged. After slice 11 the
claim has a second mechanism behind it: there is one tenant reader, one
step-up gate, and a table that says which controls guard which write. That
is a defensible sentence on a slide, and it is true.

Slices 0, 0.5, 1, 2 and 3 are the ones worth doing before Aug 18. They are
five PRs, four of which touch no production behavior at all. Slices 4
onward are good work with no webinar dependency, and rushing them to make a
date is precisely the failure the founder asked to stop.
