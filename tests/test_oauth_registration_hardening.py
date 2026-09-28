"""Open Dynamic Client Registration stays open, and stays visible.

Hosted connectors register themselves (RFC 7591; spec §13.1), so there is no
initial access token and no allowlist. What registration can do is keep an
attacker-chosen `client_name` short and printable, say in the log that a
client appeared and where its codes go, and stay under the per-client rate
limit. The consent page, not the registration, is where a person decides.
Each test names its mutation.
"""
import json
import logging

from r6 import oauth


def _register(client, **body):
    body.setdefault('redirect_uris', ['https://claude.ai/api/mcp/auth_callback'])
    return client.post('/r6/fhir/oauth/register', data=json.dumps(body),
                       content_type='application/json')


def test_client_name_is_trimmed_capped_and_stripped_of_control_characters(client):
    """MUTATION: store body['client_name'] unchanged -> red."""
    raw = '  Claude\x1b[31m‮\x00  helper\n' + 'x' * 200
    resp = _register(client, client_name=raw)
    assert resp.status_code == 201
    name = resp.get_json()['client_name']
    assert len(name) <= oauth.CLIENT_NAME_MAX == 80
    assert name.startswith('Claude[31m helper x')
    assert all(ch.isprintable() for ch in name)
    stored = oauth._oauth_store_get('client', resp.get_json()['client_id'])
    assert stored['client_name'] == name


def test_a_missing_blank_or_non_string_client_name_is_unknown_client(client):
    for body in ({}, {'client_name': '   \t'}, {'client_name': '\x00\x07'},
                 {'client_name': ['Claude']}, {'client_name': 42}):
        resp = _register(client, **body)
        assert resp.status_code == 201, body
        assert resp.get_json()['client_name'] == 'Unknown Client', body


def test_each_registration_is_logged_without_the_name_or_secret(client, caplog):
    """MUTATION: drop the registration log line -> red."""
    with caplog.at_level(logging.INFO, logger='r6.oauth'):
        resp = _register(client, client_name='Claude',
                         token_endpoint_auth_method='client_secret_post',
                         redirect_uris=['https://claude.ai/api/mcp/auth_callback',
                                        'https://claude.com/api/mcp/auth_callback'])
    body = resp.get_json()
    lines = [r.getMessage() for r in caplog.records
             if 'client registered' in r.getMessage()]
    assert len(lines) == 1, caplog.text
    line = lines[0]
    assert body['client_id'] in line
    assert 'client_secret_post' in line
    assert "'claude.ai'" in line and "'claude.com'" in line
    assert '/api/mcp/auth_callback' not in line  # hosts, not full URIs
    assert body['client_secret'] not in line
    assert 'Claude' not in line.replace('claude.', '')  # the chosen name stays out


def test_logged_hosts_cannot_forge_a_log_line(client, caplog):
    with caplog.at_level(logging.INFO, logger='r6.oauth'):
        _register(client, redirect_uris=['https://evil.example\x1b2K/cb'])
    line = next(r.getMessage() for r in caplog.records
                if 'client registered' in r.getMessage())
    assert '\x1b' not in line


def test_registration_sits_under_the_per_client_rate_limit(client):
    """MUTATION: exempt /oauth/register in rate_limit_middleware -> red.
    Keyed per IP for an unauthenticated caller, so a varying X-Tenant-Id
    buys no fresh budget (#339)."""
    from r6.rate_limit import DEFAULT_RATE_LIMIT
    statuses = []
    for i in range(DEFAULT_RATE_LIMIT + 1):
        resp = client.post('/r6/fhir/oauth/register', data=json.dumps({
            'redirect_uris': ['https://evil.example/cb']}),
            content_type='application/json',
            headers={'X-Tenant-Id': f'tenant-{i}'})
        statuses.append(resp.status_code)
    assert statuses[:DEFAULT_RATE_LIMIT] == [201] * DEFAULT_RATE_LIMIT
    assert statuses[-1] == 429
