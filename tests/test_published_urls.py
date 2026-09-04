"""What we publish to a partner, and whether they can follow it.

Two production defects with one shape: a URL we hand out that the reader
cannot use.

#567 — every URL in the SMART configuration, the OAuth discovery document,
the CapabilityStatement and every search Bundle was built from
``request.host_url``, which is ``http`` behind a TLS-terminating proxy. The
fix is a single ProxyFix at app creation (``main.create_app``), so these
tests drive the whole WSGI stack instead of calling a helper — the middleware
is the thing under test.

#574 — ``/r6/fhir/docs/privacy-policy`` sat behind the blueprint's tenant
hook, and all three documents that point at it are read by someone who has
no tenant yet.

The last two tests are pins rather than examples: they walk every URL the
published documents contain. A fourth broken pointer cannot join the set
without turning one of them red.
"""

import json

from urllib.parse import urljoin, urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

# What the platform's edge adds when it terminates TLS and forwards over
# plain HTTP. Production sends this on every request; local development
# sends nothing.
FORWARDED_HTTPS = {'X-Forwarded-Proto': 'https'}

PRIVACY_POLICY = '/r6/fhir/docs/privacy-policy'

DISCOVERY_PATHS = (
    '/r6/fhir/.well-known/smart-configuration',
    '/r6/fhir/.well-known/oauth-authorization-server',
    '/r6/fhir/metadata',
)

#: Hosts that appear in these documents as canonical FHIR *identifiers*, not
#: as endpoints we serve. ``http://hl7.org/fhir/...`` is a name — rewriting
#: it to https would be wrong, and following it is not the point of it. Every
#: other absolute URL we publish has to be https behind the proxy. A new
#: external host fails the pin until someone decides which kind it is.
CANONICAL_IDENTIFIER_HOSTS = frozenset({
    'hl7.org',
    'terminology.hl7.org',
    'fhir-registry.smarthealthit.org',
})

#: Same-host URLs that are identifiers too, so "can a reader follow this?"
#: does not apply to them:
#:   /r6/fhir                       CapabilityStatement implementation.url —
#:                                  the base of the API, not a resource.
#:   /r6/fhir/Bundle/$ingest-context an OperationDefinition identifier for a
#:                                  tenant-scoped write; a partner reads it,
#:                                  they do not GET it.
#: Anything else we publish must resolve without a tenant header. Adding to
#: this set is a decision, which is what #574 was missing.
NOT_FOLLOWABLE_PATHS = frozenset({
    '/r6/fhir',
    '/r6/fhir/Bundle/$ingest-context',
})


def _walk_urls(node, seen=None):
    """Every URL-shaped string in a JSON document, absolute or rooted."""
    if seen is None:
        seen = []
    if isinstance(node, dict):
        for value in node.values():
            _walk_urls(value, seen)
    elif isinstance(node, list):
        for value in node:
            _walk_urls(value, seen)
    elif isinstance(node, str):
        if node.startswith(('http://', 'https://', '/')):
            seen.append(node)
    return seen


def _clinical_disclaimer(client, auth_headers, sample_observation):
    """The ``_disclaimer`` block we attach to every clinical resource."""
    created = client.post(
        '/r6/fhir/Observation',
        data=json.dumps(sample_observation),
        content_type='application/json',
        headers={**auth_headers, 'X-Human-Confirmed': 'true'},
    )
    assert created.status_code in (200, 201), created.data
    read = client.get(f"/r6/fhir/Observation/{created.get_json()['id']}",
                      headers={**auth_headers, **FORWARDED_HTTPS})
    assert read.status_code == 200
    disclaimer = read.get_json()['_disclaimer']
    assert disclaimer['url']
    return disclaimer


def _published_documents(client, auth_headers, sample_observation):
    """The documents a partner reads, fetched behind the proxy."""
    documents = {}
    for path in DISCOVERY_PATHS:
        response = client.get(path, headers=FORWARDED_HTTPS)
        assert response.status_code == 200, path
        documents[path] = response.get_json()
    documents['_disclaimer'] = _clinical_disclaimer(
        client, auth_headers, sample_observation)
    return documents


# --- #567: the protocol we advertise ---------------------------------------


def test_smart_configuration_is_https_behind_the_proxy(client):
    """The first document a partner reads. Production served http:// here."""
    config = client.get('/r6/fhir/.well-known/smart-configuration',
                        headers=FORWARDED_HTTPS).get_json()
    for field in ('authorization_endpoint', 'token_endpoint',
                  'registration_endpoint', 'revocation_endpoint'):
        assert config[field].startswith('https://'), field


def test_capability_statement_is_https_behind_the_proxy(client):
    statement = client.get('/r6/fhir/metadata',
                           headers=FORWARDED_HTTPS).get_json()
    assert statement['implementation']['url'].startswith('https://')
    oauth_uris = statement['rest'][0]['security']['extension'][0]['extension']
    for entry in oauth_uris:
        assert entry['valueUri'].startswith('https://'), entry['url']


def test_search_bundle_links_are_https_behind_the_proxy(
        client, auth_headers, sample_observation):
    """fullUrl and the self link identify a resource that only exists
    over https. Fixing the discovery document alone leaves these wrong."""
    client.post('/r6/fhir/Observation', data=json.dumps(sample_observation),
                content_type='application/json',
                headers={**auth_headers, 'X-Human-Confirmed': 'true'})

    bundle = client.get('/r6/fhir/Observation?status=final',
                        headers={**auth_headers, **FORWARDED_HTTPS}).get_json()

    assert bundle['entry'], 'need at least one entry to check fullUrl'
    for entry in bundle['entry']:
        if 'fullUrl' in entry:
            assert entry['fullUrl'].startswith('https://'), entry['fullUrl']
    self_link = bundle['link'][0]['url']
    assert self_link.startswith('https://'), self_link


def test_audit_bundle_links_are_https_behind_the_proxy(client, auth_headers):
    bundle = client.get('/r6/fhir/AuditEvent',
                        headers={**auth_headers, **FORWARDED_HTTPS}).get_json()
    assert bundle['link'][0]['url'].startswith('https://')


def test_local_development_without_the_header_stays_http(
        client, auth_headers, sample_observation):
    """No proxy, no rewrite. `flask run` on a laptop still publishes the
    URLs that actually work there."""
    config = client.get(
        '/r6/fhir/.well-known/smart-configuration').get_json()
    assert config['authorization_endpoint'] == (
        'http://localhost/r6/fhir/oauth/authorize')

    statement = client.get('/r6/fhir/metadata').get_json()
    assert statement['implementation']['url'] == 'http://localhost/r6/fhir'

    client.post('/r6/fhir/Observation', data=json.dumps(sample_observation),
                content_type='application/json',
                headers={**auth_headers, 'X-Human-Confirmed': 'true'})
    bundle = client.get('/r6/fhir/Observation', headers=auth_headers).get_json()
    assert bundle['link'][0]['url'].startswith('http://localhost/')


# --- #574: the policy link its own reader could not open -------------------


def test_privacy_policy_answers_without_a_tenant_header(client):
    """Production answered 400 here. The reader of this page — a partner
    evaluating us — has no tenant by construction."""
    response = client.get(PRIVACY_POLICY)
    assert response.status_code == 200, response.data
    body = response.get_json()
    assert 'medical_disclaimer' in body
    assert 'data_protection' in body


def test_privacy_policy_reads_no_tenant_data_and_writes_no_audit(
        client, tenant_headers, other_tenant_headers):
    """Exempting a route from the tenant gate is only safe if the route has
    nothing tenant-scoped in it. Same bytes for everyone, and no AuditEvent
    — the policy is a document, not a resource access."""
    from r6.models import AuditEventRecord

    before = AuditEventRecord.query.count()
    anonymous = client.get(PRIVACY_POLICY)
    one_tenant = client.get(PRIVACY_POLICY, headers=tenant_headers)
    another = client.get(PRIVACY_POLICY, headers=other_tenant_headers)

    assert anonymous.data == one_tenant.data == another.data
    assert AuditEventRecord.query.count() == before


# --- The pins --------------------------------------------------------------


def test_every_published_url_carries_the_proxy_protocol(
        client, auth_headers, sample_observation):
    """MUTATION: publish one more URL from request.host_url without the
    proxy fix -> red.

    Walks the two discovery documents, the CapabilityStatement and the
    _disclaimer we attach to every clinical resource. Canonical FHIR
    identifiers are skipped by host; everything else is a URL we serve and
    must be https behind the proxy.
    """
    documents = _published_documents(client, auth_headers, sample_observation)
    base = 'https://localhost/'

    checked = 0
    for source, document in documents.items():
        for url in _walk_urls(document):
            absolute = urljoin(base, url)
            host = urlsplit(absolute).netloc
            if host in CANONICAL_IDENTIFIER_HOSTS:
                continue
            assert absolute.startswith('https://'), (
                f'{source} publishes {url!r}, which is not https behind the '
                f'proxy. Build it from request.host_url so ProxyFix reaches '
                f'it, or add its host to CANONICAL_IDENTIFIER_HOSTS if it is '
                f'an identifier rather than an endpoint.')
            checked += 1
    assert checked >= 12, (
        f'only walked {checked} URLs — the documents did not render, and a '
        f'pin that inspects nothing passes forever')


def test_every_published_pointer_resolves_without_a_tenant(
        client, app, auth_headers, sample_observation):
    """MUTATION: point a published field at a tenant-gated or nonexistent
    route -> red. This is #574, generalized: the reader of any of these
    documents has no tenant.
    """
    documents = _published_documents(client, auth_headers, sample_observation)
    base = 'https://localhost/'
    adapter = app.url_map.bind('localhost')

    checked = 0
    for source, document in documents.items():
        for url in _walk_urls(document):
            absolute = urljoin(base, url)
            split = urlsplit(absolute)
            if split.netloc in CANONICAL_IDENTIFIER_HOSTS:
                continue
            path = split.path or '/'
            if path in NOT_FOLLOWABLE_PATHS:
                continue

            try:
                adapter.match(path, method='GET')
            except MethodNotAllowed:
                pass  # the route exists, it just is not a GET (token, revoke)
            except NotFound:
                pytest.fail(
                    f'{source} publishes {url!r}, which routes nowhere')

            response = client.get(path)
            assert response.status_code != 404, (
                f'{source} publishes {url!r}, which 404s')
            body = response.get_json(silent=True) or {}
            if body.get('resourceType') == 'OperationOutcome':
                diagnostics = ' '.join(
                    issue.get('diagnostics', '')
                    for issue in body.get('issue', []))
                assert 'X-Tenant-Id' not in diagnostics, (
                    f'{source} publishes {url!r}, which demands a tenant '
                    f'header its reader does not have (#574). Exempt the '
                    f'route in _EXEMPT_EXACT_PATHS, or stop publishing it.')
            checked += 1
    assert checked >= 6, (
        f'only walked {checked} pointers — the documents did not render')
