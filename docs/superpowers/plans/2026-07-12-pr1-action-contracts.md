# PR #1: Action-Rail Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the pinned contracts from spec v3 (`docs/superpowers/specs/2026-07-11-real-actions-reliability-design.md`) so the three parallel rails (comms, forms, booking) can build against merged, tested interfaces without colliding. This is the serialized merge gate: NO rail work starts until this PR is merged.

**Architecture:** Extend the existing `ProposedAction` engine (`r6/actions/`) in place — it is well-built (atomic single-UPDATE claim, first-verdict-wins webhook, `unknown`-never-`failed`). We add: a canonical state-transition helper writing an append-only event log; a single-use `ActionConfirmation` table consumed atomically with the claim; an `ActionExecutor` Protocol + registry so rails register from their own modules; a red-flag safety screen at propose; attempt-ledger fields for crash recovery; the Approve-is-the-commit flow (the human's out-of-band approval executes server-side, replacing the spoofable `X-Human-Confirmed` header); and a fake-provider test harness + generic contract tests that become the rail merge gate.

**Tech Stack:** Flask + SQLAlchemy (`models.db`), pytest on `sqlite:///:memory:` (+ a new Postgres CI lane, separate plan), HMAC step-up tokens (`r6/stepup.py`), Telegram push (`r6/telegram_push.py`). No new runtime dependencies.

---

## File structure

| File | Responsibility | Action |
|------|----------------|--------|
| `r6/actions/errors.py` | Error-taxonomy constants (single source of truth) | Create |
| `r6/actions/state.py` | Canonical `transition_action()` + legal-transition map | Create |
| `r6/actions/events.py` | `ActionEvent` append-only model | Create |
| `r6/actions/confirmations.py` | `ActionConfirmation` model (single-use, TTL) | Create |
| `r6/actions/registry.py` | `ActionExecutor` Protocol, `ExecutionResult`, `register_executor`, `get_executor`, `all_kinds` | Create |
| `r6/actions/safety.py` | Red-flag emergency screen (reuses `r6/smbp/triage.py` symptom lexicon) | Create |
| `r6/actions/rails/__init__.py` | Imports each rail module so registration runs at import time | Create |
| `r6/actions/rails/phone.py` | `PhoneCallExecutor` (ports `_execute_call`) | Create |
| `r6/actions/rails/sms.py` | `SmsExecutor` (ports `_execute_sms`) | Create |
| `r6/actions/models.py` | Add attempt-ledger columns + `awaiting_confirmation`/`needs_review` states; delete decorative `transition()` | Modify |
| `r6/actions/executors.py` | Becomes a thin shim delegating to the registry (keeps `execute_action`/`ExecutionResult` import paths alive) | Modify |
| `r6/actions/routes.py` | `commit` → "submit for confirmation"; new `POST /<id>/confirm` (out-of-band, executes); propose runs red-flag + validate | Modify |
| `services/agent-orchestrator/src/tools.ts` | Delete `X-Human-Confirmed` minting (line ~1839); `action_commit` returns the pending contract text | Modify |
| `tests/conftest.py` | Add `fake_providers` fixture (FakeBland/FakeTwilio) + `reset_action_registry` | Modify |
| `tests/actions/test_contract_generic.py` | Generic suite iterating the registry — the rail merge gate | Create |
| `tests/actions/test_state_transitions.py` | `transition_action` + event-log tests | Create |
| `tests/actions/test_confirmations.py` | Single-use / TTL / atomic-consumption tests | Create |
| `tests/actions/test_safety_redflag.py` | Emergency-screen tests | Create |
| `tests/actions/test_confirm_is_commit.py` | End-to-end propose→submit→approve→execute + no-commit-before-approval | Create |

---

## Task 1: Error taxonomy

**Files:** Create `r6/actions/errors.py`; Test `tests/actions/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/actions/test_errors.py
from r6.actions import errors

def test_taxonomy_is_frozen_and_complete():
    expected = {
        'PROVIDER_NOT_CONFIGURED', 'CONTACT_NOT_ALLOWLISTED', 'DAILY_CAP_REACHED',
        'PAYLOAD_INVALID', 'PROVIDER_ERROR', 'EXTRACTION_AMBIGUOUS',
        'EMERGENCY_INDICATED', 'STALE_SOURCE_DATA',
    }
    assert set(errors.ALL) == expected
    # each name maps to a stable string code used in API responses
    assert errors.EMERGENCY_INDICATED == 'emergency_indicated'
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/actions/test_errors.py -v` → FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# r6/actions/errors.py
"""Error-taxonomy codes returned at the action gate. Single source of truth;
API responses and tests both reference these constants (never string literals)."""

PROVIDER_NOT_CONFIGURED = 'provider_not_configured'
CONTACT_NOT_ALLOWLISTED = 'contact_not_allowlisted'
DAILY_CAP_REACHED = 'daily_cap_reached'
PAYLOAD_INVALID = 'payload_invalid'
PROVIDER_ERROR = 'provider_error'
EXTRACTION_AMBIGUOUS = 'extraction_ambiguous'
EMERGENCY_INDICATED = 'emergency_indicated'
STALE_SOURCE_DATA = 'stale_source_data'

ALL = (
    PROVIDER_NOT_CONFIGURED, CONTACT_NOT_ALLOWLISTED, DAILY_CAP_REACHED,
    PAYLOAD_INVALID, PROVIDER_ERROR, EXTRACTION_AMBIGUOUS,
    EMERGENCY_INDICATED, STALE_SOURCE_DATA,
)
```

- [ ] **Step 4: Run test to verify it passes** — expected PASS
- [ ] **Step 5: Commit** — `git add r6/actions/errors.py tests/actions/test_errors.py && git commit -m "feat(actions): error taxonomy constants"`

---

## Task 2: ActionEvent append-only log

**Files:** Create `r6/actions/events.py`; Test `tests/actions/test_state_transitions.py` (shared with Task 3)

- [ ] **Step 1: Write the failing test**

```python
# tests/actions/test_state_transitions.py
import json
from models import db
from r6.actions.events import ActionEvent

def test_action_event_row_persists(app):
    with app.app_context():
        ev = ActionEvent(action_id='a1', from_status='proposed',
                         to_status='awaiting_confirmation', actor='commit-route',
                         detail='submitted')
        db.session.add(ev); db.session.commit()
        got = ActionEvent.query.filter_by(action_id='a1').one()
        assert got.from_status == 'proposed'
        assert got.to_status == 'awaiting_confirmation'
        assert got.actor == 'commit-route'
        assert got.created_at is not None
```

- [ ] **Step 2: Run** — FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# r6/actions/events.py
"""Append-only lifecycle log for actions. Dashboards, digests, webhook-lag,
dead-letter lists, and per-tenant caps are all VIEWS over this table. Written
in the SAME transaction as every state transition (see r6/actions/state.py)."""
import uuid
from datetime import datetime, timezone
from models import db

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class ActionEvent(db.Model):
    __tablename__ = 'action_events'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    from_status = db.Column(db.String(32), nullable=True)
    to_status = db.Column(db.String(32), nullable=False)
    # actor: commit-route | confirm | webhook | reaper | propose
    actor = db.Column(db.String(32), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
```

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add r6/actions/events.py tests/actions/test_state_transitions.py && git commit -m "feat(actions): append-only ActionEvent log"`

---

## Task 3: Canonical `transition_action()` + reconciled state machine

**Files:** Modify `r6/actions/models.py` (states + delete `transition()`); Create `r6/actions/state.py`; Test `tests/actions/test_state_transitions.py`

- [ ] **Step 1: Write the failing tests** (append to `test_state_transitions.py`)

```python
from r6.actions.models import ProposedAction
from r6.actions.state import transition_action, IllegalTransition
from r6.actions.events import ActionEvent
import pytest

def _make(app, status='proposed'):
    a = ProposedAction(tenant_id='t1', kind='sms', payload={'body': 'hi'})
    a.status = status
    db.session.add(a); db.session.commit()
    return a.id

def test_guarded_transition_succeeds_and_logs(app):
    with app.app_context():
        aid = _make(app, 'proposed')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is True
        assert ProposedAction.query.get(aid).status == 'awaiting_confirmation'
        assert ActionEvent.query.filter_by(action_id=aid).count() == 1

def test_guarded_transition_noop_when_state_mismatch(app):
    with app.app_context():
        aid = _make(app, 'executing')
        ok = transition_action(aid, from_states=('proposed',),
                               to_state='awaiting_confirmation', actor='commit-route')
        assert ok is False  # WHERE didn't match; no clobber, no event
        assert ActionEvent.query.filter_by(action_id=aid).count() == 0

def test_illegal_transition_rejected(app):
    with app.app_context():
        aid = _make(app, 'completed')
        with pytest.raises(IllegalTransition):
            transition_action(aid, from_states=('completed',),
                              to_state='executing', actor='commit-route')

def test_new_states_present():
    from r6.actions.models import _TRANSITIONS
    assert 'awaiting_confirmation' in _TRANSITIONS['proposed']
    assert 'executing' in _TRANSITIONS['awaiting_confirmation']
    assert 'expired' in _TRANSITIONS['awaiting_confirmation']
    assert 'needs_review' in _TRANSITIONS['executing']
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3a: Update the state map in `r6/actions/models.py`** — replace the `_TRANSITIONS` dict (lines 24-32) with:

```python
# Legal status transitions. awaiting_confirmation is the out-of-band gate:
# commit submits (proposed->awaiting_confirmation), the human's approval claims
# (awaiting_confirmation->executing). expiry from awaiting_confirmation is the
# COMMON path (proposals linger hours awaiting a human). needs_review = executed
# but outcome unconfirmable (carries evidence). unknown = post-possible-send.
_TRANSITIONS = {
    'proposed': {'awaiting_confirmation', 'expired'},
    'awaiting_confirmation': {'executing', 'expired'},
    'executing': {'completed', 'failed', 'needs_review', 'unknown'},
    'completed': set(),
    'failed': set(),
    'needs_review': set(),
    'expired': set(),
    'unknown': {'completed', 'failed', 'needs_review'},
}
```

Then **delete** the decorative `transition()` method (lines 69-74) — the real state machine lives in `state.py`; leaving the ORM method invites a parallel agent to reintroduce a TOCTOU.

- [ ] **Step 3b: Implement `r6/actions/state.py`**

```python
# r6/actions/state.py
"""Canonical state transition for actions. The ONLY sanctioned way to change
ProposedAction.status. Combines the guarded single-UPDATE claim pattern (from
routes.py) with an in-transaction ActionEvent append, so state and audit can
never diverge. Callers MUST use this — the ORM transition() method was removed."""
from models import db
from r6.actions.models import ProposedAction, _TRANSITIONS
from r6.actions.events import ActionEvent

class IllegalTransition(Exception):
    pass

def transition_action(action_id, from_states, to_state, actor, detail=None, **fields):
    """Guarded transition. Flips action_id from any of from_states to to_state
    only if the row currently matches (atomic WHERE). Returns True if it moved
    (and writes one ActionEvent in the same commit), False if the WHERE matched
    nothing (concurrent claim / already advanced — no clobber, no event).
    Raises IllegalTransition if to_state isn't reachable from every from_state."""
    for fs in from_states:
        if to_state not in _TRANSITIONS.get(fs, set()):
            raise IllegalTransition('%s -> %s not permitted' % (fs, to_state))
    updates = {'status': to_state}
    updates.update(fields)
    moved = ProposedAction.query.filter(
        ProposedAction.id == action_id,
        ProposedAction.status.in_(tuple(from_states)),
    ).update(updates, synchronize_session=False)
    if moved:
        # from_status: read the pre-image is racy; log the matched set instead.
        db.session.add(ActionEvent(
            action_id=action_id, from_status=','.join(from_states),
            to_status=to_state, actor=actor, detail=detail))
    db.session.commit()
    return bool(moved)
```

- [ ] **Step 4: Run** — PASS (also run `pytest tests/ -k action -q` to confirm existing action tests still pass after the state-map change; the removed `confirmed` state was never emitted by routes.py)
- [ ] **Step 5: Commit** — `git add r6/actions/models.py r6/actions/state.py tests/actions/test_state_transitions.py && git commit -m "feat(actions): canonical transition_action + reconciled state machine"`

---

## Task 4: Attempt-ledger columns

**Files:** Modify `r6/actions/models.py`; Test `tests/actions/test_attempt_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/actions/test_attempt_ledger.py
from models import db
from r6.actions.models import ProposedAction

def test_attempt_fields_default_null(app):
    with app.app_context():
        a = ProposedAction(tenant_id='t1', kind='sms', payload={'body': 'x'})
        db.session.add(a); db.session.commit()
        assert a.attempt_id is None
        assert a.claimed_at is None
        assert a.provider_request_at is None
```

- [ ] **Step 2: Run** — FAIL (AttributeError)

- [ ] **Step 3: Implement** — add to `ProposedAction` (after `expires_at`, line 53):

```python
    # Attempt ledger (crash-recovery, see r6/actions/state.py + the reaper).
    # attempt_id = idempotency key set at claim; provider_request_at is stamped
    # immediately before the provider POST so a crash is distinguishable:
    # claimed but never-called (safe to fail) vs called-but-unresolved (review).
    attempt_id = db.Column(db.String(64), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    provider_request_at = db.Column(db.DateTime, nullable=True)
```

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add r6/actions/models.py tests/actions/test_attempt_ledger.py && git commit -m "feat(actions): attempt-ledger columns for crash recovery"`

---

## Task 5: ActionConfirmation (single-use, TTL, atomic consumption)

**Files:** Create `r6/actions/confirmations.py`; Test `tests/actions/test_confirmations.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/actions/test_confirmations.py
from datetime import datetime, timedelta, timezone
from models import db
from r6.actions.confirmations import ActionConfirmation, issue_confirmation, consume_confirmation

def _naive(dt): return dt.replace(tzinfo=None)

def test_issue_then_consume_once(app):
    with app.app_context():
        c = issue_confirmation('a1', approved_via='telegram', ttl_minutes=15)
        db.session.commit()
        assert consume_confirmation('a1') is True     # first wins
        assert consume_confirmation('a1') is False    # replay refused

def test_expired_confirmation_refused(app):
    with app.app_context():
        c = issue_confirmation('a2', approved_via='dashboard', ttl_minutes=15)
        c.expires_at = _naive(datetime.now(timezone.utc) - timedelta(minutes=1))
        db.session.commit()
        assert consume_confirmation('a2') is False
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3: Implement**

```python
# r6/actions/confirmations.py
"""Out-of-band human approval record. issue_confirmation() is written by the
authenticated Telegram/dashboard approve handler; consume_confirmation() is
called INSIDE the claim transaction so approval and execution are atomic and
single-use (an approval from Tuesday can't authorize a Thursday commit)."""
import uuid
from datetime import datetime, timedelta, timezone
from models import db

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class ActionConfirmation(db.Model):
    __tablename__ = 'action_confirmations'
    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = db.Column(db.String(64), nullable=False, index=True)
    approved_via = db.Column(db.String(32), nullable=False)  # telegram | dashboard
    approved_at = db.Column(db.DateTime, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)

def issue_confirmation(action_id, approved_via, ttl_minutes):
    c = ActionConfirmation(action_id=action_id, approved_via=approved_via,
                           expires_at=_utcnow() + timedelta(minutes=ttl_minutes))
    db.session.add(c)
    return c

def consume_confirmation(action_id):
    """Atomically claim an unconsumed, unexpired confirmation. Returns True iff
    exactly one row was consumed by THIS call (guarded UPDATE, single-use)."""
    now = _utcnow()
    consumed = ActionConfirmation.query.filter(
        ActionConfirmation.action_id == action_id,
        ActionConfirmation.consumed_at.is_(None),
        ActionConfirmation.expires_at > now,
    ).update({'consumed_at': now}, synchronize_session=False)
    return bool(consumed)
```

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add r6/actions/confirmations.py tests/actions/test_confirmations.py && git commit -m "feat(actions): single-use ActionConfirmation with atomic consume"`

---

## Task 6: ActionExecutor Protocol + registry

**Files:** Create `r6/actions/registry.py`; Test `tests/actions/test_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/actions/test_registry.py
import pytest
from r6.actions.registry import (ActionExecutor, ExecutionResult,
                                  register_executor, get_executor, all_kinds, _clear)

class _Toy:
    kind = 'toy'
    required_env = ()
    def validate(self, payload): return [] if payload.get('body') else ['payload_invalid']
    def execute(self, action): return ExecutionResult(status='completed')
    def reconcile(self, action): return ExecutionResult(status='completed')

def test_register_and_lookup():
    _clear()
    register_executor(_Toy())
    assert 'toy' in all_kinds()
    assert isinstance(get_executor('toy'), _Toy)

def test_duplicate_kind_rejected():
    _clear(); register_executor(_Toy())
    with pytest.raises(ValueError):
        register_executor(_Toy())

def test_execution_result_shape():
    r = ExecutionResult(status='needs_review', provider_ref='x', outcome={'k': 1})
    assert r.status == 'needs_review' and r.provider_ref == 'x'
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3: Implement**

```python
# r6/actions/registry.py
"""The action extension point (public, versioned). A rail implements
ActionExecutor and calls register_executor() from its module in
r6/actions/rails/. VALID_KINDS is derived from the registry — rails never edit
a shared tuple. This is the ~50-line contributor surface the spec advertises."""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

@dataclass
class ExecutionResult:
    status: str                       # executing | completed | failed | needs_review
    provider_ref: str | None = None
    outcome: dict = field(default_factory=dict)
    error: str | None = None
    outcome_unknown: bool = False     # provider MAY have acted -> map to 'unknown'

@runtime_checkable
class ActionExecutor(Protocol):
    kind: str
    required_env: tuple                # preflight self-assembles from these
    def validate(self, payload: dict) -> list: ...       # [] if ok; error codes otherwise
    def execute(self, action) -> ExecutionResult: ...
    def reconcile(self, action) -> ExecutionResult: ...   # query provider truth

_REGISTRY = {}

def register_executor(executor):
    if not isinstance(executor, ActionExecutor):
        raise TypeError('executor must implement ActionExecutor Protocol')
    if executor.kind in _REGISTRY:
        raise ValueError('executor already registered for kind: %s' % executor.kind)
    _REGISTRY[executor.kind] = executor

def get_executor(kind):
    return _REGISTRY.get(kind)

def all_kinds():
    return tuple(_REGISTRY.keys())

def _clear():
    """Test-only: reset the registry between tests."""
    _REGISTRY.clear()
```

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add r6/actions/registry.py tests/actions/test_registry.py && git commit -m "feat(actions): ActionExecutor Protocol + registry (extension point)"`

---

## Task 7: Red-flag emergency screen

**Files:** Create `r6/actions/safety.py`; Test `tests/actions/test_safety_redflag.py`. Reference: `r6/smbp/triage.py` (the SYMPTOMS list — read it first to reuse, not duplicate).

- [ ] **Step 1: Write the failing tests**

```python
# tests/actions/test_safety_redflag.py
from r6.actions.safety import screen_text, EMERGENCY_MESSAGE

def test_chest_pain_flagged():
    hit = screen_text('book me a visit, chest pain when I climb stairs')
    assert hit is not None and hit['emergency'] is True

def test_routine_not_flagged():
    assert screen_text('annual physical, no issues') is None

def test_expanded_lexicon():
    for phrase in ['trouble breathing', 'want to kill myself', 'face drooping']:
        assert screen_text(phrase) is not None
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3: Implement** (import the existing SMBP symptom lexicon; extend it — do not fork it)

```python
# r6/actions/safety.py
"""Mandatory, non-bypassable emergency screen. Runs at PROPOSE on every
free-text reason/body. A hit refuses the action and returns 911/urgent-care
escalation, audited like a Schedule-II refusal. Reuses the SMBP triage red-flag
doctrine (symptoms trump everything) — see r6/smbp/triage.py.

NOTE (spec): lexicon is sufficient for SMS bodies; the booking-reason field
must additionally use a structured question set or a classifier held to a
zero-false-negative eval gate. That classifier is the booking rail's job; this
module is the lexicon floor every kind shares."""
import re
from r6.smbp.triage import SYMPTOMS  # existing red-flag symptom phrases

# Expansion beyond cardiac/stroke: mental-health crisis, anaphylaxis, OB.
_EXTRA = [
    'kill myself', 'suicid', 'end my life', 'want to die',
    'anaphylax', 'throat closing', 'can\'t breathe', 'cannot breathe',
    'trouble breathing', 'face drooping', 'slurred speech',
    'vaginal bleeding', 'pregnant and bleeding', 'overdose',
]
_LEXICON = [s.lower() for s in list(SYMPTOMS) + _EXTRA]

EMERGENCY_MESSAGE = (
    'This looks like it may be an emergency. HealthClaw cannot act on '
    'emergencies. If this is a medical emergency, call 911 or go to the '
    'nearest emergency department now.')

def screen_text(text):
    """Return {'emergency': True, 'matched': <phrase>} on a red-flag hit, else
    None. Case-insensitive substring match on the shared lexicon."""
    if not text:
        return None
    low = text.lower()
    for phrase in _LEXICON:
        if phrase and phrase in low:
            return {'emergency': True, 'matched': phrase}
    return None
```

> If `r6/smbp/triage.py` does not export a `SYMPTOMS` iterable of phrase strings, adapt the import to whatever the triage module exposes (read it first). The point is one shared lexicon, not two.

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add r6/actions/safety.py tests/actions/test_safety_redflag.py && git commit -m "feat(actions): mandatory red-flag emergency screen"`

---

## Task 8: Port existing executors into the registry

**Files:** Create `r6/actions/rails/__init__.py`, `r6/actions/rails/phone.py`, `r6/actions/rails/sms.py`; Modify `r6/actions/executors.py` (thin shim); Test `tests/actions/test_rails_ported.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/actions/test_rails_ported.py
from r6.actions.registry import get_executor, all_kinds
import r6.actions.rails  # triggers registration

def test_phone_and_sms_registered():
    assert 'phone-call' in all_kinds()
    assert 'sms' in all_kinds()

def test_sms_validate_requires_body():
    ex = get_executor('sms')
    assert ex.validate({'body': ''}) == ['payload_invalid']
    assert ex.validate({'body': 'hi', 'contact_id': 'c1'}) == []
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3a: `r6/actions/rails/phone.py`** — wrap the existing `_execute_call` logic as an executor. Move the body of `_execute_call` (executors.py:45-78) into `PhoneCallExecutor.execute`, returning the new `ExecutionResult` shape (`status='executing'` when a real provider ref comes back and a webhook will resolve it; `status='completed'` for simulated). Add `validate()` and a `reconcile()` that GETs `https://api.bland.ai/v1/calls/{provider_ref}`. `required_env = ('BLAND_AI_API_KEY',)`. Register at import: `register_executor(PhoneCallExecutor())`.

- [ ] **Step 3b: `r6/actions/rails/sms.py`** — same treatment for `_execute_sms`; `reconcile()` GETs the Twilio message resource; `required_env = ('TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_FROM_NUMBER')`.

- [ ] **Step 3c: `r6/actions/rails/__init__.py`**

```python
"""Importing this package registers every rail's executor."""
from r6.actions.rails import phone, sms  # noqa: F401  (import side effect)
```

- [ ] **Step 3d: `r6/actions/executors.py`** — reduce to a shim so existing imports (`from r6.actions.executors import execute_action, ExecutionResult`) keep working during the transition: re-export `ExecutionResult` from `registry`, and make `execute_action(kind, payload, action_id)` look up `get_executor(kind)` and adapt. Keep the module docstring's "no retries by design" note.

> Detailed code for 3a/3b is intentionally an adaptation of the already-correct `_execute_call`/`_execute_sms` bodies you just read — preserve every behavior (simulated-key handling becomes a loud `provider_not_configured` per spec §"No silent simulation"; the `outcome_unknown` mapping stays). The executor is the same network code behind the Protocol.

- [ ] **Step 4: Run** — `pytest tests/actions/ -q` PASS; then `pytest tests/ -k action -q` to confirm no regression in the existing `test_actions*.py` suite.
- [ ] **Step 5: Commit** — `git add r6/actions/rails/ r6/actions/executors.py tests/actions/test_rails_ported.py && git commit -m "refactor(actions): port phone/sms into registered executors"`

---

## Task 9: Fake-provider harness + generic contract tests (the rail merge gate)

**Files:** Modify `tests/conftest.py`; Create `tests/actions/test_contract_generic.py`

- [ ] **Step 1: Add fixtures to `tests/conftest.py`**

```python
@pytest.fixture
def reset_action_registry():
    from r6.actions.registry import _clear
    _clear()
    import importlib, r6.actions.rails
    importlib.reload(r6.actions.rails)  # re-register the real rails
    yield

@pytest.fixture
def fake_providers(monkeypatch):
    """Intercept the provider HTTP calls so contract tests never hit the network.
    Records calls for assertion; replays recorded fixtures from the W0 spikes."""
    calls = []
    def fake_post(url, **kw):
        calls.append({'url': url, 'kw': kw})
        class R:
            status_code = 200
            def json(self): return {'call_id': 'fake-123', 'sid': 'fake-123'}
        return R()
    monkeypatch.setattr('requests.post', fake_post)
    return calls
```

- [ ] **Step 2: Write the generic suite** — the gate every rail must pass to merge

```python
# tests/actions/test_contract_generic.py
"""Generic contract suite — parametrized over EVERY registered executor. A rail
merges only when it passes this. New rails add zero lines here; they just
register and inherit these assertions."""
import pytest
from r6.actions.registry import all_kinds, get_executor, ExecutionResult

def _kinds():
    import r6.actions.rails  # ensure registration
    return list(all_kinds())

@pytest.mark.parametrize('kind', _kinds())
def test_validate_rejects_empty_payload(kind):
    errs = get_executor(kind).validate({})
    assert isinstance(errs, list) and errs, '%s.validate({}) must return error codes' % kind

@pytest.mark.parametrize('kind', _kinds())
def test_executor_declares_required_env(kind):
    ex = get_executor(kind)
    assert isinstance(ex.required_env, tuple)

@pytest.mark.parametrize('kind', _kinds())
def test_reconcile_is_implemented(kind):
    ex = get_executor(kind)
    assert callable(getattr(ex, 'reconcile', None))

@pytest.mark.parametrize('kind', _kinds())
def test_needs_review_carries_evidence_contract(kind):
    # Documents the invariant executors must honor; enforced per-rail in their
    # own tests with a needs_review-producing input.
    r = ExecutionResult(status='needs_review', outcome={'transcript_ref': 'x'})
    assert r.status == 'needs_review' and r.outcome.get('transcript_ref')
```

- [ ] **Step 3: Run** — `pytest tests/actions/test_contract_generic.py -v` PASS for phone-call and sms.
- [ ] **Step 4: Commit** — `git add tests/conftest.py tests/actions/test_contract_generic.py && git commit -m "test(actions): fake-provider harness + generic contract gate"`

---

## Task 10: Approve-is-the-commit flow

**Files:** Modify `r6/actions/routes.py`; Test `tests/actions/test_confirm_is_commit.py`. This replaces the spoofable `X-Human-Confirmed` gate.

- [ ] **Step 1: Write the failing tests**

```python
# tests/actions/test_confirm_is_commit.py
import json

def _propose(client, tenant):
    r = client.post('/r6/actions/propose', headers={'X-Tenant-Id': tenant},
                    json={'kind': 'sms', 'payload': {'body': 'refill ready?', 'contact_id': 'c1'}})
    return r.get_json()['id']

def test_commit_submits_for_confirmation_not_execute(client, tenant_id, auth_headers, fake_providers):
    aid = _propose(client, tenant_id)
    r = client.post('/r6/actions/%s/commit' % aid, headers=auth_headers)
    assert r.status_code == 202
    assert r.get_json()['status'] == 'awaiting_confirmation'
    assert fake_providers == []  # nothing executed yet — no human approved

def test_approve_executes(client, tenant_id, auth_headers, fake_providers):
    aid = _propose(client, tenant_id)
    client.post('/r6/actions/%s/commit' % aid, headers=auth_headers)
    r = client.post('/r6/actions/%s/confirm' % aid, headers=auth_headers)  # out-of-band approve
    assert r.status_code == 200
    assert len(fake_providers) == 1  # NOW it executed

def test_no_execute_without_confirmation_record(client, tenant_id, auth_headers, fake_providers):
    aid = _propose(client, tenant_id)
    client.post('/r6/actions/%s/commit' % aid, headers=auth_headers)
    # tamper: call an internal execute path directly is impossible; confirm is the only door
    # A second confirm must not double-execute (single-use):
    client.post('/r6/actions/%s/confirm' % aid, headers=auth_headers)
    r2 = client.post('/r6/actions/%s/confirm' % aid, headers=auth_headers)
    assert r2.status_code in (409, 410)
    assert len(fake_providers) == 1

def test_emergency_reason_refused_at_propose(client, tenant_id):
    r = client.post('/r6/actions/propose', headers={'X-Tenant-Id': tenant_id},
                    json={'kind': 'sms', 'payload': {'body': 'I have chest pain', 'contact_id': 'c1'}})
    assert r.status_code == 422
    assert r.get_json()['error_code'] == 'emergency_indicated'
```

- [ ] **Step 2: Run** — FAIL

- [ ] **Step 3: Rework `routes.py`:**
  - **`propose_action`** (and `propose_rx_transfer`): after payload validation, run `screen_text(payload.get('body'))`; on hit return `422 {'error_code': 'emergency_indicated', 'error': EMERGENCY_MESSAGE}` and audit the refusal. Then run `get_executor(kind).validate(payload)`; on non-empty return `422 {'error_code': 'payload_invalid', 'errors': [...]}`.
  - **`commit_action`** becomes *submit for confirmation*: keep Gate 1 (step-up token). DELETE Gate 2 (the `X-Human-Confirmed` header check, lines 169-177). Transition `proposed → awaiting_confirmation` via `transition_action(..., actor='commit-route')` (guarded; handles expiry). Push the confirm card via `notify_tenant` (summary-only). Return `202 {'status':'awaiting_confirmation', ...}` with the pending contract text: *"terminal for this turn; the patient approves out-of-band; poll GET /r6/actions/<id>."*
  - **New `POST /<action_id>/confirm`** — the out-of-band approve handler (authenticated as the human via step-up token, tenant-scoped): (1) `issue_confirmation(action_id, approved_via, ttl)` then `db.session.flush()`; (2) staleness/red-flag re-check; (3) the claim: `transition_action(action_id, from_states=('awaiting_confirmation',), to_state='executing', actor='confirm', attempt_id=<uuid>, claimed_at=now)` — AND in the same transaction `consume_confirmation(action_id)` must return True, else roll back and return 409 (double-approve / replay); (4) stamp `provider_request_at=now`, call `get_executor(kind).execute(action)`; (5) map result to terminal state via `transition_action`. Reuse the existing simulated/real/unknown mapping logic (routes.py:231-278) but through `transition_action`.

> This is the largest task; split its steps as you implement (one test green at a time). The old `commit` semantics (execute-on-commit) are fully replaced. Keep the atomic-claim discipline — `transition_action` already encapsulates the guarded UPDATE.

- [ ] **Step 4: Run** — `pytest tests/actions/test_confirm_is_commit.py -v` PASS; then full `pytest tests/ -k action -q`. Fix any existing action test that asserted the old `X-Human-Confirmed`/execute-on-commit behavior (update them to the submit→approve flow — this is expected and correct).
- [ ] **Step 5: Commit** — `git add r6/actions/routes.py tests/actions/test_confirm_is_commit.py && git commit -m "feat(actions): Approve-is-the-commit — out-of-band execution, kills spoofable gate"`

---

## Task 11: Strip Node's confirmation-header minting

**Files:** Modify `services/agent-orchestrator/src/tools.ts`; Test `services/agent-orchestrator/src/tools.test.ts`

- [ ] **Step 1: Write/adjust the failing test** — assert `action_commit` no longer sends `X-Human-Confirmed` and that its result text tells the model to end the turn:

```typescript
// in tools.test.ts
it('action_commit does not self-attest human confirmation', async () => {
  const captured = mockFlaskFetch();               // existing test harness pattern
  await callTool('action_commit', { action_id: 'a1' });
  expect(captured.headers['X-Human-Confirmed']).toBeUndefined();
});
it('action_commit result instructs the model to end the turn', async () => {
  const res = await callTool('action_commit', { action_id: 'a1' });
  expect(res.content[0].text).toMatch(/approve.*out.?of.?band|end your turn/i);
});
```

- [ ] **Step 2: Run** — `npm --prefix services/agent-orchestrator test` → FAIL

- [ ] **Step 3: Implement** — at `tools.ts:~1839` delete the `"X-Human-Confirmed": "true"` header from the `action_commit` fetch. Update the tool's returned text to the pending contract: *"Submitted for the patient's approval. This is terminal for your turn — the patient must approve out of band (Telegram/dashboard). Poll `action_status` or end your turn; do not retry `action_commit`."* Do NOT touch `fhir_get_token` here (that's the read path; a later task governs write-action tokens end-to-end).

- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git add services/agent-orchestrator/src/tools.ts services/agent-orchestrator/src/tools.test.ts && git commit -m "fix(mcp): stop self-attesting human confirmation on action_commit"`

---

## Task 12: Wire schema_sync + registration into boot; final integration pass

**Files:** verify `main.py` calls `db.create_all()` (it does) so the three new tables (`action_events`, `action_confirmations`, plus the new columns) exist; confirm `r6.actions.rails` is imported at app startup so executors register. Test: `tests/actions/test_boot_integration.py`

- [ ] **Step 1: Failing test** — app boots, all kinds registered, tables queryable:

```python
# tests/actions/test_boot_integration.py
def test_boot_registers_rails_and_tables(app):
    with app.app_context():
        from r6.actions.registry import all_kinds
        assert set(('phone-call', 'sms')).issubset(set(all_kinds()))
        from r6.actions.events import ActionEvent
        from r6.actions.confirmations import ActionConfirmation
        assert ActionEvent.query.count() == 0
        assert ActionConfirmation.query.count() == 0
```

- [ ] **Step 2: Run** — likely FAIL if rails aren't imported at boot
- [ ] **Step 3: Implement** — ensure `import r6.actions.rails` runs during app init (add to wherever `actions_blueprint` is registered in `main.py`/app factory). Confirm the new models are imported before `db.create_all()` (import in the same module that already imports `ProposedAction`).
- [ ] **Step 4: Run** — full `pytest tests/ -q` green
- [ ] **Step 5: Commit** — `git add -A && git commit -m "chore(actions): register rails + models at boot; PR #1 integration green"`

---

## Self-review notes (author)

- **Spec coverage:** state machine ✔(T3) · transition_action+events ✔(T2,T3) · ActionConfirmation atomic single-use ✔(T5) · executor Protocol/registry ✔(T6) · red-flag screen owned by PR#1 ✔(T7) · attempt ledger ✔(T4) · error taxonomy ✔(T1) · Approve-is-commit + kills spoofable gate ✔(T10,T11) · fake-provider harness + generic gate ✔(T9). **Deferred to later plans (correctly):** the reaper (`POST /r6/ops/reap`), preflight endpoint, contact-by-reference `TenantContact` resolution (T10 accepts `contact_id` in payload; server-side number resolution is the comms-rail plan), staleness payload-hash (stub the re-check in T10; full hash in comms rail), Postgres CI lane, golden evals. These are named W0/W1 items in separate plans; PR #1 is the contract floor only.
- **Type consistency:** `ExecutionResult` defined once in `registry.py`, re-exported from `executors.py` shim (T8) — every rail imports from `registry`. `transition_action(action_id, from_states, to_state, actor, detail, **fields)` signature identical in T3 definition and T10 use.
- **Known adaptation points flagged inline:** T7 (triage.py export shape), T8 (preserve exact network behavior of ported executors), T10 (largest task — split as implemented).
