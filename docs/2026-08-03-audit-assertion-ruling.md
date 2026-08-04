# Ruling: the audit assertion's false-positive class (#321)

**Date:** 2026-08-03
**Author:** CTO
**Status:** ruling, ready for Dev
**Blocks:** access-kernel slices 12 and 13 (`docs/2026-08-03-access-kernel-spec.md` §2.7)

## Verdict

A design exists that satisfies all three constraints. Ship it.

`audit()` must stop setting the pending marker. A SQLAlchemy `after_flush`
listener sets it instead, and only when an `AuditEventRecord` is actually in
the flush. Set and clear then run on the same mechanism: three session events,
no ambient proxies, no allowlist.

Slices 4 to 11 are **not** blocked and should proceed now. Details in the last
section.

I measured every claim below against the running suite. The prototype is
reverted; no production file changed in this branch.

## What I reproduced

Two false positives, one true positive, on SQLAlchemy 2.0.46 with
Flask-SQLAlchemy 3.1.1.

| Scenario | Current code, assertion installed | Correct outcome |
|---|---|---|
| `audit()` with a faked `r6.audit.add_audit_event`, nothing else | **raises** | pass |
| Same, inside a request that also reads the database | **raises** | pass |
| `audit()` flushes a real row, request returns with no commit or rollback | raises | raise |
| `audit()` then `db.session.commit()` | pass | pass |
| `audit()` then `db.session.rollback()` | pass | pass |

The mechanism in the issue is correct. `Session.rollback()` with no active
transaction returns before it emits `after_soft_rollback`, so the marker set at
`r6/access.py:492` survives a rollback that had nothing to roll back.

Blast radius, measured by registering `install_audit_assertions(flask_app)` in
`main.py` and running the whole suite:

- current code: 2 failed, 2260 passed. The two are
  `test_neither_audit_assertion_is_installed_yet` (the pin, expected) and
  `test_audit_forwards_every_field_to_the_shipped_writer` (the false positive).
- with the change below: 1 failed, 2261 passed. Only the pin.

So the false-positive class is one test wide today and zero tests wide after
the fix. It is not one test wide in slice 12, when 41 call sites start using
`audit()` and their tests start faking the writer.

### Correction to the issue: `in_transaction()` was rejected for the wrong reason

The issue says gating the raise on `db.session.in_transaction()` breaks the
true positive. It does not, and the expression as written cannot run at all.

1. `db.session` is a `scoped_session`. SQLAlchemy 2.0.46 does not proxy
   `in_transaction` onto it. `db.session.in_transaction()` raises
   `AttributeError` inside the teardown. `is_active` is proxied, and it is
   `True` in every scenario above, so a gate on `is_active` changes nothing.
2. Written correctly as `db.session().in_transaction()`, the gate keeps the
   true positive red and removes the reported false positive. All 72 kernel
   tests pass.

The real reason to reject it is different and worse. Measured at teardown:

| Scenario | `session.in_transaction()` |
|---|---|
| real row flushed, not committed | True |
| faked writer, nothing else in the request | False |
| **faked writer, after any real query in the same request** | **True** |

Every production handler reads the database before it audits. Under an
`in_transaction()` gate the guard fires on a faked writer inside any handler
that reads, which is all of them. The gate degrades to "this request touched
the database and did not resolve it", a property the audit assertion has no
business asserting. I confirmed this end to end: with the gate applied, a
`POST` handler that queries, then calls `audit()` with a faked writer, still
raises.

Recording this so nobody re-derives the gate from the issue text and concludes
the issue was simply wrong.

## The ruling: observe the row, not the call

The marker must be written by the same kind of event that clears it. A flush
that carries an `AuditEventRecord` is the effect the assertion is about. The
call to `audit()` is not.

### Change 1: `audit()` stops setting the pending marker

`r6/access.py:491-493`

```python
    if has_app_context():
        setattr(g, _AUDIT_EMITTED, True)
```

`_AUDIT_EMITTED` stays where it is. `install_read_audit_assertion` needs to
know that `audit()` ran, which is a different question from whether a row is
pending, and the two markers must not merge.

### Change 2: an `after_flush` listener sets the pending marker

New module-level import next to the other `r6` imports:

```python
from r6.models import AuditEventRecord
```

No import cycle: `r6.access` already imports `r6.audit` at module scope, and
`r6.audit` imports `r6.models`. Verified by running the suite.

New listener, placed immediately above `_clear_pending_marker`:

```python
def _set_pending_marker(session_, _flush_context, *_args) -> None:
    """An AuditEventRecord is going to the database in an open transaction."""
    if not has_app_context():
        return
    if any(isinstance(obj, AuditEventRecord) for obj in session_.new):
        setattr(g, _AUDIT_PENDING, True)
```

`session.new` is read inside `after_flush`, where SQLAlchemy still holds it in
its pre-flush state. This is not the spec's original condition, which read
`session.new` at teardown, after `flush()` had already emptied it. Constraint 3
is satisfied: nothing outside this listener consults `new` or `dirty`.

Register it in `_install_marker_listeners`:

```python
    event.listen(db.session, 'after_flush', _set_pending_marker)
```

### Change 3: a savepoint rollback must not clear the marker

`after_soft_rollback` fires for a `SAVEPOINT` rollback as well as an outer one.
Measured: a handler that flushes an audit row and then calls
`db.session.begin_nested().rollback()` clears the marker and passes, with the
audit row still pending in the outer transaction. That is a false negative in
the current design, and it runs straight through
`record_audit_event`'s failure path (`r6/audit.py:105`), which is the exact
function slices 12 and 13 migrate away from. Shipping the guard with a masking
path through the code being migrated is not sound.

```python
def _clear_on_soft_rollback(_session, previous_transaction=None) -> None:
    if previous_transaction is not None and previous_transaction.nested:
        return
    _clear_pending_marker()
```

```python
    event.listen(db.session, 'after_soft_rollback', _clear_on_soft_rollback)
```

`after_commit` keeps `_clear_pending_marker` unchanged. It only fires for the
outermost commit, so it needs no nesting check.

Do **not** try to write this check as `session.in_transaction()` inside the
clear listener. I measured that variant: inside `after_commit` the session
still reports a transaction, so it breaks
`test_committing_or_rolling_back_satisfies_the_assertion` and
`test_the_assertion_is_inert_for_a_request_that_never_audited`.
`previous_transaction.nested` is the discriminator.

### Change 4: register the assertion

`main.py:281-283`

```python
    from r6.access import register_error_handlers, install_audit_assertions

    register_error_handlers(flask_app)
    install_audit_assertions(flask_app)
```

Register it in the same PR as changes 1 to 3, before any `audit()` adoption, so
the guard is in place before the first migrated site rather than arriving with
it. `install_read_audit_assertion` stays unregistered; it goes red on the five
S-9 paths and that is slice 12x.

Three artifacts describe the current state and must change with it:

- `main.py:274-280`, the inline comment explaining why neither assertion is
  installed. Rewrite for `install_read_audit_assertion` only.
- `tests/test_access_kernel.py:1233`,
  `test_neither_audit_assertion_is_installed_yet`. Narrow it to the read
  assertion and rename it.
- `docs/2026-08-03-access-kernel-spec.md` §2.7, slice 12. It says the guard is
  inert for unmigrated sites "because only `audit()` writes the `g` marker".
  After this change the marker is written by any `AuditEventRecord` flush. The
  guard is still inert for unmigrated sites, but for a different reason:
  `record_audit_event` commits, and a commit clears the marker. Fix the stated
  reason, because the next person will rely on it.

### Required tests

Add to `tests/test_access_kernel.py` alongside the existing §1.3.1 block:

1. A faked `r6.audit.add_audit_event` in a request that audits and nothing
   else. Assert 201, no `AuditAssertionError`. This is the #321 regression.
2. The same, in a handler that runs a real query before it audits. Assert 201.
   This is the regression for the `in_transaction()` gate.
3. A handler calling `r6.audit.add_audit_event` directly, with no commit.
   Assert it raises. This pins the widened coverage in change 2.
4. A handler that audits, then rolls back a `SAVEPOINT`, then returns. Assert
   it raises. This pins change 3.

`test_a_flushed_audit_row_that_is_never_committed_fails_the_request` stays
exactly as it is and stays red.

### Evidence

- 72 of 72 in `tests/test_access_kernel.py` pass with changes 1 to 3.
- 76 of 76 pass with the four new tests added.
- Full suite with changes 1 to 4: 1 failed, 2261 passed, 12 skipped, 6 xfailed.
  The single failure is the pin test, which change 4 replaces.
- `uv run ruff check .`: clean.

## What this design still does not catch

Named so that nobody reads a green suite as a stronger claim than it is.

1. **Flush, roll back, answer 2xx.** A handler that audits, rolls back, and
   returns 201 loses the audit row behind a success. The marker clears and the
   assertion passes. Measured. This is the failure the guard's own docstring
   describes, and this control does not cover it. See follow-up A.
2. **Requests only.** The assertion is a `teardown_request`. CLI commands,
   the durable worker, and background threads audit without it.
3. **Test-time only.** It is active under `TESTING` or
   `HC_ASSERT_AUDIT_COMMITTED`. Production is unprotected by design.
4. **One session.** The listeners attach to `db.session`. A row flushed
   through a second session or engine is invisible.
5. **Failing requests are skipped.** When `exc is not None` the teardown
   returns early, so a request that both errors and strands an audit row is not
   reported. Correct, since masking the real error is worse, but it is a gap.
6. **Content is not checked.** The guard says a row reached the database. It
   says nothing about whether the row describes the access, names the right
   tenant, or keeps `detail` free of PHI.
7. **The ambient commit itself.** `after_commit` clears the marker whether the
   caller committed deliberately or `record_audit_event` committed on its
   behalf. The guard protects against dropping the row. It does not detect the
   ambient commit that slice 13 removes.

## Alternatives, and why they lose

**Gate the raise on transaction state.** Rejected. Measured to fire on a faked
writer in any handler that reads first, which is every handler. Full reasoning
above.

**Gate the marker set on transaction state inside `audit()`.** Same defect,
same measurement, one call earlier. `in_transaction()` after the writer is
`True` whenever the handler read anything before auditing, so the marker is set
with no row behind it.

**Track the specific `AuditEventRecord` instance.** Rejected for two reasons.
It reads the writer's return value, so a fake that returns a `Mock` sets the
marker and a fake that returns `None` suppresses it. That re-couples the
control to the call, which is the defect. Instance state also fails to
discriminate. The object is persistent and in the identity map both before and
after a commit, so you still need the commit and rollback events. That is the
chosen design plus a second mechanism.

**Accept a narrower property**, such as asserting only on paths in an opt-in
list. Rejected. An allowlist added to a control that fires wrongly is how the
control stops meaning anything, which is the defect shape in
`docs/2026-08-02-retro.md`. The property is cheap to state correctly, so state
it correctly.

**Read `session.new` or `session.dirty` at teardown.** Ruled out before this
ruling and confirmed here. Measured empty in all five scenarios, including the
true positive.

## Do slices 4 to 11 proceed?

Yes. They are not blocked, and holding them costs more than it buys.

Slices 4 to 8 migrate `require_grant`. Slices 9 to 11 migrate
`tenant_from_request`. Neither calls `audit()`, neither changes transaction
semantics, and both are observed by `tests/test_write_guard_matrix.py` at the
HTTP boundary rather than by the audit assertion. The audit assertion is inert
for them: it needs an `AuditEventRecord` in a flush, and these slices add none.

The slice-9 work is also the highest-value item in the whole kernel. Four
blueprints accept any string as a tenant id today
(`r6/smbp/routes.py:27-28`, `r6/wearables/routes.py:253`,
`r6/shc/routes.py:201`, `r6/fasten/routes.py:180,299,350`). Blocking a live
input-validation gap on an unrelated test-harness defect is the wrong trade
three weeks before the webinar.

Two conditions:

1. Land the fix and the registration before slice 12, not before slice 4. The
   guard must exist before the first `audit()` adoption, which is where it
   starts doing work.
2. If any slice between 4 and 11 turns out to touch an audit call site, it
   stops and comes back to me. None should, based on the spec's site lists.

## Follow-ups to file

**A. A committed audit row is not required for a successful write.** Gap 1
above. The shape of the answer is a second assertion, parallel to
`install_read_audit_assertion`: on a 2xx answer to a mutating request on a
`/r6/fhir/` resource path, require that an audit row was flushed **and**
committed. That needs a third marker, set by `after_commit` when the pending
marker was live. It is a new control with its own red-on-arrival slice, not a
change to this one. File it, do not build it before Aug 18.

**B. The audit guard does not run outside a request.** Gap 2. The durable
worker and the CLI audit with no assertion at all. Scope it after the webinar.

Neither is a blocker for slices 12 and 13.
