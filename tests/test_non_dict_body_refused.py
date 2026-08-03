"""A JSON body that is not an object must be refused, not crash the worker.

Sibling of the depth bound shipped for #312, and found while fixing it. Same
outcome — an unauthenticated caller turns a malformed body into a 500 — but a
different mechanism, so the depth guard does not cover it: the payload is not
deep, it is the wrong *type*. ``[1]`` is a few bytes and needs only a tenant
header.

The first crash is not in any handler. ``require_human_confirmation``
(``r6/health_compliance.py``) calls ``.get()`` on the parsed body from inside
``enforce_human_in_loop``, a ``before_request`` hook that runs ahead of EVERY
non-exempt POST/PUT on ``r6_blueprint``. That is the same hook that made the
per-handler guards unreachable in #312, and it is why this file asserts through
the HTTP boundary rather than calling the handlers.

The hook returning False for a non-dict body is not a gate being skipped. It
says "this is not a clinical write I can identify", which is true — a list has
no ``resourceType``. The request is still refused, by the handler that owns
what a valid body looks like. ``propose_action`` already had this shape; these
sites did not.
"""

from __future__ import annotations

import pytest

# (method, path) for every write path reachable with only a tenant header.
WRITE_PATHS = [
    ('post', '/r6/fhir/Patient'),
    ('post', '/r6/fhir/Bundle/$ingest-context'),
    ('post', '/r6/smbp/enroll'),
    ('post', '/r6/actions/propose'),
]

# Bodies that parse as JSON but are not objects. A handler reading .get() on
# any of these raises AttributeError, which Flask turns into a 500.
NON_DICT_BODIES = ['[1]', '"a string"', '42', 'true', '[]', 'null']


@pytest.mark.parametrize('method,path', WRITE_PATHS,
                         ids=[p for _, p in WRITE_PATHS])
@pytest.mark.parametrize('body', NON_DICT_BODIES)
def test_a_non_object_body_is_refused_not_crashed(client, method, path, body):
    """MUTATION: drop the isinstance check in require_human_confirmation
    (r6/health_compliance.py) -> the two /r6/fhir/ rows go red with a 500.

    Any 5xx here is an unauthenticated caller crashing a worker.
    """
    response = getattr(client, method)(
        path, data=body,
        headers={'X-Tenant-Id': 'test-tenant',
                 'Content-Type': 'application/json'})
    assert response.status_code < 500, (
        f'{method.upper()} {path} answered {response.status_code} for a '
        f'non-object body {body!r}; a malformed body must be refused, not '
        'raise')


def test_the_human_in_the_loop_hook_tolerates_a_non_dict_body():
    """The hook runs before every handler, so it must not be the crash site.

    MUTATION: remove the isinstance guard from require_human_confirmation
    -> AttributeError instead of a clean False.
    """
    from r6.health_compliance import require_human_confirmation

    for body in ([1], 'text', 42, None, [], True):
        assert require_human_confirmation(body) is False, (
            f'the hook claimed {body!r} needs human confirmation')


def test_a_clinical_dict_still_requires_human_confirmation():
    """The tolerance above must not become a bypass.

    A real clinical resource still trips the gate — otherwise "not a dict, so
    no confirmation needed" would have widened into "no confirmation needed".

    MUTATION: return False unconditionally -> red.
    """
    from r6.health_compliance import require_human_confirmation

    assert require_human_confirmation(
        {'resourceType': 'Observation', 'status': 'final'}) is True
    assert require_human_confirmation({'resourceType': 'Consent'}) is True
    assert require_human_confirmation({'resourceType': 'Patient'}) is False
