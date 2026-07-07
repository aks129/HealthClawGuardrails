"""
Defense-in-depth security hardening tests.

Covers:
- security response headers on every response
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
def test_rate_limit_key_uses_tenant_when_present(app):
    from r6.rate_limit import rate_limit_key
    with app.test_request_context('/r6/actions/x', headers={'X-Tenant-Id': 'acme'}):
        assert rate_limit_key() == 'acme'


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
