"""The kernel audits every refusal it renders, exactly once (#648).

Before this, `require_grant` raised StepUpDenied, `_render_step_up_denied`
turned it into an OperationOutcome, and nothing recorded that a write was
refused: a credential probe against any migrated site left no trace. The one
direct site that did audit its refusal ($ingest-context, pinned by
tests/test_ingest_auth.py) could not migrate without losing that row. Now the
renderer writes it, so the migration is a move (#648 PR 2).

THE PROPERTY, row by row over every require_grant site: a refused request
answers the status and body it answered before, and leaves exactly one new
AuditEvent — outcome 'failure', the tenant require_grant was handed, and a
detail line with no token, no path, and no header in it.

What this does NOT cover is a named list, `r6.access._UNAUDITED_REFUSALS`,
pinned below, so "every refusal is audited" cannot be read wider than it is.

MUTATIONS (commit first; PYTHONDONTWRITEBYTECODE=1; purge __pycache__):
  delete the `_audit_refusal(exc)` call      -> the census rows go red
  call `_audit_refusal(exc)` twice           -> the census rows go red (== 1)
  drop the leading rollback                  -> the staged-row test goes red
  widen the storage catch and drop the log   -> the storage tests go red
"""

import json
import logging
import re

import pytest
from sqlalchemy.exc import OperationalError

from models import db
from r6 import access
from r6.access import (Scope, StepUpDenied, TenantSource, audit,
                       decide_grant, has_grant, require_grant,
                       tenant_from_request)
from r6.models import AuditEventRecord
from tests.test_step_up_refuses_a_non_ascii_token import (
    ROWS, _SCAN_FLOOR, _call, _require_grant_sites)

_ABSENT_BODY = {'resourceType': 'OperationOutcome', 'issue': [
    {'severity': 'error', 'code': 'security',
     'diagnostics': 'Step-up token required'}]}
_REJECTED_BODY = {'resourceType': 'OperationOutcome', 'issue': [
    {'severity': 'error', 'code': 'security',
     'diagnostics': 'Invalid token signature'}]}

_DETAIL = re.compile(
    r'step-up refused \((absent|rejected)\) at [A-Za-z0-9_.]+: '
    r'(Step-up token required|Invalid token signature)')

_EVENT_TYPE = {'GET': 'read', 'POST': 'create', 'PUT': 'update'}


def _rows():
    db.session.expire_all()
    return AuditEventRecord.query.order_by(AuditEventRecord.recorded).all()


def _new_rows(before_ids):
    return [r for r in _rows() if r.id not in before_ids]


# ---------------------------------------------------------------------------
# The census: every require_grant site, both refusal kinds
# ---------------------------------------------------------------------------

def test_the_census_covers_every_require_grant_site():
    """The rows are the other census's rows, so a new gate without a row goes
    red there AND here — one list of surfaces, not two that can drift."""
    sites, scanned = _require_grant_sites()
    assert scanned >= _SCAN_FLOOR
    assert sites == {row.site for row in ROWS}


@pytest.mark.parametrize('kind', ['absent', 'rejected'])
@pytest.mark.parametrize('row', ROWS, ids=lambda r: r.id)
def test_a_rendered_refusal_is_audited_exactly_once(
        client, tenant_id, step_up_token, row, kind):
    """MUTATION: delete `_audit_refusal(exc)` -> 0 rows; call it twice -> 2."""
    if row.seed is not None:
        row.seed(client, tenant_id, step_up_token)
    before = {r.id for r in _rows()}

    token = '' if kind == 'absent' else step_up_token + 'x'
    resp = _call(client, row, tenant_id, token)

    # Unchanged answer: the status this site answered before, and the exact
    # kernel wording. A row refused earlier for another reason fails here
    # rather than passing on some other audit row.
    assert resp.status_code == row.status, resp.get_data(as_text=True)
    assert resp.get_json() == (_ABSENT_BODY if kind == 'absent'
                               else _REJECTED_BODY)

    new = _new_rows(before)
    assert len(new) == 1, (
        f'{row.id}: a {kind} refusal left {len(new)} audit rows, not 1: '
        f'{[(r.event_type, r.detail) for r in new]}')
    event = new[0]
    assert event.outcome == 'failure'
    assert event.tenant_id == tenant_id
    assert event.event_type == _EVENT_TYPE[row.method]
    assert _DETAIL.fullmatch(event.detail or ''), event.detail
    assert f'({kind})' in event.detail

    # PHI-free and credential-free: nothing the caller sent is stored but the
    # tenant id in its own column.
    stored = ' '.join(str(v) for v in (
        event.event_type, event.resource_type, event.resource_id,
        event.context_id, event.agent_id, event.detail))
    if token:
        assert step_up_token not in stored
    for segment in row.path.split('/'):
        if segment.startswith('no-such-'):
            assert segment not in stored, 'the path id reached the audit row'


# ---------------------------------------------------------------------------
# Rollback first: the refusal row is the only thing the commit carries
# ---------------------------------------------------------------------------

def test_work_staged_before_the_gate_is_not_committed_by_the_audit(
        app, tenant_id):
    """The audit commits. Without the rollback before it, whatever the
    handler staged before its gate would commit too, behind a 401.

    MUTATION: drop the leading `db.session.rollback()` -> the staged row
    survives.
    """
    @app.route('/kernel/stage-then-refuse', methods=['POST'])
    def stage_then_refuse():
        tenant = tenant_from_request(sources=(TenantSource.HEADER,))
        audit(tenant=tenant, event_type='create',
              detail='staged by the handler before its gate')
        require_grant(scope=Scope.WRITE, tenant=tenant)
        return 'unreachable', 201

    before = {r.id for r in _rows()}
    resp = app.test_client().post('/kernel/stage-then-refuse',
                                  headers={'X-Tenant-Id': tenant_id})
    assert resp.status_code == 401
    new = _new_rows(before)
    assert [r.detail for r in new] == [
        'step-up refused (absent) at stage_then_refuse: '
        'Step-up token required']


# ---------------------------------------------------------------------------
# The bound: an anonymous flood cannot become unbounded storage
# ---------------------------------------------------------------------------

def test_refusal_audits_are_bounded_per_client(app, client, tenant_id,
                                                monkeypatch):
    """Past the budget the refusal still answers, byte for byte; one row
    says the budget was reached, and the rest are not stored. Another client
    has its own budget.

    MUTATION: skip the allowance check -> 6 refusal rows, no budget row.
    """
    monkeypatch.setattr(access, '_REFUSAL_AUDIT_BUDGET', 3)
    before = {r.id for r in _rows()}

    bodies = set()
    for _ in range(6):
        resp = client.post('/r6/fhir/$share-bundle', json={},
                           headers={'X-Tenant-Id': tenant_id})
        assert resp.status_code == 401
        bodies.add(resp.get_data())
    assert len(bodies) == 1, 'the refusal changed once the budget ran out'

    new = _new_rows(before)
    # Counted, not ordered: two rows can share a `recorded` timestamp.
    details = sorted(r.detail for r in new)
    assert details == sorted(
        ['step-up refused (absent) at r6.share_bundle: '
         'Step-up token required'] * 3 + [access._BUDGET_DETAIL]), details
    assert all(r.tenant_id == tenant_id and r.outcome == 'failure'
               for r in new)

    other = client.post('/r6/fhir/$share-bundle', json={},
                        headers={'X-Tenant-Id': tenant_id},
                        environ_base={'REMOTE_ADDR': '203.0.113.9'})
    assert other.status_code == 401
    assert len(_new_rows(before)) == 5, 'one client spent another\'s budget'


# ---------------------------------------------------------------------------
# Audit storage failure: the refusal still answers, nothing leaks
# ---------------------------------------------------------------------------

def _storage_down(*_args, **_kwargs):
    raise OperationalError('INSERT INTO audit_events', {},
                           Exception('driver text that must not leak'))


def _assert_refusal_survives(client, tenant_id, step_up_token, caplog):
    before = {r.id for r in _rows()}
    healthy_body = json.dumps(_REJECTED_BODY)
    with caplog.at_level(logging.ERROR, logger='r6.access'):
        resp = client.post('/r6/fhir/$share-bundle', json={},
                           headers={'X-Tenant-Id': tenant_id,
                                    'X-Step-Up-Token': step_up_token + 'x'})
    assert resp.status_code == 401
    assert resp.get_json() == json.loads(healthy_body)
    assert 'driver text' not in resp.get_data(as_text=True)
    assert _new_rows(before) == []
    logged = caplog.text
    assert 'step-up refusal audit write failed: OperationalError' in logged
    assert 'driver text' not in logged
    assert step_up_token not in logged


def test_a_failed_audit_insert_still_answers_the_refusal(
        client, tenant_id, step_up_token, caplog, monkeypatch):
    """Fail closed means the refusal, not a 500: a refusal withholds
    everything already, and a 500 would tell a prober the store is down.

    MUTATION: re-raise in the storage except -> OperationalError escapes.
    """
    from r6 import audit as audit_mod
    monkeypatch.setattr(audit_mod, 'add_audit_event', _storage_down)
    _assert_refusal_survives(client, tenant_id, step_up_token, caplog)


def test_a_failed_audit_commit_leaves_no_row_and_answers_the_refusal(
        client, tenant_id, step_up_token, caplog, monkeypatch):
    """The row flushed, then the commit failed: it must be rolled back, not
    left pending (install_audit_assertions would fail the request)."""
    monkeypatch.setattr(db.session, 'commit', _storage_down)
    _assert_refusal_survives(client, tenant_id, step_up_token, caplog)


# ---------------------------------------------------------------------------
# The exclusions are named, and each one that can be driven writes nothing
# ---------------------------------------------------------------------------

def test_the_exclusions_are_a_decision():
    """Adding an exclusion is a two-file change a reviewer sees."""
    assert set(access._UNAUDITED_REFUSALS) == {
        'has_grant',
        'decide_grant',
        'unchecked StepUpDenied',
        'validator exception',
        'TenantRejected',
        'direct validate_step_up_token sites',
        'over the refusal-audit budget',
        'audit storage failure',
    }
    assert all(reason.strip() for reason in access._UNAUDITED_REFUSALS.values())


def test_a_has_grant_or_decide_grant_refusal_writes_no_row(
        app, tenant_id, step_up_token):
    """The caller renders these, so the kernel records nothing — the rate
    limiter asks has_grant on every request."""
    before = {r.id for r in _rows()}
    headers = {'X-Tenant-Id': tenant_id,
               'X-Step-Up-Token': step_up_token + 'x'}
    with app.test_request_context('/probe', method='POST', headers=headers):
        tenant = tenant_from_request(sources=(TenantSource.HEADER,))
        assert has_grant(scope=Scope.WRITE, tenant=tenant) is None
        assert not decide_grant(scope=Scope.WRITE, tenant=tenant).granted
    assert _new_rows(before) == []


def test_a_tenant_rejection_writes_no_row(app):
    """No validated tenant exists to attribute a row to."""
    @app.route('/kernel/no-tenant', methods=['POST'])
    def no_tenant():
        tenant_from_request(sources=(TenantSource.HEADER,))
        raise AssertionError('unreachable')

    before = {r.id for r in _rows()}
    client = app.test_client()
    assert client.post('/kernel/no-tenant').status_code == 400
    assert client.post('/kernel/no-tenant',
                       headers={'X-Tenant-Id': 'bad tenant!'}).status_code == 400
    assert _new_rows(before) == []


def test_an_unchecked_denial_is_a_500_and_writes_no_row(app, tenant_id):
    app.config['PROPAGATE_EXCEPTIONS'] = False

    @app.route('/kernel/stray-denial')
    def stray_denial():
        raise StepUpDenied('raised by a helper', http_status=401,
                           tenant_id=tenant_id)

    before = {r.id for r in _rows()}
    assert app.test_client().get('/kernel/stray-denial').status_code == 500
    assert _new_rows(before) == []

