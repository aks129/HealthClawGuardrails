"""An exempt discovery path is never a candidate for the resource routes (#591).

/r6/fhir/docs/privacy-policy has the shape of /<resource_type>/<resource_id>,
and /r6/fhir/oauth/token, /r6/fhir/internal/bind-telegram and the rest of
the exempt sub-trees have it too. Werkzeug routes a method the explicit
route does not declare (PUT on the policy document) to the resource
handler, where two guards nobody reasoned about for discovery (the
resource-type allowlist, the write gate) happen to stop it. The tenant
hook now refuses, with 405, any request whose exempt path was routed to a
resource-typed rule, so the class is gone rather than the instance.

The pins are derived from the app's own url_map, so a new exempt path or
a new resource rule is covered on arrival: for every exempt path and every
method, if Werkzeug would route the pair to a rule carrying
<resource_type>, the answer must be 405 and no tenant is needed to get
it; if it routes to an explicit route, that route's own behaviour stands.

MUTATION: r6/discovery_paths.py, make refuse_resource_rule_on_exempt_path
return None -> red on every routed pair (the resource handler answers
instead: 400, 403 or 415).
"""

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from r6.discovery_paths import _EXEMPT_EXACT_PATHS, _EXEMPT_PATH_PREFIXES

METHODS = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH')
# One concrete path per exempt sub-tree, each two segments deep: the shape
# that matches /<resource_type>/<resource_id>.
SAMPLE_UNDER_PREFIX = {
    '/r6/fhir/internal/': '/r6/fhir/internal/bind-telegram',
    '/r6/fhir/.well-known/': '/r6/fhir/.well-known/smart-configuration',
    '/r6/fhir/oauth/': '/r6/fhir/oauth/token',
    '/r6/fhir/mcp-apps/': '/r6/fhir/mcp-apps/care-gaps',
    '/r6/fhir/demo/': '/r6/fhir/demo/agent-loop',
}


def _paths():
    paths = sorted(_EXEMPT_EXACT_PATHS)
    for prefix in _EXEMPT_PATH_PREFIXES:
        assert prefix in SAMPLE_UNDER_PREFIX, f'add a sample path for {prefix}'
        paths.append(SAMPLE_UNDER_PREFIX[prefix])
    return paths


def _routed_rule(app, path, method):
    adapter = app.url_map.bind('localhost')
    try:
        endpoint, _args = adapter.match(path, method=method)
    except (MethodNotAllowed, NotFound):
        return None
    for rule in app.url_map.iter_rules(endpoint):
        if method in (rule.methods or ()) and rule.rule.startswith('/r6/fhir/'):
            return rule.rule
    return None


@pytest.mark.parametrize('method', METHODS)
def test_every_exempt_path_routed_to_a_resource_rule_answers_405(app, method):
    client = app.test_client()
    checked, skipped = [], []
    for path in _paths():
        rule = _routed_rule(app, path, method)
        if rule is None or not rule.startswith('/r6/fhir/<resource_type>'):
            skipped.append((path, rule))
            continue
        resp = client.open(path, method=method, json={'resourceType': 'Patient'})
        checked.append((path, resp.status_code))
        assert resp.status_code == 405, (path, method, rule, resp.status_code, resp.get_data(as_text=True)[:200])
        body = resp.get_json()
        assert body['resourceType'] == 'OperationOutcome'
        assert body['issue'][0]['code'] == 'not-supported'
        # No tenant header was sent: the refusal comes before tenant enforcement.
        assert 'X-Tenant-Id' not in resp.get_data(as_text=True)
    # The class exists: at least the policy document and one sub-tree path
    # route to a resource rule for some method.
    # No route accepts DELETE at all today, so only PUT proves the scan is
    # alive: the policy document and the two-segment sub-tree paths all
    # route to the resource PUT rule.
    if method == 'PUT':
        assert len(checked) >= 2, f'the scan found {checked}; it used to find the policy document and every sub-tree sample'


def test_the_explicit_discovery_routes_are_untouched(app):
    client = app.test_client()
    assert client.get('/r6/fhir/metadata').status_code == 200
    assert client.get('/r6/fhir/health').status_code == 200
    assert client.get('/r6/fhir/docs/privacy-policy').status_code == 200
    # An explicit non-GET route under an exempt sub-tree keeps its own answer.
    resp = client.post('/r6/fhir/oauth/token', data={})
    assert resp.status_code != 405
