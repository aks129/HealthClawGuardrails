"""The resource id a FHIR resource rule takes from the URL path.

`/<resource_type>/<resource_id>` accepted anything the path could carry. A
402-character sentence was answered 404 with the sentence echoed back in the
diagnostics, and in proxy mode the not-found audit row copied it verbatim
into `AuditEventRecord.resource_id`, a `String(255)` column that Postgres
refuses past its width (#279, #281). The kernel pattern for ids that come
from a body already existed; the path had none.

The width is 255, not FHIR's 64, on purpose: the live Fasten connector
exports ids longer than 64, which is why `R6Resource.id` and the ingester's
`_RESOURCE_ID_PATTERN` were widened rather than truncated. A read of an
ingested row must keep working; the charset is what keeps free text out.
"""

import re

from flask import jsonify, request

from r6.discovery_paths import _R6_PREFIX

# Same charset and width as r6/fasten/ingester.py::_RESOURCE_ID_PATTERN, so
# every id that can be stored can be read back, and nothing else can be
# asked for.
_PATH_RESOURCE_ID_PATTERN = re.compile(r'^[A-Za-z0-9\-.]{1,255}$')


def refuse_malformed_resource_id():
    """Answer 400 when a resource rule's path id is not a FHIR id.

    Runs in the blueprint's before_request, after tenant enforcement, so an
    untenanted request still gets the tenant error first. The diagnostics
    never echo the id: the whole point is that its content is untrusted.

    Returns None when the rule takes no resource id, or the id is well-formed.
    """
    rule = request.url_rule.rule if request.url_rule is not None else ''
    if not rule.startswith(f'{_R6_PREFIX}/<resource_type>/<resource_id>'):
        return None
    resource_id = (request.view_args or {}).get('resource_id')
    if resource_id is None or _PATH_RESOURCE_ID_PATTERN.fullmatch(resource_id):
        return None
    return jsonify({
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': 'error', 'code': 'invalid',
                   'diagnostics': 'Resource id must match [A-Za-z0-9.-]{1,255}'}],
    }), 400
