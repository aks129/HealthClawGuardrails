"""The discovery paths the FHIR blueprint serves without a tenant.

Moved out of r6/routes.py unchanged (#591), so the rule that keeps an exempt
path from ever reaching a resource handler can live beside the list it
protects without growing the god module.
"""

from flask import jsonify, request

# The blueprint's url_prefix. Exemptions are matched against the full request
# path, so they must be anchored to this prefix — NOT matched by suffix.
# FHIR resource ids match [A-Za-z0-9.\-]{1,64}, so a suffix test like
# path.endswith('/metadata') also matches GET /r6/fhir/Patient/metadata (a
# resource read of id "metadata"), which would silently exempt a real read
# from tenant/read-auth enforcement. Anchor every exemption instead.
_R6_PREFIX = '/r6/fhir'

# Discovery/public endpoints exempt from tenant + read-auth enforcement.
# EXACT full paths — never suffix-matched.
_EXEMPT_EXACT_PATHS = frozenset({
    f'{_R6_PREFIX}/metadata',       # CapabilityStatement
    f'{_R6_PREFIX}/health',         # health check
    f'{_R6_PREFIX}/$conformance',   # guardrail self-test (self-tenanted internally)
    f'{_R6_PREFIX}/docs/privacy-policy',  # published in discovery + every _disclaimer; its reader has no tenant (#574)
})

# Genuinely-namespaced sub-trees exempt from tenant + read-auth enforcement.
# These are prefix-matched because every path under them is non-clinical and
# no FHIR resource read can introduce one of these segments: a resource read is
# /{prefix}/{ResourceType}/{id}, where ResourceType is a single bare segment —
# it can never be "internal", ".well-known", "oauth", "mcp-apps", or "demo"
# *followed by another '/segment'*. (e.g. /r6/fhir/demo/agent-loop is the demo
# endpoint; /r6/fhir/Demo is a — nonexistent — resource type but still a single
# segment, so it would NOT match these prefixes.)
_EXEMPT_PATH_PREFIXES = (
    f'{_R6_PREFIX}/internal/',
    f'{_R6_PREFIX}/.well-known/',
    f'{_R6_PREFIX}/oauth/',
    f'{_R6_PREFIX}/mcp-apps/',
    f'{_R6_PREFIX}/demo/',
)


def _is_exempt_discovery_path(path):
    """True if `path` is a public discovery/namespaced endpoint.

    Exact-match the literal discovery routes (/metadata, /health) and
    prefix-match the namespaced sub-trees. Crucially this does NOT use a
    suffix test, so a FHIR read like /r6/fhir/Patient/metadata (resource id
    "metadata") is NOT treated as discovery and stays fully gated.
    """
    if path in _EXEMPT_EXACT_PATHS:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PATH_PREFIXES)


def refuse_resource_rule_on_exempt_path():
    """Answer 405 when an exempt discovery path was routed to a resource rule.

    /r6/fhir/docs/privacy-policy and every two-segment path under the exempt
    sub-trees have the shape of /<resource_type>/<resource_id>. Werkzeug
    sends a method the explicit route does not declare (PUT on the policy
    document, GET on an internal POST-only endpoint) to the resource
    handler, where the resource-type allowlist and the write gate happened
    to stop it: two guards nobody reasoned about for discovery, either of
    which could be relaxed without anyone revisiting the exemption (#591).
    An exempt path is a document or an endpoint of its own; it is never a
    resource, so a request that landed on a resource rule is refused here,
    before tenant enforcement and before any handler runs.

    Returns None when the request reached an explicit route.
    """
    # The generic resource rules are the ones that START with the resource
    # type right after the blueprint prefix. An explicit route elsewhere may
    # use a converter of the same name (the MCP Apps pages take a resource
    # type and id under their own prefix); those are explicit and stand.
    rule = request.url_rule.rule if request.url_rule is not None else ''
    if not rule.startswith(f'{_R6_PREFIX}/<resource_type>'):
        return None
    # A plain OperationOutcome, built here rather than through the access
    # kernel: this module is not a kernel adopter, and the kernel's importer
    # list is a ratchet a discovery helper has no business moving.
    return jsonify({
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': 'error', 'code': 'not-supported',
                   'diagnostics': 'This discovery path does not accept %s'
                                  % request.method}],
    }), 405

