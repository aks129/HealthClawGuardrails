"""
Defense-in-depth security hardening tests.

Covers:
- security response headers on every zero-argument GET route the app registers
- dashboards still render (200) with CSP present
- opt-in step-up token replay guard
- global payload cap (413 on oversized body)
- rate-limit keying falls back to client IP when no tenant header is present
"""

import pytest

from r6.stepup import (
    generate_step_up_token,
    validate_step_up_token,
    clear_nonce_cache,
)


# ---------------------------------------------------------------------------
# 1. Security headers
# ---------------------------------------------------------------------------
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
}


def test_security_headers_present_on_normal_response(client):
    resp = client.get('/r6-dashboard')
    assert resp.status_code == 200
    for header, expected in SECURITY_HEADERS.items():
        assert resp.headers.get(header) == expected, f'missing/mismatched {header}'
    assert 'Permissions-Policy' in resp.headers
    csp = resp.headers.get('Content-Security-Policy', '')
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize('path', ['/r6-dashboard', '/fhir-control-panel'])
def test_dashboards_200_with_csp(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert 'Content-Security-Policy' in resp.headers
    assert resp.headers.get('X-Content-Type-Options') == 'nosniff'


# The audit SSE stream is an infinite generator (while True + sleep) and the
# Werkzeug test client buffers the body, so a GET here never returns.
_UNSWEEPABLE_RULES = {'/r6/fhir/AuditEvent/$stream'}


def test_security_headers_on_every_registered_get_route(app, client):
    """MUTATION: early-return `response` from app.py's _security_headers when
    request.path starts with '/r6/fhir' or '/fasten' -> red, naming every
    stripped path. The three page checks above stay green under it: they only
    reach the web blueprint, so /r6/fhir/mcp-apps/care-gaps — a patient-facing
    text/html surface — could lose X-Frame-Options: DENY and its CSP
    frame-ancestors 'none' with the file green (#634 F6).

    Enumerating the url_map rather than a hand-kept path list means a new
    blueprint is covered the moment it is registered.
    """
    rules = {str(r) for r in app.url_map.iter_rules()
             if not r.arguments and 'GET' in (r.methods or set())}
    stale = _UNSWEEPABLE_RULES - rules
    assert not stale, f'exclusion names routes that no longer exist: {stale}'
    # Non-vacuity: an empty or mis-read url_map would sweep nothing and pass.
    assert '/r6/fhir/mcp-apps/care-gaps' in rules

    failures = []
    for path in sorted(rules - _UNSWEEPABLE_RULES):
        resp = client.get(path)
        bad = [f'{h}={resp.headers.get(h)!r}'
               for h, expected in SECURITY_HEADERS.items()
               if resp.headers.get(h) != expected]
        if "frame-ancestors 'none'" not in resp.headers.get(
                'Content-Security-Policy', ''):
            bad.append("CSP frame-ancestors 'none'")
        if bad:
            failures.append(f'{path} -> {", ".join(bad)}')
    assert not failures, failures


def test_command_center_has_csp(client):
    # Command center may redirect/login-gate; whatever it returns must still
    # carry the security headers from the global after_request.
    resp = client.get('/command-center')
    assert resp.status_code in (200, 302, 401, 403)
    assert 'Content-Security-Policy' in resp.headers
    assert resp.headers.get('X-Frame-Options') == 'DENY'


# ---------------------------------------------------------------------------
# 2. Step-up token replay guard (opt-in)
# ---------------------------------------------------------------------------
def test_replay_guard_default_off_allows_reuse(tenant_id):
    clear_nonce_cache()
    token = generate_step_up_token(tenant_id)
    ok1, err1 = validate_step_up_token(token, tenant_id)
    ok2, err2 = validate_step_up_token(token, tenant_id)
    assert (ok1, err1) == (True, None)
    # Default (consume_nonce=False) must still permit reuse — no regression.
    assert (ok2, err2) == (True, None)


def test_replay_guard_consume_rejects_second_use(tenant_id):
    clear_nonce_cache()
    token = generate_step_up_token(tenant_id)
    ok1, err1 = validate_step_up_token(token, tenant_id, consume_nonce=True)
    ok2, err2 = validate_step_up_token(token, tenant_id, consume_nonce=True)
    assert (ok1, err1) == (True, None)
    assert ok2 is False
    assert err2 == 'Token already used (replay)'


def test_replay_guard_consume_then_default_still_blocked(tenant_id):
    # Once consumed, even a default (non-consuming) validation should see the
    # token is still otherwise valid — the guard only triggers under consume.
    clear_nonce_cache()
    token = generate_step_up_token(tenant_id)
    assert validate_step_up_token(token, tenant_id, consume_nonce=True) == (True, None)
    # Non-consuming validation does not check the nonce, so it still passes.
    assert validate_step_up_token(token, tenant_id) == (True, None)
    # But another consuming validation is a replay.
    ok, err = validate_step_up_token(token, tenant_id, consume_nonce=True)
    assert ok is False and 'replay' in err.lower()


# ---------------------------------------------------------------------------
# 3. Global payload cap
# ---------------------------------------------------------------------------
def test_oversized_body_rejected_413(client):
    from main import app
    cap = app.config.get('MAX_CONTENT_LENGTH')
    assert cap is not None
    oversized = b'x' * (cap + 1024)
    resp = client.post(
        '/api/subscribe',
        data=oversized,
        content_type='application/json',
    )
    assert resp.status_code == 413


def test_payload_cap_configured_at_least_5mb(client):
    from main import app
    assert app.config.get('MAX_CONTENT_LENGTH') >= 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# 4. Rate-limit keying: IP fallback when no tenant header
# ---------------------------------------------------------------------------
def test_rate_limit_key_uses_tenant_when_the_claim_is_PROVEN(app):
    """Updated by #339. This test previously asserted that a bare
    X-Tenant-Id header selected the bucket — which was the vulnerability,
    not the feature: the header is caller-chosen, so the limit was opt-out.

    Per-tenant keying survives, but only for a caller that proved the tenant.
    """
    from r6.rate_limit import rate_limit_key
    from r6.stepup import generate_step_up_token

    token = generate_step_up_token('acme')
    with app.test_request_context('/r6/actions/x', headers={
            'X-Tenant-Id': 'acme', 'X-Step-Up-Token': token}):
        assert rate_limit_key() == 'acme'


def test_an_unproven_tenant_header_does_not_select_the_bucket(app):
    """MUTATION: key on the header again -> red. This is #339 itself."""
    from r6.rate_limit import rate_limit_key
    with app.test_request_context(
            '/r6/actions/x', headers={'X-Tenant-Id': 'acme'},
            environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        assert rate_limit_key() == 'ip:203.0.113.9'


def test_rate_limit_key_falls_back_to_ip(app):
    from r6.rate_limit import rate_limit_key
    with app.test_request_context(
        '/r6/actions/callback/twilio',
        environ_base={'REMOTE_ADDR': '203.0.113.7'},
    ):
        key = rate_limit_key()
        assert key == 'ip:203.0.113.7'
        # Crucially, it is NOT the shared anonymous bucket.
        assert key != 'anonymous'


def test_rate_limit_key_honors_forwarded_for(app):
    from r6.rate_limit import rate_limit_key
    # The rightmost X-Forwarded-For hop is the one appended by our trusted
    # edge proxy — the real peer it saw. Leftmost entries are client-spoofable,
    # so keying off the last hop removes that bucket-splitting surface.
    with app.test_request_context(
        '/r6/actions/callback/bland',
        headers={'X-Forwarded-For': '198.51.100.5, 10.0.0.1'},
    ):
        assert rate_limit_key() == 'ip:10.0.0.1'


def test_rate_limit_key_forwarded_for_ignores_spoofed_left_hop(app):
    """A client-injected leftmost XFF entry cannot change the bucket key."""
    from r6.rate_limit import rate_limit_key
    with app.test_request_context(
        '/r6/actions/callback/bland',
        headers={'X-Forwarded-For': 'spoofed-by-client, 203.0.113.9'},
    ):
        assert rate_limit_key() == 'ip:203.0.113.9'


def test_csp_allows_fasten_widget_iframe(client):
    # The /connect/<tenant> page embeds the Fasten Stitch widget. Without an
    # explicit frame-src, default-src 'self' blocks the iframe and the page
    # shows "content blocked" (found live 2026-07-08). Identity verification
    # may navigate the frame to CLEAR/ID.me, so those hosts are allowed too.
    resp = client.get('/connect/csp-check-tenant')
    csp = resp.headers.get('Content-Security-Policy', '')
    assert 'frame-src' in csp
    assert 'https://*.fastenhealth.com' in csp
    assert 'https://*.id.me' in csp
    assert 'https://*.clearme.com' in csp
    # embedding US stays forbidden — frame-src (what we embed) must not
    # loosen frame-ancestors (who embeds us)
    assert "frame-ancestors 'none'" in csp


def test_vercel_serverless_copy_refuses_stateful_writes(client, monkeypatch):
    # api/index.py (ephemeral serverless SQLite) must refuse mutating
    # requests to stateful paths — a write accepted there is silently lost.
    # The hook can't be re-registered mid-suite, so exercise the guard
    # function directly in a request context.
    from api.index import _refuse_serverless_writes
    from main import app as flask_app

    monkeypatch.setenv('VERCEL', '1')
    with flask_app.test_request_context('/r6/fhir/Patient', method='POST'):
        resp = _refuse_serverless_writes()
        assert resp is not None
        body, status = resp
        assert status == 405
        assert 'app.healthclaw.io' in body.get_data(as_text=True)
    with flask_app.test_request_context('/r6/fhir/metadata', method='GET'):
        assert _refuse_serverless_writes() is None
    monkeypatch.delenv('VERCEL')
    with flask_app.test_request_context('/r6/fhir/Patient', method='POST'):
        assert _refuse_serverless_writes() is None  # non-Vercel: no-op


def test_all_model_tables_registered_in_metadata():
    # schema_sync reconciles db.metadata — a model module not imported at
    # boot means its new columns silently never reach long-lived Postgres
    # (live incident 2026-07-08: fasten_connections.webhook_verified_at).
    from models import db  # noqa: F401
    import main  # noqa: F401  (executes the boot-time imports)
    tables = set(db.metadata.tables)
    for expected in ("fasten_connections", "fasten_jobs",
                     "proposed_actions" if "proposed_actions" in tables else "proposed_action",
                     "wearable_connections" if "wearable_connections" in tables else "wearable_connection"):
        assert any(expected.rstrip('s') in t for t in tables), (expected, tables)
    assert "fasten_connections" in tables


# ---------------------------------------------------------------------------
# 4b. #339 — the limiter must not be opt-out via a caller-chosen header
# ---------------------------------------------------------------------------
def _flood(app, headers_for, n=150, ip='198.51.100.4'):
    """Send n requests through the keying+counting path; return throttled count."""
    from r6 import rate_limit

    rate_limit._rate_limits.clear()
    throttled = 0
    for i in range(n):
        with app.test_request_context('/r6/fhir/Patient',
                                      headers=headers_for(i),
                                      environ_base={'REMOTE_ADDR': ip}):
            allowed, _remaining, _reset = rate_limit.check_rate_limit(
                rate_limit.rate_limit_key(), max_requests=30, window_seconds=60)
            if not allowed:
                throttled += 1
    return throttled


def test_varying_the_tenant_header_no_longer_evades_the_limit(app):
    """MUTATION: revert rate_limit_key to the header -> red.

    MEASURED on the live deployment before this fix (#339): 150 requests with
    a constant tenant header -> 30 throttled; the same 150 with a VARYING
    header -> 0 throttled, and 150 buckets opened. The limiter both failed to
    limit and became its own memory-growth vector. Both halves are asserted
    here so neither can regress silently.
    """
    from r6 import rate_limit

    constant = _flood(app, lambda i: {'X-Tenant-Id': 'acme'})
    assert constant > 0, "the limiter did not fire at all — check the fixture"

    varying = _flood(app, lambda i: {'X-Tenant-Id': f'acme-{i}'})
    assert varying > 0, (
        "varying a caller-chosen header still evades the rate limit (#339)")
    assert len(rate_limit._rate_limits) == 1, (
        "each forged tenant id still opened its own bucket — the limiter is "
        "a memory-growth vector as well as ineffective")


def test_a_proven_tenant_still_gets_its_own_bucket(app):
    """The fix must not collapse legitimate multi-tenant traffic from one
    egress IP (CareAgents reads /r6/fhir for every tenant from one host)."""
    from r6 import rate_limit
    from r6.stepup import generate_step_up_token

    rate_limit._rate_limits.clear()
    for tenant in ('t-one', 't-two', 't-three'):
        token = generate_step_up_token(tenant)
        with app.test_request_context('/r6/fhir/Patient', headers={
                'X-Tenant-Id': tenant, 'X-Step-Up-Token': token},
                environ_base={'REMOTE_ADDR': '198.51.100.9'}):
            rate_limit.check_rate_limit(rate_limit.rate_limit_key(),
                                        max_requests=30)
    assert set(rate_limit._rate_limits) == {'t-one', 't-two', 't-three'}


def test_a_malformed_token_does_not_prove_the_tenant(app):
    """PRESENT but not valid, the branch the proven/unproven pair never
    reached. Pinned before kernel slice 20 moves this predicate.

    MUTATION (pre-kernel shape): `return bool(valid)` -> `return True` ->
    red, while the proven-tenant test stays green. Executed 2026-09-06.
    Kernel shape: `has_grant(...) is not None` -> `True` -> the same red.
    Executed 2026-09-06.
    """
    from r6.rate_limit import rate_limit_key
    with app.test_request_context('/r6/actions/x', headers={
            'X-Tenant-Id': 'acme', 'X-Step-Up-Token': 'not-a-real-token'},
            environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        assert rate_limit_key() == 'ip:203.0.113.9'


def test_a_bearer_alias_proves_the_tenant_like_the_header_does(app):
    """Authorization: Bearer <step-up token> is the alias the read gate
    accepts; the limiter must key the same caller the same way."""
    from r6.rate_limit import rate_limit_key
    from r6.stepup import generate_step_up_token
    token = generate_step_up_token('acme')
    with app.test_request_context('/r6/actions/x', headers={
            'X-Tenant-Id': 'acme', 'Authorization': f'Bearer {token}'}):
        assert rate_limit_key() == 'acme'


def test_a_validator_that_raises_never_fails_the_request(app, monkeypatch):
    """The limiter must never fail a request: a validator that cannot reach
    its store answers "unproven", and the request keys by IP. Patched at
    both the module the limiter imported from and the name it bound, so
    the pin holds on either side of the kernel move.

    MUTATION: drop the try/except around the check -> red (the exception
    escapes rate_limit_key). Executed 2026-09-06.
    """
    from r6 import rate_limit, stepup
    from r6.stepup import generate_step_up_token

    def boom(*args, **kwargs):
        raise RuntimeError('nonce store unreachable')

    token = generate_step_up_token('acme')
    monkeypatch.setattr(stepup, 'validate_step_up_token', boom)
    monkeypatch.setattr(rate_limit, 'validate_step_up_token', boom,
                        raising=False)
    with app.test_request_context('/r6/actions/x', headers={
            'X-Tenant-Id': 'acme', 'X-Step-Up-Token': token},
            environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        assert rate_limit.rate_limit_key() == 'ip:203.0.113.9'


def test_a_token_for_a_DIFFERENT_tenant_does_not_prove_this_one(app):
    """MUTATION: validate the token without binding it to the claimed tenant
    -> red. Holding one valid token would otherwise re-open the evasion."""
    from r6.rate_limit import rate_limit_key
    from r6.stepup import generate_step_up_token

    token = generate_step_up_token('tenant-i-own')
    with app.test_request_context('/r6/fhir/Patient', headers={
            'X-Tenant-Id': 'someone-elses-tenant', 'X-Step-Up-Token': token},
            environ_base={'REMOTE_ADDR': '198.51.100.7'}):
        assert rate_limit_key() == 'ip:198.51.100.7'


def test_a_garbage_token_degrades_to_ip_and_never_raises(app):
    """The limiter runs as a before_request hook: a malformed token must
    key by IP, not 500 the request."""
    from r6.rate_limit import rate_limit_key
    for bad in ('', 'not-a-token', 'a.b.c', 'x' * 5000):
        with app.test_request_context('/r6/fhir/Patient', headers={
                'X-Tenant-Id': 'acme', 'X-Step-Up-Token': bad},
                environ_base={'REMOTE_ADDR': '198.51.100.8'}):
            assert rate_limit_key() == 'ip:198.51.100.8'
