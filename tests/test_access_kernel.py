"""Behavior tests for the access kernel (r6/access.py).

Slice 1 of `docs/2026-08-03-access-kernel-spec.md`: the kernel exists and is
adopted by nothing. Everything here tests `r6.access` directly, because there
is no call site to observe it through yet — that is the point of the slice.

Three of these are structural rather than behavioral, and they ship now
because they cannot honestly ship later:

- `test_the_kernel_is_not_adopted_yet` — the zero-risk claim, mechanised.
- `test_no_guard_call_sits_inside_a_swallowing_try` — spec §4.1. A guard added
  after the violations exist gets an allowlist, and an allowlist is where this
  defect class comes back.
- `test_the_checked_flag_is_set_in_exactly_one_place` — spec §1.2.
"""

import ast
import dataclasses
import json
import pathlib
import re

import pytest
from flask import Flask

from models import db
from r6.access import (
    AuditAssertionError,
    Grant,
    Profile,
    Scope,
    StepUpDenied,
    Tenant,
    TenantRejected,
    TenantSource,
    audit,
    fhir_response,
    install_audit_assertions,
    install_read_audit_assertion,
    outcome_response,
    register_error_handlers,
    require_grant,
    tenant_from_request,
    unredacted_response,
)
from r6.stepup import generate_step_up_token

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directories that ship as the product. tests/ is excluded on purpose: a test
# is allowed to import the kernel, and in slice 1 only a test may.
_PRODUCTION_DIRS = ('r6', 'careagents', 'adapters', 'api', 'services',
                    'scripts', 'openclaw', 'hermes', 'migrations')
_PRODUCTION_ROOT_FILES = ('main.py', 'app.py', 'models.py')


def _production_python_files():
    for name in _PRODUCTION_ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path
    for folder in _PRODUCTION_DIRS:
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            yield path


# ---------------------------------------------------------------------------
# §1.1 tenant_from_request
# ---------------------------------------------------------------------------

def test_a_well_formed_header_yields_a_tenant_that_names_its_source(app):
    """MUTATION: drop `source=source` from _validated -> red."""
    with app.test_request_context(headers={'X-Tenant-Id': 'acme-1'}):
        tenant = tenant_from_request()
    assert tenant == Tenant(id='acme-1', source=TenantSource.HEADER)


def test_a_missing_tenant_is_rejected_as_absent(app):
    """MUTATION: return Tenant('', HEADER) instead of raising -> red."""
    with app.test_request_context():
        with pytest.raises(TenantRejected) as exc:
            tenant_from_request()
    assert exc.value.reason == 'absent'


@pytest.mark.parametrize('bad', [
    'has space', 'sql;drop', 'a' * 65, '../etc/passwd', ' leading',
])
def test_a_malformed_tenant_is_rejected_whatever_the_source(app, bad):
    """THE ONE PROPERTY: nothing that fails [A-Za-z0-9_-]{1,64} gets out.

    MUTATION: delete the fullmatch check in _validated -> red.

    ' leading' is here deliberately: r6/routes.py:239 matches the raw header,
    so a leading space is malformed today and must stay malformed. Trimming
    inside the kernel would widen the accepted set during migration.
    """
    with app.test_request_context(headers={'X-Tenant-Id': bad}):
        with pytest.raises(TenantRejected) as exc:
            tenant_from_request()
    assert exc.value.reason == 'malformed'


def test_sources_are_an_ordered_tuple_and_the_first_match_wins(app):
    """THE ONE PROPERTY that forced a tuple over a frozenset.

    MUTATION: sort or set()-ify `sources` inside tenant_from_request -> red.
    """
    ctx = dict(query_string={'tenant_id': 'from-query'},
               headers={'X-Tenant-Id': 'from-header'})
    with app.test_request_context('/x', **ctx):
        header_first = tenant_from_request(
            sources=(TenantSource.HEADER, TenantSource.QUERY))
        query_first = tenant_from_request(
            sources=(TenantSource.QUERY, TenantSource.HEADER))
    assert header_first.id == 'from-header'
    assert header_first.source is TenantSource.HEADER
    assert query_first.id == 'from-query'
    assert query_first.source is TenantSource.QUERY


def test_an_earlier_source_that_yields_nothing_falls_through(app):
    with app.test_request_context('/x', query_string={'tenant_id': 'q'}):
        tenant = tenant_from_request(
            sources=(TenantSource.HEADER, TenantSource.QUERY))
    assert tenant == Tenant(id='q', source=TenantSource.QUERY)


def test_query_keys_are_consulted_in_the_order_given(app):
    with app.test_request_context('/x', query_string={'t': 'short',
                                                      'tenant': 'long'}):
        tenant = tenant_from_request(sources=(TenantSource.QUERY,),
                                     query_keys=('tenant', 't'))
    assert tenant.id == 'long'


def test_the_body_source_reads_the_named_field_only(app):
    body = {'tenant_id': 'body-tenant', 'other': 'ignored'}
    with app.test_request_context('/x', json=body):
        assert tenant_from_request(sources=(TenantSource.BODY,)).id == 'body-tenant'
    with app.test_request_context('/x', json=body):
        with pytest.raises(TenantRejected):
            tenant_from_request(sources=(TenantSource.BODY,), body_key='nope')


def test_the_session_source_reads_the_command_center_cookie(app):
    from r6.read_auth import TENANT_SESSION_KEY
    with app.test_request_context('/x'):
        from flask import session
        session[TENANT_SESSION_KEY] = 'cc-tenant'
        tenant = tenant_from_request(sources=(TenantSource.SESSION,
                                              TenantSource.HEADER))
    assert tenant == Tenant(id='cc-tenant', source=TenantSource.SESSION)


def test_the_default_is_last_even_when_declared_first(app):
    """MUTATION: consult DEFAULT in tuple position -> red."""
    with app.test_request_context(headers={'X-Tenant-Id': 'real'}):
        tenant = tenant_from_request(
            sources=(TenantSource.DEFAULT, TenantSource.HEADER),
            default='fallback')
    assert tenant == Tenant(id='real', source=TenantSource.HEADER)


def test_the_default_applies_only_when_nothing_else_answered(app):
    with app.test_request_context():
        tenant = tenant_from_request(sources=(TenantSource.HEADER,),
                                     default='desktop-demo')
    assert tenant == Tenant(id='desktop-demo', source=TenantSource.DEFAULT)


def test_a_malformed_default_fails_loudly_rather_than_at_query_time(app):
    """A typo'd literal is a bug at the endpoint, not a mystery empty result.

    MUTATION: return the default without validating it -> red.
    """
    with app.test_request_context():
        with pytest.raises(TenantRejected) as exc:
            tenant_from_request(default='desktop demo')
    assert exc.value.reason == 'malformed'


def test_the_sharp_source_honours_the_ssrf_guard(app, monkeypatch):
    """A SHARP tenant is synthesized only from an upstream that PASSED the
    guard — the property r6/fhir_proxy.py:816 exists to hold.

    MUTATION: drop the is_sharp_context_active() check in _sharp_tenant ->
    red (the second half starts returning a tenant).
    """
    headers = {'X-FHIR-Server-URL': 'https://fhir.example.org/r4'}

    monkeypatch.setattr('r6.fhir_proxy.is_sharp_context_active', lambda: True)
    with app.test_request_context('/x', headers=headers):
        tenant = tenant_from_request(sources=(TenantSource.HEADER,
                                              TenantSource.SHARP))
    assert tenant.source is TenantSource.SHARP
    assert tenant.id.startswith('sharp-')

    monkeypatch.setattr('r6.fhir_proxy.is_sharp_context_active', lambda: False)
    with app.test_request_context('/x', headers=headers):
        with pytest.raises(TenantRejected):
            tenant_from_request(sources=(TenantSource.HEADER,
                                         TenantSource.SHARP))


def test_the_synthesized_sharp_tenant_matches_the_shipped_formula(app,
                                                                  monkeypatch):
    """Pins the digest against r6/routes.py:223-228 so slice 11k is a move."""
    import hashlib
    url = 'https://fhir.example.org/r4'
    expected = 'sharp-' + hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    monkeypatch.setattr('r6.fhir_proxy.is_sharp_context_active', lambda: True)
    with app.test_request_context('/x', headers={'X-FHIR-Server-URL': url}):
        assert tenant_from_request(sources=(TenantSource.SHARP,)).id == expected


def test_the_kernel_accepts_exactly_what_the_shipped_pattern_accepts(app):
    """Slices 9-11 must be a move, not a change: the kernel's format rule and
    r6/routes.py:97 have to agree on every candidate, or migrating a tenant
    read silently widens or narrows what an endpoint accepts.

    MUTATION: change the kernel's character class -> red.
    """
    from r6.access import _TENANT_ID_PATTERN as kernel_pattern
    from r6.routes import _TENANT_ID_PATTERN as shipped_pattern

    candidates = ['acme', 'ACME_1', 'a-b_c', 'a' * 64, 'a' * 65, '', ' ',
                  'a b', 'a.b', 'a/b', 'a\nb', 'sharp-0123456789abcdef',
                  'tenant\n', '../x', 'ünïcode']
    for value in candidates:
        assert bool(kernel_pattern.fullmatch(value)) is \
            bool(shipped_pattern.fullmatch(value)), value


def test_a_tenant_is_frozen(app):
    with pytest.raises(dataclasses.FrozenInstanceError):
        Tenant(id='a', source=TenantSource.HEADER).id = 'b'


# ---------------------------------------------------------------------------
# §1.2 require_grant
# ---------------------------------------------------------------------------

def _headers(token=None, **extra):
    headers = dict(extra)
    if token:
        headers['X-Step-Up-Token'] = token
    return headers


def test_a_valid_write_token_yields_a_grant(app, tenant_id):
    """THE ONE PROPERTY: a Grant exists only after the validator said True.

    MUTATION: return Grant(...) before calling validate_step_up_token -> the
    refusal tests below go red.
    """
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant)
    assert grant == Grant(tenant_id=tenant_id, scope=Scope.WRITE,
                          audience=None, operation=None, nonce_consumed=False)


def test_an_absent_token_is_denied_with_the_absent_status(app, tenant_id):
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context():
        with pytest.raises(StepUpDenied) as exc:
            require_grant(scope=Scope.WRITE, tenant=tenant, absent_status=403)
    assert exc.value.http_status == 403
    assert exc.value.checked is True


def test_a_rejected_token_is_denied_with_the_rejected_status(app, tenant_id):
    """The 403 dialect (r6/wearables/routes.py:257-261) survives migration."""
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers('not-a-token')):
        with pytest.raises(StepUpDenied) as exc:
            require_grant(scope=Scope.WRITE, tenant=tenant,
                          absent_status=401, rejected_status=403)
    assert exc.value.http_status == 403
    assert exc.value.checked is True


def test_a_token_for_another_tenant_never_yields_a_grant(app, tenant_id):
    """MUTATION: pass a literal instead of tenant.id to the validator -> red."""
    token = generate_step_up_token('some-other-tenant')
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant)


def test_a_read_scoped_token_cannot_buy_a_write_grant(app, tenant_id):
    """H4: Scope.WRITE maps to require_scope='write', which rejects it.

    MUTATION: map Scope.WRITE to None in _SCOPE_REQUIREMENT -> red.
    """
    token = generate_step_up_token(tenant_id, scope='read')
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant)


def test_a_read_scoped_token_does_buy_a_tenant_bound_grant(app, tenant_id):
    """r6/read_auth.py:88-93 passes require_scope=None and that is
    load-bearing — slice 8 depends on this mapping.

    MUTATION: map Scope.TENANT_BOUND to 'write' -> red.
    """
    token = generate_step_up_token(tenant_id, scope='read')
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        grant = require_grant(scope=Scope.TENANT_BOUND, tenant=tenant)
    assert grant.scope is Scope.TENANT_BOUND


def test_the_bearer_header_is_ignored_unless_the_endpoint_opted_in(app,
                                                                  tenant_id):
    """MUTATION: read Authorization unconditionally -> the first half red."""
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    headers = {'Authorization': f'Bearer {token}'}

    with app.test_request_context(headers=headers):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant)

    with app.test_request_context(headers=headers):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant,
                              also_bearer=True)
    assert grant.tenant_id == tenant_id


def test_the_body_field_is_ignored_unless_the_endpoint_named_it(app, tenant_id):
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)

    with app.test_request_context('/x', json={'step_up_token': token}):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant)

    with app.test_request_context('/x', json={'step_up_token': token}):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant,
                              also_body_field='step_up_token')
    assert grant.tenant_id == tenant_id


def test_the_step_up_header_outranks_the_opted_in_sources(app, tenant_id):
    good = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    headers = _headers('garbage', Authorization=f'Bearer {good}')
    with app.test_request_context(headers=headers):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant, also_bearer=True)


def test_audience_and_operation_bindings_are_enforced(app, tenant_id):
    """MUTATION: stop forwarding require_audience/require_operation -> red."""
    token = generate_step_up_token(tenant_id, audience='actions',
                                   operation='commit')
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)

    with app.test_request_context(headers=_headers(token)):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant,
                              audience='actions', operation='commit')
    assert grant.audience == 'actions' and grant.operation == 'commit'

    with app.test_request_context(headers=_headers(token)):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant,
                          audience='actions', operation='cancel')


def test_consume_nonce_makes_a_token_single_use(app, tenant_id):
    from r6.stepup import clear_nonce_cache
    clear_nonce_cache()
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)

    with app.test_request_context(headers=_headers(token)):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant,
                              consume_nonce=True)
    assert grant.nonce_consumed is True

    with app.test_request_context(headers=_headers(token)):
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant, consume_nonce=True)
    clear_nonce_cache()


def test_require_grant_refuses_a_raw_string_tenant(app, tenant_id):
    """A str here means the caller skipped format validation.

    MUTATION: accept `str` by falling back to `tenant` when it has no .id ->
    red.
    """
    token = generate_step_up_token(tenant_id)
    with app.test_request_context(headers=_headers(token)):
        with pytest.raises(TypeError):
            require_grant(scope=Scope.WRITE, tenant=tenant_id)


def test_a_grant_is_frozen_so_a_handler_cannot_widen_it(app, tenant_id):
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        grant = require_grant(scope=Scope.WRITE, tenant=tenant)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.tenant_id = 'someone-else'


def test_a_denial_never_repeats_the_validators_reason_to_the_client(app,
                                                                    tenant_id):
    """r6/read_auth.py:262 refuses to say why a token failed; so does this.

    A per-reason answer ('Token tenant mismatch' vs 'Step-up token expired')
    is an oracle for a caller probing another tenant.
    """
    expired = generate_step_up_token(tenant_id, ttl_seconds=-10)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(expired)):
        with pytest.raises(StepUpDenied) as exc:
            require_grant(scope=Scope.WRITE, tenant=tenant)
    assert exc.value.reason == 'Invalid step-up token'


def test_step_up_denied_is_catchable_as_exception(app):
    """Flask calls handle_user_exception from inside `except Exception`.

    MUTATION: make StepUpDenied subclass BaseException -> red, and the
    errorhandler would silently stop running in production.
    """
    assert issubclass(StepUpDenied, Exception)
    assert StepUpDenied.__bases__ == (Exception,)


# ---------------------------------------------------------------------------
# §1.2 register_error_handlers
# ---------------------------------------------------------------------------

def _handler_app():
    handler_app = Flask(__name__)
    handler_app.config['TESTING'] = True
    register_error_handlers(handler_app)
    return handler_app


def test_a_checked_denial_renders_as_an_operation_outcome():
    handler_app = _handler_app()

    @handler_app.route('/denied')
    def denied():
        raise StepUpDenied('Step-up token required', http_status=401,
                           **{'checked': True})

    response = handler_app.test_client().get('/denied')
    assert response.status_code == 401
    body = response.get_json()
    assert body['resourceType'] == 'OperationOutcome'
    assert body['issue'][0]['code'] == 'security'


def test_an_unchecked_denial_is_re_raised_and_becomes_a_500():
    """THE ONE PROPERTY of the checked flag. Without it the errorhandler turns
    any stray raise into a clean 401 that reads like a working guard.

    MUTATION: delete the `if not exc.checked: raise exc` branch -> red.
    """
    handler_app = _handler_app()
    handler_app.config['PROPAGATE_EXCEPTIONS'] = False

    @handler_app.route('/stray')
    def stray():
        raise StepUpDenied('raised by a helper', http_status=401)

    response = handler_app.test_client().get('/stray')
    assert response.status_code == 500


def test_a_rejected_tenant_renders_the_shipped_400s():
    """Matches r6/routes.py:230-247 exactly, both code and status."""
    handler_app = _handler_app()

    @handler_app.route('/absent')
    def absent():
        raise TenantRejected('absent')

    @handler_app.route('/malformed')
    def malformed():
        raise TenantRejected('malformed')

    client = handler_app.test_client()
    absent_response = client.get('/absent')
    malformed_response = client.get('/malformed')
    assert absent_response.status_code == 400
    assert absent_response.get_json()['issue'][0]['code'] == 'security'
    assert malformed_response.status_code == 400
    assert malformed_response.get_json()['issue'][0]['code'] == 'invalid'


def test_register_error_handlers_installs_handlers_only():
    """HANDLERS ONLY — r6/sdc/delivery.py must keep working with no headers.

    MUTATION: add a before_request hook in register_error_handlers -> red.
    """
    before = Flask(__name__)
    hooks_before = (len(before.before_request_funcs),
                    len(before.after_request_funcs),
                    len(before.teardown_request_funcs),
                    len(before.url_value_preprocessors))
    register_error_handlers(before)
    hooks_after = (len(before.before_request_funcs),
                   len(before.after_request_funcs),
                   len(before.teardown_request_funcs),
                   len(before.url_value_preprocessors))
    assert hooks_before == hooks_after
    assert StepUpDenied in before.error_handler_spec[None][None]
    assert TenantRejected in before.error_handler_spec[None][None]


# ---------------------------------------------------------------------------
# §1.3 audit
# ---------------------------------------------------------------------------

def test_audit_flushes_a_row_without_committing(app, tenant_id):
    """THE ONE PROPERTY: flushed in the caller's unit of work, never committed.

    MUTATION: call db.session.commit() inside audit() -> red (the row
    survives the rollback).
    """
    from r6.models import AuditEventRecord
    with app.test_request_context():
        audit(tenant=Tenant(id=tenant_id, source=TenantSource.HEADER),
              event_type='read', resource_type='Observation',
              resource_id='obs-1')
        assert AuditEventRecord.query.filter_by(
            tenant_id=tenant_id).count() == 1
        db.session.rollback()
        assert AuditEventRecord.query.filter_by(
            tenant_id=tenant_id).count() == 0


def test_audit_accepts_a_bare_tenant_id_string(app, tenant_id):
    from r6.models import AuditEventRecord
    with app.test_request_context():
        audit(tenant=tenant_id, event_type='read', detail='string tenant')
        row = AuditEventRecord.query.filter_by(tenant_id=tenant_id).one()
        assert row.detail == 'string tenant'
        db.session.rollback()


def test_audit_forwards_every_field_to_the_shipped_writer(app, tenant_id,
                                                          monkeypatch):
    """§1.0: patched by module path, because that is how the suite patches.

    MUTATION: change r6/access.py to `from r6.audit import add_audit_event`
    -> red.
    """
    seen = {}

    def fake(event_type, **kwargs):
        seen['event_type'] = event_type
        seen.update(kwargs)

    monkeypatch.setattr('r6.audit.add_audit_event', fake)
    with app.test_request_context():
        audit(tenant=Tenant(id=tenant_id, source=TenantSource.HEADER),
              event_type='create', resource_type='Observation',
              resource_id='o1', agent_id='a1', context_id='c1',
              outcome='failure', detail='no phi here',
              outcome_detail_code='code-1')
    assert seen == {
        'event_type': 'create', 'resource_type': 'Observation',
        'resource_id': 'o1', 'agent_id': 'a1', 'context_id': 'c1',
        'outcome': 'failure', 'detail': 'no phi here',
        'tenant_id': tenant_id, 'outcome_detail_code': 'code-1',
    }


# ---------------------------------------------------------------------------
# §1.3.1 install_audit_assertions
# ---------------------------------------------------------------------------

def _audit_routes(target_app, tenant):
    @target_app.route('/kernel/flush-only', methods=['POST'])
    def flush_only():
        audit(tenant=tenant, event_type='create', detail='flush only')
        return '', 201

    @target_app.route('/kernel/flush-and-commit', methods=['POST'])
    def flush_and_commit():
        audit(tenant=tenant, event_type='create', detail='committed')
        db.session.commit()
        return '', 201

    @target_app.route('/kernel/flush-and-rollback', methods=['POST'])
    def flush_and_rollback():
        audit(tenant=tenant, event_type='create', detail='refused')
        db.session.rollback()
        return '', 400

    @target_app.route('/kernel/no-audit', methods=['POST'])
    def no_audit():
        return '', 201


def test_a_flushed_audit_row_that_is_never_committed_fails_the_request(
        app, tenant_id):
    """THE ONE PROPERTY: no request leaves an audit row flushed-but-uncommitted.

    MUTATION: delete the raise in _assert_audit_committed -> red. This is the
    control that makes moving 41 sites off the ambient commit survivable.
    """
    _audit_routes(app, tenant_id)
    install_audit_assertions(app)
    with pytest.raises(AuditAssertionError):
        app.test_client().post('/kernel/flush-only')


def test_committing_or_rolling_back_satisfies_the_assertion(app, tenant_id):
    """A refused write carries no audit row and passes, correctly."""
    _audit_routes(app, tenant_id)
    install_audit_assertions(app)
    client = app.test_client()
    assert client.post('/kernel/flush-and-commit').status_code == 201
    assert client.post('/kernel/flush-and-rollback').status_code == 400
    assert client.post('/kernel/no-audit').status_code == 201


def test_the_assertion_is_inert_for_a_request_that_never_audited(app,
                                                                 tenant_id):
    """Unmigrated sites use record_audit_event, which sets no marker, so the
    guard arrives with the first migrated site rather than all at once."""
    from r6.audit import record_audit_event

    @app.route('/kernel/legacy-audit', methods=['POST'])
    def legacy_audit():
        record_audit_event('create', tenant_id=tenant_id, detail='legacy')
        return '', 201

    install_audit_assertions(app)
    assert app.test_client().post('/kernel/legacy-audit').status_code == 201


def test_the_marker_does_not_leak_from_one_request_into_the_next(app,
                                                                 tenant_id):
    """flask.g is app-context scoped and a test can share one across requests.

    MUTATION: delete _install_marker_cleanup's teardown -> red (the second,
    innocent request inherits the first's pending marker).
    """
    _audit_routes(app, tenant_id)
    install_audit_assertions(app)
    client = app.test_client()
    with pytest.raises(AuditAssertionError):
        client.post('/kernel/flush-only')
    db.session.rollback()
    assert client.post('/kernel/no-audit').status_code == 201


def test_the_assertion_is_off_when_neither_testing_nor_the_env_var_is_set(
        app, tenant_id, monkeypatch):
    _audit_routes(app, tenant_id)
    install_audit_assertions(app)
    monkeypatch.delenv('HC_ASSERT_AUDIT_COMMITTED', raising=False)
    monkeypatch.setitem(app.config, 'TESTING', False)
    assert app.test_client().post('/kernel/flush-only').status_code == 201
    db.session.rollback()


# ---------------------------------------------------------------------------
# §1.3.1 install_read_audit_assertion
# ---------------------------------------------------------------------------

def _read_routes(target_app, tenant):
    @target_app.route('/r6/fhir/Widget/<wid>')
    def widget(wid):
        if wid == 'audited':
            audit(tenant=tenant, event_type='read', resource_type='Widget',
                  resource_id=wid)
            db.session.commit()
            return {'resourceType': 'Widget', 'id': wid}
        if wid == 'missing':
            return {'resourceType': 'OperationOutcome'}, 404
        return {'resourceType': 'Widget', 'id': wid}

    @target_app.route('/r6/fhir/discovery-ish')
    def discovery_ish():
        return {'resourceType': 'CapabilityStatement'}


def test_a_fhir_read_that_never_audited_fails_the_request(app, tenant_id):
    """THE ONE PROPERTY: a /r6/fhir/ resource route answering 200 or 404
    emitted an AuditEvent. This is the structural answer to S-9 — the five
    unaudited 404 paths. It is defined here and installed by NOTHING, because
    installing it goes red on those five paths and that redness is its own
    slice (spec §2.7, 12x).

    MUTATION: delete the raise in _assert_read_audited -> red.
    """
    _read_routes(app, tenant_id)
    install_read_audit_assertion(app)
    client = app.test_client()
    with pytest.raises(AuditAssertionError):
        client.get('/r6/fhir/Widget/unaudited')
    with pytest.raises(AuditAssertionError):
        client.get('/r6/fhir/Widget/missing')


def test_a_fhir_read_that_audited_and_committed_passes(app, tenant_id):
    """The emitted marker must survive the commit — a read that audits and
    commits is the correct shape, not a violation."""
    _read_routes(app, tenant_id)
    install_read_audit_assertion(app)
    assert app.test_client().get('/r6/fhir/Widget/audited').status_code == 200


def test_the_read_assertion_ignores_discovery_and_non_reads(app, tenant_id):
    _read_routes(app, tenant_id)
    install_read_audit_assertion(app)
    client = app.test_client()
    assert client.get('/r6/fhir/discovery-ish').status_code == 200
    assert client.post('/r6/fhir/Widget/unaudited').status_code == 405


# ---------------------------------------------------------------------------
# §1.4 response shaping
# ---------------------------------------------------------------------------

_PATIENT = {
    'resourceType': 'Patient',
    'id': 'p1',
    'name': [{'family': 'Rivera', 'given': ['Marisol']}],
    'birthDate': '1984-07-02',
    'telecom': [{'system': 'phone', 'value': '555-0100'}],
    'text': {'status': 'generated', 'div': '<div>Marisol Rivera</div>'},
    'note': [{'text': 'patient reports chest pain'}],
    'identifier': [
        {'system': 'http://hl7.org/fhir/sid/us-ssn', 'value': '123-45-6789'},
        {'system': 'urn:mrn', 'value': 'MRN-99'},
    ],
}


def test_there_is_no_profile_member_meaning_no_redaction():
    """The opt-out is a different function with a different signature.

    MUTATION: add Profile.NONE -> red.
    """
    assert {p.name for p in Profile} == {'STANDARD', 'PATIENT_CONTROLLED',
                                         'INTAKE'}


def test_the_standard_profile_strips_upstream_display_and_adds_a_disclaimer(
        app):
    """MUTATION: return jsonify(payload) without apply_redaction -> red.

    Guards the non-negotiable directly: an upstream `display` carrying a
    patient name never reaches the wire.
    """
    payload = {
        'resourceType': 'Observation',
        'id': 'o1',
        'code': {'coding': [{'system': 'http://loinc.org', 'code': '8480-6',
                             'display': 'Rivera, Marisol'}],
                 'text': 'Rivera, Marisol'},
    }
    with app.test_request_context():
        response = fhir_response(payload, profile=Profile.STANDARD,
                                 resource_type='Observation')
    body = json.loads(response.get_data(as_text=True))
    coding = body['code']['coding'][0]
    assert coding.get('display') != 'Rivera, Marisol'
    assert body['code'].get('text') != 'Rivera, Marisol'
    assert '_disclaimer' in body


def test_the_standard_profile_carries_the_status_and_etag_it_was_given(app):
    with app.test_request_context():
        response = fhir_response({'resourceType': 'Patient'},
                                 profile=Profile.STANDARD, status=201,
                                 etag='W/"3"')
    assert response.status_code == 201
    assert response.headers['ETag'] == 'W/"3"'


def test_the_patient_controlled_profile_requires_the_patient_id(app):
    """The spec's §1.4 signature omits patient_id, but
    apply_patient_controlled_redaction(resource, patient_id) cannot run
    without it. Named and required rather than defaulted to something."""
    with app.test_request_context():
        with pytest.raises(ValueError):
            fhir_response(dict(_PATIENT), profile=Profile.PATIENT_CONTROLLED)
        response = fhir_response(dict(_PATIENT),
                                 profile=Profile.PATIENT_CONTROLLED,
                                 patient_id='hc-123')
    body = json.loads(response.get_data(as_text=True))
    assert 'name' not in body and 'telecom' not in body
    assert body['birthDate'] == '1984-07-02'


def test_a_patient_id_on_another_profile_is_refused(app):
    """It would read as though patient-controlled redaction had run."""
    with app.test_request_context():
        with pytest.raises(ValueError):
            fhir_response({'resourceType': 'Patient'},
                          profile=Profile.STANDARD, patient_id='hc-123')


def test_the_intake_profile_matches_the_shipped_intake_strip(app):
    """The kernel carries a copy of r6/routes.py:_intake_strip until slice 14
    deletes the original. Pinned equal so the copy cannot drift while it waits.

    MUTATION: drop the SSN filter from _intake_profile -> red.
    """
    from r6.routes import _intake_strip
    expected = _intake_strip(json.loads(json.dumps(_PATIENT)))
    with app.test_request_context():
        response = fhir_response(json.loads(json.dumps(_PATIENT)),
                                 profile=Profile.INTAKE)
    body = json.loads(response.get_data(as_text=True))
    assert body == expected
    assert [i['system'] for i in body['identifier']] == ['urn:mrn']
    assert 'note' not in body and 'text' not in body


def test_fhir_response_refuses_anything_that_is_not_a_profile(app):
    with app.test_request_context():
        with pytest.raises(TypeError):
            fhir_response({'resourceType': 'Patient'}, profile='standard')


def test_the_redaction_module_is_reached_by_module_path(app, monkeypatch):
    """§1.0 again, for r6.redaction.

    MUTATION: change r6/access.py to `from r6.redaction import
    apply_redaction` -> red.
    """
    monkeypatch.setattr('r6.redaction.apply_redaction',
                        lambda res: {'resourceType': 'Patched'})
    with app.test_request_context():
        response = fhir_response({'resourceType': 'Patient'},
                                 profile=Profile.STANDARD)
    assert json.loads(response.get_data(as_text=True))['resourceType'] == 'Patched'


def test_an_unredacted_exit_must_be_on_the_allowlist(app):
    """THE ONE PROPERTY: the endpoint name appears in _UNREDACTED_EXITS.

    MUTATION: delete the membership check -> red. Raises at request time in
    every environment, not only under a flag.
    """
    with app.test_request_context():
        with pytest.raises(RuntimeError):
            unredacted_response({'resourceType': 'Bundle'},
                                endpoint='r6.patient_read',
                                reason='I just needed the raw record')


def test_an_allowlisted_exit_answers_untouched(app):
    payload = {'resourceType': 'Bundle', 'entry': [
        {'resource': {'resourceType': 'SubscriptionTopic',
                      'title': 'kept verbatim'}}]}
    with app.test_request_context():
        response = unredacted_response(
            payload, endpoint='r6.subscription_topics',
            reason='SubscriptionTopic is server metadata, never patient data')
    assert json.loads(response.get_data(as_text=True)) == payload


def test_the_reason_is_required_and_never_reaches_the_client(app):
    with app.test_request_context():
        with pytest.raises(ValueError):
            unredacted_response({'resourceType': 'Bundle'},
                                endpoint='r6.audit_search', reason='   ')
        response = unredacted_response(
            {'resourceType': 'Bundle'}, endpoint='r6.audit_search',
            reason='AuditEventRecord is PHI-free by construction')
    assert 'PHI-free' not in response.get_data(as_text=True)


def test_outcome_response_is_the_shipped_shape_with_a_status(app):
    with app.test_request_context():
        response = outcome_response('error', 'not-found', 'Widget/1 not found',
                                    status=404)
    assert response.status_code == 404
    assert json.loads(response.get_data(as_text=True)) == {
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': 'error', 'code': 'not-found',
                   'diagnostics': 'Widget/1 not found'}],
    }


# ---------------------------------------------------------------------------
# Structural guards — these ship before the first adoption, on purpose
# ---------------------------------------------------------------------------

def test_the_kernel_is_not_adopted_yet():
    """MUTATION: import r6.access from r6/routes.py -> red.

    Slice 1's entire risk argument is this assertion. The kernel cannot change
    any behavior while no production module can reach it.
    """
    importers = []
    scanned = 0
    for path in _production_python_files():
        scanned += 1
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == 'r6.access' or
                       alias.name.startswith('r6.access.')
                       for alias in node.names):
                    importers.append(f'{path.relative_to(REPO_ROOT)}:{node.lineno}')
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                named_access = (module == 'r6' and
                                any(a.name == 'access' for a in node.names))
                if module == 'r6.access' or module.startswith('r6.access.') \
                        or named_access:
                    importers.append(f'{path.relative_to(REPO_ROOT)}:{node.lineno}')
    # A scan that walks nothing would pass this test forever. 172 production
    # modules exist today; the floor only has to catch a broken path list.
    assert scanned > 100, f'the adoption scan only walked {scanned} files'
    assert not importers, (
        'slice 1 adopts the kernel nowhere, but it is imported by: '
        + ', '.join(importers))


def test_the_checked_flag_is_set_in_exactly_one_place():
    """Spec §1.2. The flag is what stops the errorhandler turning a stray
    raise into a clean 401, so it must not be copyable.

    MUTATION: add a second one anywhere (a helper, a test double) -> red.
    """
    needle = re.compile(r'\bchecked\s*=\s*' + 'True')
    hits = []
    for path in sorted(REPO_ROOT.rglob('*.py')):
        # Relative to REPO_ROOT: an agent worktree lives under .claude/, so
        # testing the absolute parts would skip the entire repository.
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & {'.venv', '.git', '__pycache__', 'node_modules', '.claude'}:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding='utf-8').splitlines(), start=1):
            if needle.search(line):
                hits.append((path, lineno))

    assert len(hits) == 1, (
        'the checked flag must be set on exactly one line; found: '
        + ', '.join(f'{p.relative_to(REPO_ROOT)}:{n}' for p, n in hits))

    path, lineno = hits[0]
    assert path == REPO_ROOT / 'r6' / 'access.py'

    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    enclosing = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    assert enclosing == ['require_grant'], (
        f'the checked flag is set inside {enclosing}, not require_grant')


# The names whose refusal must never be swallowed. A `raise` from any of these
# is the guard doing its job; a broad except above it turns the refusal into a
# fall-through and the response into a 200.
_GUARD_CALLS = frozenset({
    'require_grant', 'audit', 'tenant_from_request', 'unredacted_response',
})


def _catches_broadly(handler):
    """True when this except clause catches Exception/BaseException/everything."""
    if handler.type is None:  # bare `except:`
        return True
    candidates = (handler.type.elts if isinstance(handler.type, ast.Tuple)
                  else [handler.type])
    for node in candidates:
        name = getattr(node, 'id', None) or getattr(node, 'attr', None)
        if name in ('Exception', 'BaseException'):
            return True
    return False


def _re_raises(handler):
    """True when the handler body contains a bare `raise`."""
    return any(isinstance(node, ast.Raise) and node.exc is None
               for node in ast.walk(handler))


def _guard_calls_in(nodes):
    for node in nodes:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            if name in _GUARD_CALLS:
                yield name, child.lineno


def test_no_guard_call_sits_inside_a_swallowing_try():
    """Spec §4.1. 99 `except Exception` blocks live in r6/; any one wrapped
    around a require_grant call swallows StepUpDenied and lets the handler
    fall through into the write. The guard would look like it fired and the
    response would be a 200.

    MUTATION: wrap a require_grant call in `try: ... except Exception: pass`
    -> red.

    This lands BEFORE the first adoption on purpose. A guard added after the
    violations exist gets an allowlist, and an allowlist is where this defect
    class comes back.
    """
    offenders = []
    for path in sorted((REPO_ROOT / 'r6').rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            swallowing = [h for h in node.handlers
                          if _catches_broadly(h) and not _re_raises(h)]
            if not swallowing:
                continue
            # Only node.body is protected by these handlers — an exception
            # raised in `orelse` or in another handler is not caught here.
            for name, lineno in _guard_calls_in(node.body):
                offenders.append(
                    f'{path.relative_to(REPO_ROOT)}:{lineno} ({name}())')

    assert not offenders, (
        'a kernel guard call sits inside a try that swallows Exception; its '
        'refusal would be discarded and the handler would fall through: '
        + ', '.join(sorted(offenders)))


def test_the_broad_except_guard_actually_detects_the_shape(tmp_path):
    """The §4.1 guard is only worth having if it fires. Proven on a fixture
    rather than by mutating r6/, so the proof survives in CI.
    """
    sample = tmp_path / 'offender.py'
    sample.write_text(
        'def handler():\n'
        '    try:\n'
        '        grant = require_grant(scope=1, tenant=2)\n'
        '    except Exception:\n'
        '        grant = None\n'
        '    return grant\n', encoding='utf-8')
    tree = ast.parse(sample.read_text(encoding='utf-8'))
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert len(tries) == 1
    handler = tries[0].handlers[0]
    assert _catches_broadly(handler) and not _re_raises(handler)
    assert [name for name, _ in _guard_calls_in(tries[0].body)] == ['require_grant']

    reraising = ast.parse(
        'def handler():\n'
        '    try:\n'
        '        require_grant(scope=1, tenant=2)\n'
        '    except Exception:\n'
        '        raise\n')
    handler = [n for n in ast.walk(reraising)
               if isinstance(n, ast.Try)][0].handlers[0]
    assert _re_raises(handler)


def test_the_kernel_resolves_its_collaborators_by_module_attribute():
    """Spec §1.0. 33 tests monkeypatch r6.stepup.* by module path; a
    `from r6.stepup import validate_step_up_token` here binds at import time
    and those tests would keep passing while patching nothing.

    MUTATION: rewrite any of the three as a `from X import name` -> red.
    """
    source = (REPO_ROOT / 'r6' / 'access.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
                'r6.stepup', 'r6.audit', 'r6.redaction'):
            forbidden.append(f'{node.module}:{node.lineno}')
    assert not forbidden, (
        'the kernel binds a collaborator at import time, which silently '
        'unhooks the suite monkeypatches: ' + ', '.join(forbidden))
