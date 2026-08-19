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
import logging
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
    has_grant,
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


def test_a_denial_states_a_reason_about_the_callers_own_token(app, tenant_id):
    """A step-up refusal tells the caller why (owner ruling, 2026-08-10).

    THIS PIN WAS INVERTED, DELIBERATELY. It previously asserted the opposite —
    that a denial never repeats the validator's reason — on the grounds that a
    per-reason answer is an oracle. That reasoning was right about one reason
    and wrong about the other ten.

    An expired token is the caller's own. Telling them it expired discloses
    nothing they could not read out of the token they are holding, and
    withholding it left them with four words and no way to act. The oracle
    argument survives intact for the reason it actually applies to, which the
    test below pins.
    """
    expired = generate_step_up_token(tenant_id, ttl_seconds=-10)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(expired)):
        with pytest.raises(StepUpDenied) as exc:
            require_grant(scope=Scope.WRITE, tenant=tenant)
    assert exc.value.reason == 'Step-up token expired'


def test_a_denial_never_reveals_that_a_token_belongs_to_another_tenant(
        app, tenant_id):
    """The carve-out, and the whole of it.

    'Token tenant mismatch' is the one reason that describes a credential the
    caller should not have. Saying it separates a real token issued elsewhere
    from junk, which is precisely what a prober wants to learn.
    r6/read_auth.py:262 withholds it for the same reason.

    MUTATION: add 'Token tenant mismatch' to _PUBLIC_REASONS -> red.
    """
    other = generate_step_up_token('some-other-tenant')
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(other)):
        with pytest.raises(StepUpDenied) as exc:
            require_grant(scope=Scope.WRITE, tenant=tenant)
    assert exc.value.reason == 'Invalid step-up token'
    assert 'tenant' not in exc.value.reason.lower()


def test_step_up_denied_is_catchable_as_exception(app):
    """Flask calls handle_user_exception from inside `except Exception`.

    MUTATION: make StepUpDenied subclass BaseException -> red, and the
    errorhandler would silently stop running in production.
    """
    assert issubclass(StepUpDenied, Exception)
    assert StepUpDenied.__bases__ == (Exception,)


# ---------------------------------------------------------------------------
# §1.2b has_grant — the same decision, returned instead of raised
# ---------------------------------------------------------------------------
#
# Four step-up call sites are predicates, not gates: a rate limiter that must
# never fail a request, two boolean helpers that also accept a session cookie,
# and one refusal whose wire contract names `dryRun=true`. They cannot call
# require_grant, so they kept calling validate_step_up_token and its tuple.
#
# The property that makes has_grant safe to add is EQUIVALENCE, and it is the
# only thing worth testing hard: `has_grant(...) is None` in exactly the cases
# `require_grant(...)` raises. Two functions that answer the same question
# differently would be worse than the tuple.

#: (label, headers, kwargs) — every way the kernel refuses.
_REFUSALS = [
    ('no token at all', {}, {}),
    ('junk', {'X-Step-Up-Token': 'not-a-token'}, {}),
    ('a token for another tenant', 'other-tenant-token', {}),
    ('an expired token', 'expired-token', {}),
    ('a read-scoped token asked for write', 'read-token', {}),
    ('a bearer the endpoint did not opt into', 'bearer-only', {}),
    ('the wrong audience', 'valid-token', {'audience': 'someone-else'}),
    ('the wrong operation', 'valid-token', {'operation': 'not-this-one'}),
]


def _refusal_headers(marker, tenant_id):
    """Build the headers for one row of _REFUSALS."""
    if isinstance(marker, dict):
        return marker
    if marker == 'other-tenant-token':
        return _headers(generate_step_up_token('some-other-tenant'))
    if marker == 'expired-token':
        return _headers(generate_step_up_token(tenant_id, ttl_seconds=-10))
    if marker == 'read-token':
        return _headers(generate_step_up_token(tenant_id, scope='read'))
    if marker == 'bearer-only':
        # Valid, but presented on a header neither call opted into reading.
        return {'Authorization': f'Bearer {generate_step_up_token(tenant_id)}'}
    if marker == 'valid-token':
        return _headers(generate_step_up_token(tenant_id))
    raise AssertionError(f'unknown refusal marker {marker!r}')


@pytest.mark.parametrize('label,marker,kwargs', _REFUSALS,
                         ids=[row[0] for row in _REFUSALS])
def test_has_grant_answers_none_wherever_require_grant_refuses(
        app, tenant_id, label, marker, kwargs):
    """THE ONE PROPERTY, both sides in one request context.

    MUTATION: make has_grant skip any check require_grant makes — drop the
    audience binding, accept a read token for write, read the bearer without
    being asked — and the row for it goes red on the has_grant half while
    require_grant still refuses.
    """
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    headers = _refusal_headers(marker, tenant_id)

    with app.test_request_context(headers=headers):
        assert has_grant(scope=Scope.WRITE, tenant=tenant, **kwargs) is None
        with pytest.raises(StepUpDenied):
            require_grant(scope=Scope.WRITE, tenant=tenant, **kwargs)


@pytest.mark.parametrize('scope', [Scope.WRITE, Scope.TENANT_BOUND])
def test_has_grant_returns_the_same_grant_require_grant_returns(
        app, tenant_id, scope):
    """Equivalence on the other side: where one grants, so does the other.

    A predicate that were merely stricter would pass every refusal test above
    and quietly deny real callers. This is the half that catches it.
    """
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        assert has_grant(scope=scope, tenant=tenant) == require_grant(
            scope=scope, tenant=tenant)


def test_has_grant_reads_the_opted_in_sources_the_same_way(app, tenant_id):
    """also_bearer / also_body_field mean the same thing in both.

    MUTATION: hardcode also_bearer=False in has_grant's delegation -> red.
    """
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers={'Authorization': f'Bearer {token}'}):
        assert has_grant(scope=Scope.WRITE, tenant=tenant) is None
        grant = has_grant(scope=Scope.WRITE, tenant=tenant, also_bearer=True)
    assert grant is not None and grant.tenant_id == tenant_id


def test_the_grant_it_returns_names_the_tenant_it_proved(app, tenant_id):
    """Why it is not a bool. A caller scopes its next query to
    grant.tenant_id rather than re-reading the header it just checked."""
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        grant = has_grant(scope=Scope.WRITE, tenant=tenant)
    assert grant.tenant_id == tenant_id
    assert grant.nonce_consumed is False


def test_has_grant_cannot_be_asked_to_consume_a_nonce(app, tenant_id):
    """Asking is not spending.

    A predicate that burned a single-use token as a side effect of being
    consulted is the retro's defect shape — a control that looks like one
    thing and quietly does two. The rate limiter consults this on every
    request; if it could consume, it would eat the caller's token before the
    handler that needed it ever ran.

    MUTATION: add consume_nonce to the signature -> red.
    """
    import inspect
    from r6 import access as access_mod

    params = inspect.signature(access_mod.has_grant).parameters
    assert 'consume_nonce' not in params
    assert 'absent_status' not in params and 'rejected_status' not in params, (
        'has_grant makes no HTTP decision — that is why it is not '
        'require_grant')

    # And behaviorally: consulting it twice does not spend the token.
    token = generate_step_up_token(tenant_id)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers(token)):
        assert has_grant(scope=Scope.WRITE, tenant=tenant) is not None
        assert has_grant(scope=Scope.WRITE, tenant=tenant) is not None


def test_has_grant_refuses_a_raw_string_tenant_and_says_which_call(
        app, tenant_id):
    """Same type check as require_grant, and the message names the caller
    rather than the shared helper the traceback actually died in."""
    with app.test_request_context(headers=_headers(
            generate_step_up_token(tenant_id))):
        with pytest.raises(TypeError, match='has_grant'):
            has_grant(scope=Scope.WRITE, tenant=tenant_id)


def test_a_validator_failure_propagates_rather_than_reading_as_a_denial(
        app, tenant_id, monkeypatch):
    """An outage is not an authorization answer.

    If the nonce store is unreachable the validator has decided NOTHING.
    Returning None there would file the outage as "this caller has no grant",
    which is indistinguishable at every call site from a bad token — and on
    r6/read_auth.py's path it would turn a 500 into a silent denial, changing
    behaviour in the slice that adopts it.

    r6/rate_limit.py catches this itself, at the site that has a reason to,
    where the catch is visible to a reviewer.

    MUTATION: wrap the validator call in try/except and return None -> red.
    """
    from r6 import access as access_mod

    def _explode(*_args, **_kwargs):
        raise RuntimeError('nonce store unreachable')

    monkeypatch.setattr(access_mod._stepup_mod, 'validate_step_up_token',
                        _explode)
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with app.test_request_context(headers=_headers('any-token')):
        with pytest.raises(RuntimeError):
            has_grant(scope=Scope.WRITE, tenant=tenant)
        with pytest.raises(RuntimeError):
            require_grant(scope=Scope.WRITE, tenant=tenant)


def test_an_absent_token_is_not_logged_but_a_rejected_one_is(
        app, tenant_id, caplog):
    """The limiter consults this on every request. An anonymous request is
    not an event; a presented-and-invalid token is.

    MUTATION: log unconditionally -> red, and the rate limiter emits one INFO
    line per unauthenticated request on the internet.
    """
    tenant = Tenant(id=tenant_id, source=TenantSource.HEADER)
    with caplog.at_level(logging.INFO, logger='r6.access'):
        with app.test_request_context():
            assert has_grant(scope=Scope.WRITE, tenant=tenant) is None
        assert caplog.records == []

        with app.test_request_context(headers=_headers('not-a-token')):
            assert has_grant(scope=Scope.WRITE, tenant=tenant) is None
        assert [r for r in caplog.records if 'step-up refused' in r.message]


def test_a_grant_is_constructed_in_exactly_one_place():
    """Two entry points, one decision. The equivalence tests above check the
    behaviour; this checks it stays structural rather than duplicated, which
    is what would let the two drift later.

    MUTATION: give has_grant its own Grant(...) -> red.
    """
    source = (REPO_ROOT / 'r6' / 'access.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    built = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and getattr(node.func, 'id', None) == 'Grant']
    assert len(built) == 1, f'Grant is constructed on lines {built}'
    enclosing = [node.name for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and node.lineno <= built[0] <= (node.end_lineno or 0)]
    assert enclosing == ['_evaluate']


# --- the hazard has_grant introduces, and the two tests that hold it -------
#
# `has_grant` where `require_grant` was meant does not refuse. It returns None
# and the handler falls through into the write. The broad-except guard cannot
# see it, because nothing was raised to swallow.

#: Production call sites of has_grant. EMPTY: this slice is the pure addition,
#: adopted by nothing, exactly as the kernel itself landed. A migration slice
#: adds its site here in the same PR that writes it — adoption is a reviewable
#: list, not a thing that happens quietly.
_HAS_GRANT_CALLSITES: frozenset[str] = frozenset()


def _has_grant_calls():
    """(path:lineno, is_discarded) for every production call to has_grant."""
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        discarded = {node.value.lineno for node in ast.walk(tree)
                     if isinstance(node, ast.Expr)
                     and isinstance(node.value, ast.Call)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, 'id', None)
                    or getattr(node.func, 'attr', None))
            if name == 'has_grant':
                yield (f'{path.relative_to(REPO_ROOT)}:{node.lineno}',
                       node.lineno in discarded)


def test_has_grant_is_adopted_by_nothing_yet():
    """MUTATION: call has_grant from any production module -> red."""
    sites = sorted(site for site, _ in _has_grant_calls())
    unexpected = [s for s in sites if s not in _HAS_GRANT_CALLSITES]
    assert not unexpected, (
        'has_grant is a non-raising check: a call site that forgets to act on '
        'the answer is a silent bypass, so each one is listed deliberately in '
        '_HAS_GRANT_CALLSITES by the PR that adopts it. Unlisted: '
        + ', '.join(unexpected))


def test_a_has_grant_call_may_never_have_its_answer_thrown_away():
    """`has_grant(...)` on a line by itself gates nothing at all.

    With require_grant that shape is safe — the refusal is the raise. Here it
    is the whole bypass, and it looks exactly like a guard to a reader.

    MUTATION: write a bare `has_grant(...)` statement anywhere -> red.
    """
    thrown_away = [site for site, discarded in _has_grant_calls() if discarded]
    assert not thrown_away, (
        'the answer is the only thing has_grant does; discarding it is a '
        'guard that checks nothing: ' + ', '.join(thrown_away))


def test_the_discarded_answer_guard_actually_detects_the_shape(tmp_path):
    """A guard that cannot fail is the defect it was written to catch.

    This suite has shipped a check whose subject never ran; this one proves
    its own detector on a synthetic file before trusting the real scan.
    """
    tree = ast.parse(
        'def handler():\n'
        '    has_grant(scope=1, tenant=2)\n'          # thrown away
        '    grant = has_grant(scope=1, tenant=2)\n'  # kept
        '    if has_grant(scope=1, tenant=2):\n'      # kept
        '        pass\n'
        '    return grant\n')
    discarded = {node.value.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call)}
    calls = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and getattr(node.func, 'id', None) == 'has_grant']
    assert sorted(calls) == [2, 3, 4]
    assert [lineno in discarded for lineno in sorted(calls)] == [
        True, False, False]


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


def test_a_faked_writer_that_wrote_nothing_does_not_trip_the_assertion(
        app, tenant_id, monkeypatch):
    """#321: the marker follows the ROW, not the call to audit().

    Faking r6.audit.add_audit_event is how the migration's own tests are
    written. Under the old design audit() set the marker unconditionally and
    nothing cleared it, so a writer that wrote nothing failed the request with
    no row pending — a control firing when its property is NOT violated.

    MUTATION: set _AUDIT_PENDING in audit() again -> red.
    """
    monkeypatch.setattr('r6.audit.add_audit_event',
                        lambda event_type, **kwargs: None)
    _audit_routes(app, tenant_id)
    install_audit_assertions(app)
    assert app.test_client().post('/kernel/flush-only').status_code == 201


def test_a_faked_writer_after_a_real_query_does_not_trip_the_assertion(
        app, tenant_id, monkeypatch):
    """The regression for the rejected in_transaction() gate.

    Every production handler reads the database before it audits, so a gate on
    transaction state fires on a faked writer in every one of them. The row is
    the discriminator; an open transaction is not.
    """
    from r6.models import AuditEventRecord

    monkeypatch.setattr('r6.audit.add_audit_event',
                        lambda event_type, **kwargs: None)

    @app.route('/kernel/read-then-audit', methods=['POST'])
    def read_then_audit():
        AuditEventRecord.query.filter_by(tenant_id=tenant_id).count()
        audit(tenant=tenant_id, event_type='create', detail='faked writer')
        return '', 201

    install_audit_assertions(app)
    assert app.test_client().post('/kernel/read-then-audit').status_code == 201


def test_a_row_flushed_without_going_through_audit_still_fails_the_request(
        app, tenant_id):
    """The marker is set by the flush, so it covers the shipped writer too.

    MUTATION: gate _set_pending_marker on audit() having run -> red.
    """
    from r6.audit import add_audit_event

    @app.route('/kernel/raw-writer', methods=['POST'])
    def raw_writer():
        add_audit_event('create', tenant_id=tenant_id, detail='raw')
        return '', 201

    install_audit_assertions(app)
    with pytest.raises(AuditAssertionError):
        app.test_client().post('/kernel/raw-writer')
    db.session.rollback()


def test_a_pending_row_that_is_not_an_audit_row_is_none_of_its_business(
        app, tenant_id):
    """The marker follows an AuditEventRecord, not any flush.

    Without this the guard quietly widens into "no request leaves ANY pending
    write", which is a property it was never asked to assert and which fires
    on every unmigrated write handler. That widening is how a control stops
    meaning what its name says (docs/2026-08-02-retro.md).

    MUTATION: set the marker for every flush in _set_pending_marker -> red.
    """
    from r6.models import R6Resource

    @app.route('/kernel/flush-a-plain-row', methods=['POST'])
    def flush_a_plain_row():
        db.session.add(R6Resource(
            'Observation', '{"resourceType": "Observation"}',
            resource_id='kernel-1', tenant_id=tenant_id))
        db.session.flush()
        return '', 201

    install_audit_assertions(app)
    assert app.test_client().post('/kernel/flush-a-plain-row').status_code == 201
    db.session.rollback()


def test_rolling_back_a_savepoint_does_not_satisfy_the_assertion(
        app, tenant_id):
    """A SAVEPOINT rollback leaves the audit row pending in the OUTER
    transaction, so it must not clear the marker.

    This is not hypothetical: record_audit_event's failure path
    (r6/audit.py:105) rolls back exactly this savepoint, and that is the
    function slices 12 and 13 migrate away from. A guard with a masking path
    through the code being migrated is not a guard.

    MUTATION: drop the `previous_transaction.nested` check in
    _clear_on_soft_rollback -> red.
    """
    @app.route('/kernel/flush-then-savepoint-rollback', methods=['POST'])
    def flush_then_savepoint_rollback():
        audit(tenant=tenant_id, event_type='create', detail='pending')
        db.session.begin_nested().rollback()
        return '', 201

    install_audit_assertions(app)
    with pytest.raises(AuditAssertionError):
        app.test_client().post('/kernel/flush-then-savepoint-rollback')
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

#: Slice 2 registers the kernel's error handlers from main.py, so main.py is
#: the one production module allowed to import r6.access. Nothing else may:
#: a route module importing the kernel means a guard has been migrated, and
#: that is a later slice with its own PR and its own pin.
#:
#: Adoption is a reviewable list. Each entry names the slice that added it:
#:   main.py            slice 2 — register_error_handlers/install_audit_assertions
#:   r6/smbp/routes.py  slice 3 — reading()'s step-up gate -> require_grant
#: r6/smbp/trend_routes.py is the BP trend chart, registered onto
#: smbp_blueprint from r6/smbp/routes.py — already on this list. It is
#: a separate module so the page does not land in r6/routes.py, and it
#: reads its tenant through tenant_from_request(HEADER, QUERY) because
#: a browser opening a chart cannot set a header. Deliberate adoption,
#: which is what this pin asks for.
_ADOPTION_ALLOWED = {'main.py', 'r6/smbp/routes.py', 'r6/shc/routes.py',
                     'r6/smbp/trend_routes.py',
                     'r6/fasten/routes.py', 'r6/wearables/routes.py',
                     # slice 10: _tenant_or_none resolves through the kernel.
                     # r6/actions/review.py imports that helper rather than
                     # the kernel, so it is not on this list — the blueprint
                     # has one tenant reader, which is the property.
                     'r6/actions/routes.py',
                     # slice 5: three of the four step-up gates in this
                     # blueprint. review.py imports the kernel directly for
                     # _require_step_up rather than going through routes.py,
                     # so it is named here in its own right.
                     'r6/actions/review.py',
                     # slice 10a: the four FHIR resource handlers (create,
                     # read, update, search) read the tenant through the
                     # kernel. This is the god module's FIRST kernel import
                     # and the remaining 20 raw reads in it are pinned by
                     # tests/test_ratchets.py::_RAW_TENANT_READS.
                     'r6/routes.py',
                     # #508, and NOT a migration slice. This blueprint imports
                     # `public_step_up_reason` and nothing else: its two
                     # step-up checks still call the validator directly and
                     # are still counted by _STEP_UP_CALLSITES. What moved is
                     # only which sentence a refusal is allowed to say, which
                     # is a ruling rather than a gate. Migrating these two is
                     # slice 17, and it stays blocked on their JSON wire
                     # shape.
                     'r6/command_center/routes.py'}


def test_no_request_handler_has_adopted_the_kernel():
    """MUTATION: import r6.access from r6/routes.py -> red.

    Slice 1's risk argument was 'imported by nothing'. Slice 2 registers the
    error handlers from main.py, so the property narrows rather than
    disappears: the kernel is REGISTERED but no request handler USES it. An
    errorhandler is inert until something raises, and nothing raises yet.

    When a real migration slice lands, it adds its module here deliberately,
    in the same PR that migrates it — which is the point. Adoption is a
    reviewable list, not a thing that happens quietly.
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
    unexpected = [i for i in importers
                  if i.split(':')[0] not in _ADOPTION_ALLOWED]
    assert not unexpected, (
        'only ' + ', '.join(sorted(_ADOPTION_ALLOWED)) + ' may import the '
        'kernel until a migration slice adopts it deliberately, but it is '
        'also imported by: ' + ', '.join(unexpected))
    assert any(i.split(':')[0] == 'main.py' for i in importers), (
        'main.py no longer imports the kernel — slice 2 registers the error '
        'handlers there, so losing that import silently un-registers them')


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

# --- Slice 2: the handlers are registered, and still inert ----------------
#
# Additive infrastructure, not a migration, so adding tests here is correct —
# the protocol's "a migration PR's diff to tests/ is empty" rule governs
# slices that MOVE behavior, not slices that add plumbing.
#
# The behavioural tests below use a bare Flask app plus register_error_handlers
# rather than the real factory, on purpose: they test the handler contract, and
# building the whole app to observe it would drag db/session state into a test
# about exception rendering.


def _handler_app():
    """A bare app with only the kernel's handlers on it."""
    app = Flask(__name__)
    # The renderers must run; PROPAGATE_EXCEPTIONS would re-raise past them and
    # hide exactly the 500-vs-401 distinction these tests exist to check.
    app.config['PROPAGATE_EXCEPTIONS'] = False
    register_error_handlers(app)
    return app


def test_an_unchecked_step_up_denial_becomes_a_500_not_a_401():
    """The property that makes having the errorhandler safe at all.

    A StepUpDenied raised anywhere other than require_grant carries no checked
    flag. If the handler rendered it as 401, a bug in a service or model layer
    would reach the client looking exactly like a guard that worked, and a
    monitor would read it as ordinary refused traffic. It must stay a 500.

    MUTATION: render the unchecked case as exc.http_status instead of
    re-raising -> red.
    """
    app = _handler_app()

    @app.route('/unchecked')
    def _unchecked():
        raise StepUpDenied('synthetic', http_status=401)

    resp = app.test_client().get('/unchecked')
    assert resp.status_code == 500, (
        f'an unchecked StepUpDenied rendered as {resp.status_code}; it must '
        'surface as a server error, not as a working-looking guard')


def test_a_real_denial_from_require_grant_renders_its_operation_outcome(app):
    """The other half, exercised through the REAL path.

    This deliberately does not hand-construct a checked denial: the flag is
    meant to exist in exactly one place, and a test that copies the literal
    would both break that pin and prove less. require_grant raising for a
    missing token is the actual production shape.

    MUTATION: stop setting the checked flag in require_grant -> renders 500.
    """
    register_error_handlers(app)

    @app.route('/needs-step-up')
    def _needs():
        require_grant(scope=Scope.WRITE,
                      tenant=Tenant(id='t1', source=TenantSource.HEADER))
        return 'unreachable'

    resp = app.test_client().get('/needs-step-up')
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['resourceType'] == 'OperationOutcome'
    assert body['issue'][0]['severity'] == 'error'


def test_a_rejected_tenant_renders_its_operation_outcome():
    """Matches the OperationOutcome r6/routes.py already returns at 400."""
    app = _handler_app()

    @app.route('/no-tenant')
    def _no_tenant():
        raise TenantRejected('absent')

    resp = app.test_client().get('/no-tenant')
    assert resp.status_code == 400
    assert resp.get_json()['resourceType'] == 'OperationOutcome'


def test_registering_the_handlers_adds_no_request_hooks():
    """Slice 2 is HANDLERS ONLY.

    A before_request hook registered app-wide would also run for
    r6/sdc/delivery.py, which sits off r6_blueprint on purpose because the
    HMAC signature in the URL is the credential and the route must work with
    no headers. A tenant hook there breaks every signed delivery link.

    MUTATION: add a before_request hook inside register_error_handlers -> red.
    """
    app = Flask(__name__)
    before = (len(app.before_request_funcs), len(app.after_request_funcs))
    register_error_handlers(app)
    after = (len(app.before_request_funcs), len(app.after_request_funcs))
    assert before == after, (
        f'register_error_handlers changed the request pipeline: {before} -> '
        f'{after}')


def test_the_app_factory_registers_both_kernel_handlers():
    """Slice 2's actual edit: main.py wires the kernel in.

    MUTATION: delete the register_error_handlers call in main.py -> red.
    """
    import main
    flask_app = main.create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'LEGACY_BOOT_ON_CREATE': False,
    })
    registered = flask_app.error_handler_spec[None][None]
    assert StepUpDenied in registered, 'StepUpDenied handler not registered'
    assert TenantRejected in registered, 'TenantRejected handler not registered'


def test_the_audit_assertion_is_installed_and_the_read_one_is_not():
    """#321 is fixed, so install_audit_assertions ships BEFORE the first
    audit() adoption — a guard that arrives with the migration it guards is a
    guard nobody was protected by.

    install_read_audit_assertion still ships nowhere. It goes red on the five
    unaudited-404 paths (S-9) on arrival, and that redness is its own slice
    (spec §2.7, 12x).

    MUTATION: delete the install_audit_assertions call in main.py -> red.
    """
    import main
    flask_app = main.create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'LEGACY_BOOT_ON_CREATE': False,
    })
    teardowns = [f.__name__ for fns in flask_app.teardown_request_funcs.values()
                 for f in fns]
    assert any('audit_committed' in n for n in teardowns), (
        f'install_audit_assertions is not registered: {teardowns}')
    after = [f.__name__ for fns in flask_app.after_request_funcs.values()
             for f in fns]
    assert not any('read_audited' in n for n in teardowns + after), (
        f'install_read_audit_assertion is registered too early: {after}')
