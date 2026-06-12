# Action Core (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real-world actions (phone calls, SMS) become guardrailed operations: propose → confirm (step-up + human confirmation) → execute → webhook callback, with every transition audited — exposed as three new MCP tools.

**Architecture:** New Flask blueprint `r6/actions/` mirroring the `r6/fasten/` module pattern. `ProposedAction` rows track the lifecycle (`proposed → confirmed → executing → completed | failed | expired | unknown`). Executors call Bland.ai/Twilio, with **simulation mode** when API keys are absent. The MCP server (`services/agent-orchestrator/src/tools.ts`) gains `action_propose`, `action_commit` (step-up gated), `action_status`. `shl_generate` and `provider_lookup` are Phases 3–4, NOT this plan.

**Tech Stack:** Flask + SQLAlchemy (existing `models.db`), `requests` (already a dependency via telegram_push), TypeScript MCP server + Jest, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-unified-action-layer-design.md`

**Clarification vs spec:** Call scripts are NOT passed through `apply_redaction` (it operates on FHIR resources, not free text, and the executor needs the verbatim script to place the call). Instead: full payload lives only in the tenant-scoped `ProposedAction` row; **audit `detail` and `notify_tenant` messages carry summaries only** (kind + recipient label, never script text, phone, or PHI).

**CI gotcha reminder:** CI runs Python 3.11 — no backslash escapes inside f-string `{...}` expressions. `validate_step_up_token` returns a `(bool, str)` tuple — always destructure.

---

### Task 1: ProposedAction model

**Files:**
- Create: `r6/actions/__init__.py` (empty)
- Create: `r6/actions/models.py`
- Test: `tests/test_actions_models.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_actions_models.py
"""ProposedAction lifecycle model tests."""
import json
from datetime import datetime, timedelta, timezone

from r6.actions.models import ProposedAction, PROPOSAL_TTL_MINUTES


def test_create_defaults(app):
    with app.app_context():
        from models import db
        action = ProposedAction(
            tenant_id='test-tenant',
            kind='phone-call',
            payload={'to': 'CVS Pharmacy', 'phone': '617-555-0100',
                     'body': 'Refill script text'},
        )
        db.session.add(action)
        db.session.commit()

        assert action.id  # uuid assigned
        assert action.status == 'proposed'
        assert action.kind == 'phone-call'
        assert json.loads(action.payload_json)['phone'] == '617-555-0100'
        assert action.external_ref is None
        # expires ~30 min out
        delta = action.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
        assert timedelta(minutes=PROPOSAL_TTL_MINUTES - 1) < delta <= timedelta(minutes=PROPOSAL_TTL_MINUTES)


def test_is_expired(app):
    with app.app_context():
        from models import db
        action = ProposedAction(tenant_id='t', kind='sms', payload={'body': 'x'})
        action.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.add(action)
        db.session.commit()
        assert action.is_expired() is True


def test_invalid_kind_rejected(app):
    with app.app_context():
        import pytest
        with pytest.raises(ValueError):
            ProposedAction(tenant_id='t', kind='teleport', payload={})


def test_transition_guard(app):
    with app.app_context():
        from models import db
        action = ProposedAction(tenant_id='t', kind='phone-call', payload={'body': 'x'})
        db.session.add(action)
        db.session.commit()
        # legal: proposed -> confirmed -> executing -> completed
        action.transition('confirmed')
        action.transition('executing')
        action.transition('completed')
        # illegal: completed -> executing
        import pytest
        with pytest.raises(ValueError):
            action.transition('executing')


def test_summary_has_no_payload(app):
    with app.app_context():
        from models import db
        action = ProposedAction(
            tenant_id='t', kind='phone-call',
            payload={'to': 'CVS', 'phone': '617-555-0100', 'body': 'SECRET SCRIPT'},
        )
        db.session.add(action)
        db.session.commit()
        s = action.summary()
        assert 'SECRET SCRIPT' not in json.dumps(s)
        assert '617-555-0100' not in json.dumps(s)
        assert s['kind'] == 'phone-call'
        assert s['to'] == 'CVS'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'r6.actions'`

- [ ] **Step 3: Write the model**

```python
# r6/actions/models.py
"""
ProposedAction — lifecycle record for real-world actions (calls, SMS).

Mirrors the FHIR propose -> commit write pattern: an action is proposed
(draft shown to the patient), confirmed (step-up + human confirmation),
executed (Bland.ai / Twilio), and resolved by webhook callback.

PHI note: payload_json holds the verbatim script (needed to execute) and
is tenant-scoped like R6Resource. summary() is the ONLY representation
allowed in audit detail and Telegram notifications.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from models import db

PROPOSAL_TTL_MINUTES = 30

VALID_KINDS = ('phone-call', 'sms', 'form-fill', 'insurance-call')

# Legal status transitions
_TRANSITIONS = {
    'proposed': {'confirmed', 'expired'},
    'confirmed': {'executing', 'failed'},
    'executing': {'completed', 'failed', 'unknown'},
    'completed': set(),
    'failed': set(),
    'expired': set(),
    'unknown': {'completed', 'failed'},  # late webhook may still resolve it
}


def _utcnow():
    # Stored naive-UTC, matching every other model in this codebase
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProposedAction(db.Model):
    __tablename__ = 'proposed_actions'

    id = db.Column(db.String(64), primary_key=True,
                   default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='proposed')
    external_ref = db.Column(db.String(128), nullable=True)
    outcome_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

    def __init__(self, tenant_id, kind, payload, **kwargs):
        if kind not in VALID_KINDS:
            raise ValueError('Unsupported action kind: %s' % kind)
        super().__init__(
            tenant_id=tenant_id,
            kind=kind,
            payload_json=json.dumps(payload),
            expires_at=_utcnow() + timedelta(minutes=PROPOSAL_TTL_MINUTES),
            **kwargs,
        )

    def is_expired(self):
        return _utcnow() >= self.expires_at

    def transition(self, new_status):
        allowed = _TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                'Illegal transition %s -> %s' % (self.status, new_status))
        self.status = new_status

    @property
    def payload(self):
        return json.loads(self.payload_json)

    def summary(self):
        """PHI-safe representation — the only shape allowed in audit/notify."""
        p = self.payload
        return {
            'id': self.id,
            'kind': self.kind,
            'to': p.get('to'),          # recipient label, e.g. "CVS Pharmacy"
            'status': self.status,
            'expires_at': self.expires_at.isoformat() + 'Z',
        }

    def to_dict(self):
        """Full representation for the owning tenant (includes draft)."""
        d = self.summary()
        d['payload'] = self.payload
        d['external_ref'] = self.external_ref
        d['outcome_summary'] = self.outcome_summary
        return d
```

Also create empty `r6/actions/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_models.py -v`
Expected: 5 PASS. (The `app` fixture in `tests/conftest.py` calls `db.create_all()`, which picks up the new table — verify no fixture change is needed; if the conftest imports models explicitly, add `import r6.actions.models  # noqa` beside the other model imports.)

- [ ] **Step 5: Commit**

```bash
git add r6/actions/__init__.py r6/actions/models.py tests/test_actions_models.py
git commit -m "feat(actions): ProposedAction lifecycle model with PHI-safe summary"
```

---

### Task 2: Executors with simulation mode

**Files:**
- Create: `r6/actions/executors.py`
- Test: `tests/test_actions_executors.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_actions_executors.py
"""Executor tests — simulation mode (no keys) and mocked provider contracts."""
from unittest.mock import patch, MagicMock

from r6.actions.executors import execute_action, ExecutionResult


def test_phone_call_simulated_without_key(monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    result = execute_action(
        kind='phone-call',
        payload={'phone': '617-555-0100', 'body': 'script', 'to': 'CVS'},
    )
    assert isinstance(result, ExecutionResult)
    assert result.simulated is True
    assert result.ok is True
    assert result.external_ref.startswith('sim-')


def test_sms_simulated_without_keys(monkeypatch):
    for var in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'):
        monkeypatch.delenv(var, raising=False)
    result = execute_action(kind='sms', payload={'phone': '+16175550100', 'body': 'hi'})
    assert result.simulated is True
    assert result.ok is True


def test_phone_call_real_contract(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {'call_id': 'bl-123'}
    with patch('r6.actions.executors.requests.post', return_value=fake) as post:
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script', 'to': 'CVS'},
            action_id='act-1',
        )
    assert result.ok is True
    assert result.simulated is False
    assert result.external_ref == 'bl-123'
    args, kwargs = post.call_args
    assert args[0] == 'https://api.bland.ai/v1/calls'
    assert kwargs['headers']['Authorization'] == 'test-key'
    assert kwargs['json']['phone_number'] == '617-555-0100'
    assert kwargs['json']['task'] == 'script'
    assert 'act-1' in kwargs['json']['webhook']


def test_phone_call_provider_error(monkeypatch):
    monkeypatch.setenv('BLAND_AI_API_KEY', 'test-key')
    fake = MagicMock()
    fake.status_code = 502
    fake.text = 'upstream error'
    with patch('r6.actions.executors.requests.post', return_value=fake):
        result = execute_action(
            kind='phone-call',
            payload={'phone': '617-555-0100', 'body': 'script'},
        )
    assert result.ok is False
    assert result.simulated is False
    assert 'upstream error' not in (result.error or '') or len(result.error) < 200


def test_missing_phone_fails_fast():
    result = execute_action(kind='phone-call', payload={'body': 'script'})
    assert result.ok is False
    assert 'phone' in result.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_executors.py -v`
Expected: FAIL — `ModuleNotFoundError` (executors module doesn't exist)

- [ ] **Step 3: Write the executors**

```python
# r6/actions/executors.py
"""
Action executors — Bland.ai (phone calls) and Twilio (SMS).

SIMULATION MODE: when the relevant API keys are absent, executors return a
successful simulated result instead of making network calls. This keeps
local dev, CI, and the demo stack fully functional with zero credentials,
matching the contract careagents.cloud used before this module existed.

No retries by design: a double-placed phone call is worse than a failed one.
"""

import base64
import logging
import os
import secrets
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


@dataclass
class ExecutionResult:
    ok: bool
    simulated: bool
    external_ref: str | None = None
    error: str | None = None


def _webhook_url(provider, action_id):
    base = os.environ.get('PUBLIC_BASE_URL', 'https://app.healthclaw.io')
    secret = os.environ.get('ACTIONS_WEBHOOK_SECRET', '')
    url = '%s/r6/actions/callback/%s?action_id=%s' % (base, provider, action_id)
    if secret:
        url += '&secret=%s' % secret
    return url


def _execute_call(payload, action_id):
    phone = payload.get('phone')
    if not phone:
        return ExecutionResult(ok=False, simulated=False,
                               error='phone number is required')
    api_key = os.environ.get('BLAND_AI_API_KEY')
    if not api_key:
        return ExecutionResult(ok=True, simulated=True,
                               external_ref='sim-' + secrets.token_hex(6))
    resp = requests.post(
        'https://api.bland.ai/v1/calls',
        headers={'Authorization': api_key,
                 'Content-Type': 'application/json'},
        json={
            'phone_number': phone,
            'task': payload.get('body', ''),
            'voice': 'maya',
            'webhook': _webhook_url('bland', action_id),
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.error('Bland.ai error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False,
                               error='Call provider error (HTTP %s)' % resp.status_code)
    return ExecutionResult(ok=True, simulated=False,
                           external_ref=resp.json().get('call_id'))


def _execute_sms(payload, action_id):
    phone = payload.get('phone')
    if not phone:
        return ExecutionResult(ok=False, simulated=False,
                               error='phone number is required')
    sid = os.environ.get('TWILIO_ACCOUNT_SID')
    token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_num = os.environ.get('TWILIO_FROM_NUMBER')
    if not (sid and token and from_num):
        return ExecutionResult(ok=True, simulated=True,
                               external_ref='sim-' + secrets.token_hex(6))
    auth = base64.b64encode(('%s:%s' % (sid, token)).encode()).decode()
    resp = requests.post(
        'https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json' % sid,
        headers={'Authorization': 'Basic ' + auth},
        data={'To': phone, 'From': from_num, 'Body': payload.get('body', '')},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        logger.error('Twilio error %s', resp.status_code)
        return ExecutionResult(ok=False, simulated=False,
                               error='SMS provider error (HTTP %s)' % resp.status_code)
    return ExecutionResult(ok=True, simulated=False,
                           external_ref=resp.json().get('sid'))


_EXECUTORS = {
    'phone-call': _execute_call,
    'insurance-call': _execute_call,   # same transport, different script source
    'sms': _execute_sms,
}


def execute_action(kind, payload, action_id=None):
    """Dispatch to the executor for this action kind."""
    executor = _EXECUTORS.get(kind)
    if executor is None:
        return ExecutionResult(ok=False, simulated=False,
                               error='No executor for kind: %s' % kind)
    try:
        return executor(payload, action_id)
    except requests.RequestException as exc:
        logger.error('Executor network failure: %s', exc)
        return ExecutionResult(ok=False, simulated=False,
                               error='Provider unreachable')
```

Note: `form-fill` has no executor in Phase 1 (the upload/fill pipeline is Phase 4); proposing one will fail at commit with "No executor" — acceptable and tested implicitly by the dispatch default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_executors.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add r6/actions/executors.py tests/test_actions_executors.py
git commit -m "feat(actions): Bland/Twilio executors with simulation mode"
```

---

### Task 3: Blueprint + propose route

**Files:**
- Create: `r6/actions/routes.py`
- Modify: `main.py` (register blueprint, after the fasten registration around line 138)
- Test: `tests/test_actions_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_actions_routes.py
"""Action routes — propose / commit / status / callback."""
import json

from r6.actions.models import ProposedAction


PROPOSE_BODY = {
    'kind': 'phone-call',
    'payload': {
        'to': 'CVS Pharmacy',
        'phone': '617-555-0100',
        'body': 'Hi, calling for a refill of metformin 500mg for John Smith.',
    },
}


def test_propose_requires_tenant(client):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY)
    assert resp.status_code == 400


def test_propose_creates_action(client, tenant_headers, app):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'proposed'
    assert data['payload']['body'].startswith('Hi, calling')
    with app.app_context():
        row = ProposedAction.query.get(data['id'])
        assert row is not None
        assert row.tenant_id == tenant_headers['X-Tenant-Id']


def test_propose_rejects_bad_kind(client, tenant_headers):
    resp = client.post('/r6/actions/propose',
                       json={'kind': 'teleport', 'payload': {}},
                       headers=tenant_headers)
    assert resp.status_code == 400


def test_propose_emits_audit_event(client, tenant_headers, app):
    client.post('/r6/actions/propose', json=PROPOSE_BODY, headers=tenant_headers)
    with app.app_context():
        from r6.models import AuditEventRecord
        events = AuditEventRecord.query.filter_by(
            tenant_id=tenant_headers['X-Tenant-Id'],
            resource_type='ProposedAction').all()
        assert len(events) == 1
        # PHI-safe: no script text or phone number in audit detail
        assert '617-555-0100' not in (events[0].detail or '')
        assert 'metformin' not in (events[0].detail or '')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_routes.py -v`
Expected: FAIL — 404s (blueprint not registered)

- [ ] **Step 3: Write the blueprint with the propose route**

```python
# r6/actions/routes.py
"""
Action lifecycle API — propose / commit / status / provider callbacks.

Contract mirrors FHIR writes:
  propose  -> tenant header only, returns draft for human review
  commit   -> X-Step-Up-Token (validated, tuple destructured) AND
              X-Human-Confirmed: true, else 401 / 428
  callback -> shared-secret (Bland) or X-Twilio-Signature verification

Audit detail and Telegram pushes use ProposedAction.summary() ONLY (no PHI).
"""

import hmac
import json
import logging
import os
import re

from flask import Blueprint, jsonify, request

from models import db
from r6.actions.executors import execute_action
from r6.actions.models import ProposedAction, VALID_KINDS
from r6.audit import record_audit_event
from r6.stepup import validate_step_up_token

logger = logging.getLogger(__name__)

actions_blueprint = Blueprint('actions', __name__, url_prefix='/r6/actions')

_TENANT_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _error(status, message):
    return jsonify({'error': message}), status


def _tenant_or_none():
    tenant_id = request.headers.get('X-Tenant-Id', '')
    if not tenant_id or not _TENANT_PATTERN.match(tenant_id):
        return None
    return tenant_id


@actions_blueprint.route('/propose', methods=['POST'])
def propose_action():
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    body = request.get_json(silent=True) or {}
    kind = body.get('kind')
    payload = body.get('payload')
    if kind not in VALID_KINDS:
        return _error(400, 'kind must be one of: %s' % ', '.join(VALID_KINDS))
    if not isinstance(payload, dict) or not payload.get('body'):
        return _error(400, 'payload.body is required')

    action = ProposedAction(tenant_id=tenant_id, kind=kind, payload=payload)
    db.session.add(action)
    db.session.commit()

    record_audit_event(
        'create', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )
    return jsonify(action.to_dict()), 201
```

- [ ] **Step 4: Register the blueprint in `main.py`**

Add directly after the fasten registration block (`main.py:138-139`):

```python
from r6.actions.routes import actions_blueprint
app.register_blueprint(actions_blueprint)
logger.info("Actions Blueprint registered at /r6/actions")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_routes.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add r6/actions/routes.py main.py tests/test_actions_routes.py
git commit -m "feat(actions): propose route with tenant gate and PHI-safe audit"
```

---

### Task 4: Commit route — step-up + human confirmation + execute

**Files:**
- Modify: `r6/actions/routes.py` (append route)
- Test: `tests/test_actions_routes.py` (append tests)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_actions_routes.py`)

```python
def _propose(client, tenant_headers):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    return resp.get_json()['id']


def test_commit_requires_step_up(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=tenant_headers)
    assert resp.status_code == 401


def test_commit_requires_human_confirmation(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    # auth_headers has step-up but NOT X-Human-Confirmed
    resp = client.post('/r6/actions/%s/commit' % action_id,
                       headers=auth_headers)
    assert resp.status_code == 428


def test_commit_executes_in_simulation(client, tenant_headers, auth_headers,
                                        app, monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    # simulation completes synchronously
    assert data['status'] == 'completed'
    assert data['simulated'] is True
    with app.app_context():
        from r6.models import AuditEventRecord
        commits = AuditEventRecord.query.filter_by(
            event_type='update', resource_type='ProposedAction',
            resource_id=action_id).all()
        assert len(commits) == 1


def test_commit_expired_returns_410(client, tenant_headers, auth_headers, app):
    from datetime import datetime, timedelta, timezone
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        from models import db
        row = ProposedAction.query.get(action_id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.commit()
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code == 410


def test_commit_double_commit_conflict(client, tenant_headers, auth_headers,
                                        monkeypatch):
    monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    first = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert first.status_code == 200
    second = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert second.status_code == 409


def test_commit_wrong_tenant_404(client, tenant_headers, auth_headers):
    action_id = _propose(client, tenant_headers)
    headers = dict(auth_headers)
    headers['X-Human-Confirmed'] = 'true'
    headers['X-Tenant-Id'] = 'other-tenant'
    # step-up token is tenant-bound, so this fails at validation -> 401
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=headers)
    assert resp.status_code in (401, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_routes.py -v -k commit`
Expected: FAIL — 404 (route doesn't exist)

- [ ] **Step 3: Write the commit route** (append to `r6/actions/routes.py`)

```python
@actions_blueprint.route('/<action_id>/commit', methods=['POST'])
def commit_action(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    # Gate 1: step-up token (ALWAYS destructure the tuple)
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _error(401, 'Action commit requires X-Step-Up-Token header')
    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _error(401, 'Step-up token rejected: %s' % err)

    # Gate 2: human confirmation (same contract as clinical writes)
    confirmed = request.headers.get('X-Human-Confirmed', '').lower()
    if confirmed != 'true':
        return jsonify({
            'error': 'Real-world actions require human confirmation. '
                     'Set X-Human-Confirmed: true after the patient approves '
                     'the draft.',
            'requires_confirmation': True,
        }), 428

    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')
    if action.status != 'proposed':
        return _error(409, 'Action is %s, not proposed' % action.status)
    if action.is_expired():
        action.transition('expired')
        db.session.commit()
        return _error(410, 'Proposal expired — propose the action again')

    action.transition('confirmed')
    action.transition('executing')
    db.session.commit()

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        agent_id=request.headers.get('X-Agent-Id'), tenant_id=tenant_id,
        detail=json.dumps(action.summary()),
    )

    result = execute_action(action.kind, action.payload, action_id=action.id)

    if not result.ok:
        action.transition('failed')
        action.outcome_summary = result.error
        db.session.commit()
        record_audit_event(
            'update', resource_type='ProposedAction', resource_id=action.id,
            tenant_id=tenant_id, outcome='failure', detail=result.error,
        )
        return jsonify({'id': action.id, 'status': 'failed',
                        'error': result.error}), 502

    action.external_ref = result.external_ref
    if result.simulated:
        # No webhook will ever arrive — resolve synchronously
        action.transition('completed')
        action.outcome_summary = 'Simulated (no provider keys configured)'
    db.session.commit()

    response = action.to_dict()
    response['simulated'] = result.simulated
    return jsonify(response), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_routes.py -v`
Expected: all PASS (Task 3's four + six new)

- [ ] **Step 5: Commit**

```bash
git add r6/actions/routes.py tests/test_actions_routes.py
git commit -m "feat(actions): commit route with step-up + 428 human-confirmation gates"
```

---

### Task 5: Status route

**Files:**
- Modify: `r6/actions/routes.py` (append route)
- Test: `tests/test_actions_routes.py` (append tests)

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_status_returns_action(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    resp = client.get('/r6/actions/%s' % action_id, headers=tenant_headers)
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'proposed'


def test_status_tenant_isolation(client, tenant_headers):
    action_id = _propose(client, tenant_headers)
    other = dict(tenant_headers)
    other['X-Tenant-Id'] = 'other-tenant'
    resp = client.get('/r6/actions/%s' % action_id, headers=other)
    assert resp.status_code == 404


def test_status_marks_overdue_expiry(client, tenant_headers, app):
    from datetime import datetime, timedelta, timezone
    action_id = _propose(client, tenant_headers)
    with app.app_context():
        from models import db
        row = ProposedAction.query.get(action_id)
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.commit()
    resp = client.get('/r6/actions/%s' % action_id, headers=tenant_headers)
    assert resp.get_json()['status'] == 'expired'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_routes.py -v -k status`
Expected: FAIL — 404/405

- [ ] **Step 3: Write the status route** (append to `r6/actions/routes.py`)

```python
@actions_blueprint.route('/<action_id>', methods=['GET'])
def action_status(action_id):
    tenant_id = _tenant_or_none()
    if not tenant_id:
        return _error(400, 'X-Tenant-Id header is required')

    action = ProposedAction.query.filter_by(
        id=action_id, tenant_id=tenant_id).first()
    if action is None:
        return _error(404, 'Unknown action')

    # Lazy expiry: a stale proposal flips to expired on read
    if action.status == 'proposed' and action.is_expired():
        action.transition('expired')
        db.session.commit()

    return jsonify(action.to_dict()), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_routes.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add r6/actions/routes.py tests/test_actions_routes.py
git commit -m "feat(actions): status route with tenant isolation and lazy expiry"
```

---

### Task 6: Webhook callbacks (Bland + Twilio) with verification

**Files:**
- Modify: `r6/actions/routes.py` (append routes + verification helpers)
- Test: `tests/test_actions_callbacks.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_actions_callbacks.py
"""Provider webhook callbacks — secret verification + status resolution."""
import json

from r6.actions.models import ProposedAction

PROPOSE_BODY = {
    'kind': 'phone-call',
    'payload': {'to': 'CVS', 'phone': '617-555-0100', 'body': 'script'},
}


def _executing_action(client, tenant_headers, app):
    resp = client.post('/r6/actions/propose', json=PROPOSE_BODY,
                       headers=tenant_headers)
    action_id = resp.get_json()['id']
    with app.app_context():
        from models import db
        row = ProposedAction.query.get(action_id)
        row.transition('confirmed')
        row.transition('executing')
        row.external_ref = 'bl-123'
        db.session.commit()
    return action_id


def test_bland_callback_requires_secret(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=wrong' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed',
              'summary': 'Refill confirmed'})
    assert resp.status_code == 403


def test_bland_callback_completes_action(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed',
              'summary': 'Refill confirmed, ready after 3pm'})
    assert resp.status_code == 200
    with app.app_context():
        row = ProposedAction.query.get(action_id)
        assert row.status == 'completed'
        assert 'ready after 3pm' in row.outcome_summary


def test_bland_callback_failed_call(client, tenant_headers, app, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _executing_action(client, tenant_headers, app)
    resp = client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'failed', 'summary': 'no answer'})
    assert resp.status_code == 200
    with app.app_context():
        row = ProposedAction.query.get(action_id)
        assert row.status == 'failed'


def test_callback_notifies_tenant_summary_only(client, tenant_headers, app,
                                               monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    sent = {}

    def fake_notify(tenant_id, message, parse_mode='Markdown'):
        sent['tenant'] = tenant_id
        sent['message'] = message
        return 1

    monkeypatch.setattr('r6.actions.routes.notify_tenant', fake_notify)
    action_id = _executing_action(client, tenant_headers, app)
    client.post(
        '/r6/actions/callback/bland?action_id=%s&secret=hook-secret' % action_id,
        json={'call_id': 'bl-123', 'status': 'completed', 'summary': 'done'})
    assert sent['tenant'] == tenant_headers['X-Tenant-Id']
    # PHI-safe: recipient label OK, phone number NOT
    assert '617-555-0100' not in sent['message']
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_actions_callbacks.py -v`
Expected: FAIL — 404 (callback route doesn't exist)

- [ ] **Step 3: Write the callback route** (append to `r6/actions/routes.py`; add `from r6.telegram_push import notify_tenant` to the imports at top)

```python
@actions_blueprint.route('/callback/<provider>', methods=['POST'])
def action_callback(provider):
    if provider not in ('bland', 'twilio'):
        return _error(404, 'Unknown provider')

    # Shared-secret verification (constant-time). The secret rides in the
    # webhook URL we registered with the provider at execution time.
    expected = os.environ.get('ACTIONS_WEBHOOK_SECRET', '')
    supplied = request.args.get('secret', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return _error(403, 'Webhook verification failed')

    action_id = request.args.get('action_id', '')
    action = ProposedAction.query.filter_by(id=action_id).first()
    if action is None:
        return _error(404, 'Unknown action')
    if action.status not in ('executing', 'unknown'):
        # Late or duplicate webhook — acknowledge without changing state
        return jsonify({'ok': True, 'note': 'no state change'}), 200

    body = request.get_json(silent=True) or {}
    provider_status = (body.get('status') or '').lower()
    new_status = 'completed' if provider_status in ('completed', 'success') \
        else 'failed'
    action.transition(new_status)
    action.outcome_summary = (body.get('summary') or '')[:2000]
    db.session.commit()

    record_audit_event(
        'update', resource_type='ProposedAction', resource_id=action.id,
        tenant_id=action.tenant_id,
        outcome='success' if new_status == 'completed' else 'failure',
        detail=json.dumps(action.summary()),
    )

    # Telegram push: summary-level ONLY (kind + recipient label + status)
    label = action.summary().get('to') or 'recipient'
    icon = '✅' if new_status == 'completed' else '⚠️'
    notify_tenant(action.tenant_id,
                  '%s %s to %s: %s' % (icon, action.kind, label, new_status))

    return jsonify({'ok': True}), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_actions_callbacks.py -v`
Expected: 4 PASS

- [ ] **Step 5: Run the full Python suite for regressions**

Run: `uv run python -m pytest tests/ -q`
Expected: all pass (553 existing + new)

- [ ] **Step 6: Commit**

```bash
git add r6/actions/routes.py tests/test_actions_callbacks.py
git commit -m "feat(actions): provider webhook callbacks with secret verification"
```

---

### Task 7: MCP tools — action_propose / action_commit / action_status

**Files:**
- Modify: `services/agent-orchestrator/src/tools.ts` (add 3 schemas to `getToolSchemas()`, 3 cases to `executeTool()`, extend the step-up gate)
- Test: `services/agent-orchestrator/src/tools.test.ts` (append)

- [ ] **Step 1: Write the failing tests** (append to `tools.test.ts`, following the file's existing describe/mock style — check the top of the file for the fetch-mock helper before writing)

```typescript
describe("action tools", () => {
  test("action_propose forwards tenant and returns draft", async () => {
    mockFetchResponse(201, {
      id: "act-1", status: "proposed", kind: "phone-call",
      payload: { to: "CVS", body: "script" },
    });
    const result = await tools.executeTool(
      "action_propose",
      { kind: "phone-call", payload: { to: "CVS", phone: "617-555-0100", body: "script" } },
      { "x-tenant-id": "test-tenant" }
    );
    expect(result.status).toBe("proposed");
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain("/r6/actions/propose");
    expect((init.headers as Record<string, string>)["X-Tenant-Id"]).toBe("test-tenant");
  });

  test("action_commit without step-up returns requires_step_up", async () => {
    const result = await tools.executeTool(
      "action_commit", { action_id: "act-1" }, { "x-tenant-id": "test-tenant" }
    );
    expect(result.requires_step_up).toBe(true);
  });

  test("action_commit forwards step-up + human-confirmed headers", async () => {
    mockFetchResponse(200, { id: "act-1", status: "completed", simulated: true });
    await tools.executeTool(
      "action_commit",
      { action_id: "act-1", _stepUpToken: "tok-1" },
      { "x-tenant-id": "test-tenant" }
    );
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain("/r6/actions/act-1/commit");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Step-Up-Token"]).toBe("tok-1");
    expect(headers["X-Human-Confirmed"]).toBe("true");
  });

  test("action_status polls the action", async () => {
    mockFetchResponse(200, { id: "act-1", status: "executing" });
    const result = await tools.executeTool(
      "action_status", { action_id: "act-1" }, { "x-tenant-id": "test-tenant" }
    );
    expect(result.status).toBe("executing");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent-orchestrator && npm test -- --testPathPattern tools`
Expected: FAIL — `Unknown tool: action_propose`

- [ ] **Step 3: Add tool schemas** (append inside the array returned by `getToolSchemas()` in `tools.ts`)

```typescript
      // --- Real-world action tools (Phase 1: action core) ---
      {
        name: "action_propose",
        description:
          "Propose a real-world action (phone call or SMS) on the patient's behalf. Returns a draft (id + script) the patient MUST review before commit. Does not execute anything.",
        tier: "write",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            kind: {
              type: "string",
              enum: ["phone-call", "sms", "insurance-call"],
              description: "Action type",
            },
            payload: {
              type: "object",
              description:
                "Action content: { to: recipient label, phone: number to dial/text, body: call script or message text }",
            },
          },
          required: ["kind", "payload"],
        },
      },
      {
        name: "action_commit",
        description:
          "Execute a previously proposed action AFTER the patient has explicitly approved the draft. Requires step-up authorization (call fhir_get_token first; pass as _stepUpToken). Only call this after the patient says yes.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            action_id: { type: "string", description: "ID returned by action_propose" },
          },
          required: ["action_id"],
        },
      },
      {
        name: "action_status",
        description:
          "Check the status and outcome of an action (proposed/executing/completed/failed). Use after commit to report the result back to the patient.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            action_id: { type: "string", description: "ID returned by action_propose" },
          },
          required: ["action_id"],
        },
      },
```

- [ ] **Step 4: Extend the step-up gate and add execute cases** (in `executeTool()`)

Change the existing gate condition (`tools.ts:516`) from:

```typescript
    if (tool.tier === "write" && toolName === "fhir_commit_write") {
```

to:

```typescript
    if (tool.tier === "write" && (toolName === "fhir_commit_write" || toolName === "action_commit")) {
```

Note: the existing gate reads `headers?.["x-step-up-token"]`; verify (at `tools.ts` around line 516) how `_stepUpToken` from tool arguments is merged into headers for Claude Desktop callers — `fhir_commit_write` already supports it, so follow the identical mechanism (it is handled where `input._stepUpToken` is lifted into the header map; replicate for `action_commit` if it's per-tool rather than generic).

Add to the `switch (toolName)` block:

```typescript
      case "action_propose":
        return this.httpJson(
          "POST", "/r6/actions/propose",
          { kind: input.kind, payload: input.payload },
          fwdHeaders
        );

      case "action_commit": {
        const commitHeaders = { ...fwdHeaders, "X-Human-Confirmed": "true" };
        return this.httpJson(
          "POST", `/r6/actions/${input.action_id}/commit`, undefined, commitHeaders
        );
      }

      case "action_status":
        return this.httpJson(
          "GET", `/r6/actions/${input.action_id}`, undefined, fwdHeaders
        );
```

If `tools.ts` has no generic `httpJson(method, path, body, headers)` helper, follow whatever per-endpoint private method pattern the neighboring cases use (e.g. `this.readResource`) and add three small private methods `proposeAction`, `commitAction`, `getActionStatus` with the same fetch + error-handling shape as `commitWrite`.

Design note: the MCP server sets `X-Human-Confirmed: true` on commit because the *persona* is responsible for collecting the patient's "yes confirm" before calling `action_commit` — same trust model as `curatr_apply_fix`. The tool description enforces this contract.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/agent-orchestrator && npm test`
Expected: all PASS

- [ ] **Step 6: TypeScript compile check**

Run: `cd services/agent-orchestrator && npx tsc --noEmit`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add services/agent-orchestrator/src/tools.ts services/agent-orchestrator/src/tools.test.ts
git commit -m "feat(mcp): action_propose / action_commit / action_status tools"
```

---

### Task 8: demo_e2e.sh gate 11

**Files:**
- Modify: `scripts/demo_e2e.sh` (append gate after gate 10; read the file first and copy its gate/assertion style exactly)

- [ ] **Step 1: Read the existing gate pattern**

Run: `sed -n '1,60p' scripts/demo_e2e.sh` and locate how gates count, fetch step-up tokens, and assert. Reuse its helper functions/variables.

- [ ] **Step 2: Append gate 11** (adapt variable names to the script's conventions)

```bash
# Gate 11: Action core — propose + commit in simulation mode, audit asserted
echo "Gate 11: action propose/commit (simulation)"
ACTION_ID=$(curl -s -X POST "$BASE_URL/r6/actions/propose" \
  -H "X-Tenant-Id: $TENANT" -H "Content-Type: application/json" \
  -d '{"kind":"phone-call","payload":{"to":"Demo Pharmacy","phone":"617-555-0100","body":"Demo refill call script"}}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
[ -n "$ACTION_ID" ] || fail "Gate 11: propose returned no id"

STATUS=$(curl -s -X POST "$BASE_URL/r6/actions/$ACTION_ID/commit" \
  -H "X-Tenant-Id: $TENANT" -H "X-Step-Up-Token: $STEP_UP_TOKEN" \
  -H "X-Human-Confirmed: true" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
[ "$STATUS" = "completed" ] || fail "Gate 11: commit status was $STATUS, expected completed"

AUDIT_COUNT=$(curl -s "$BASE_URL/r6/fhir/AuditEvent?_count=50" -H "X-Tenant-Id: $TENANT" \
  | python3 -c "import json,sys; b=json.load(sys.stdin); print(sum(1 for e in b.get('entry',[]) if 'ProposedAction' in json.dumps(e)))")
[ "$AUDIT_COUNT" -ge 2 ] || fail "Gate 11: expected >=2 ProposedAction audit events, got $AUDIT_COUNT"
echo "  ✓ Gate 11 passed"
```

Note: no `-f` on curl (CI gotcha — `-f` exits 22 on 4xx and kills the step). Verify the AuditEvent search URL shape against how earlier gates query audit events; adjust to the existing pattern if it differs.

- [ ] **Step 3: Run the gate locally**

Run: `python main.py &` then `./scripts/demo_e2e.sh`
Expected: "All 11 gates passed" (or the script's equivalent summary)

- [ ] **Step 4: Commit**

```bash
git add scripts/demo_e2e.sh
git commit -m "test(e2e): gate 11 — action propose/commit in simulation mode"
```

---

### Final verification

- [ ] Full Python suite: `uv run python -m pytest tests/ -q` — all pass
- [ ] Node suite: `cd services/agent-orchestrator && npm test` — all pass
- [ ] TypeScript: `npx tsc --noEmit` — clean
- [ ] `./scripts/demo_e2e.sh` — all 11 gates pass
- [ ] Grep check: `grep -rn "validate_step_up_token" r6/actions/` shows tuple destructuring everywhere
- [ ] Grep check: `grep -n "617-555" tests/` only appears in test fixtures, never in audit/notify assertions as allowed content

### Environment variables introduced (document in deploy notes, set on Railway when going live)

| Var | Purpose | Absent ⇒ |
| --- | --- | --- |
| `BLAND_AI_API_KEY` | Real phone calls | simulation mode |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | Real SMS | simulation mode |
| `ACTIONS_WEBHOOK_SECRET` | Webhook verification | callbacks rejected (403) |
| `PUBLIC_BASE_URL` | Webhook URL base | defaults to `https://app.healthclaw.io` |
