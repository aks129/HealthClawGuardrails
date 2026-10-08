"""QA on #913: the write freeze answers only a caller who is already authorized.

`require_open_tenant` says it must run AFTER the route's own authorization, so
an unauthenticated caller cannot learn which tenants are closed (409 against
401/403). Nothing pinned that order: moving the check above `require_grant`
in create_resource survived the suite (QA mutation M45). The CTO round's\nwriters (SMBP reading, review submit, action commit and confirm, seed,\n$curatr-apply-fix, the demo agent loop) are pinned the same way. Each row sends no
credential to a closed tenant and expects the route's own refusal, never 409.

Synthetic tenant only.
"""

import pytest

from tests.test_fasten_revoke_on_disconnect import (  # noqa: F401  (fixture)
    TENANT, _tombstone, secret)

_OBS = {"resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]}}
_BUNDLE = {"resourceType": "Bundle", "type": "collection",
           "entry": [{"resource": {**_OBS, "id": "qa913-o"}}]}


@pytest.mark.parametrize("method,path,body,expected", [
    ("post", "/r6/fhir/Observation", _OBS, (401,)),
    ("put", "/r6/fhir/Observation/qa913-o", {**_OBS, "id": "qa913-o"}, (401,)),
    ("post", "/r6/fhir/Bundle/$ingest-context", _BUNDLE, (401, 403)),
    ("post", "/r6/fhir/internal/ingest-bundle", {"bundle": _BUNDLE}, (403,)),
    ("post", "/shc/ingest", _BUNDLE, (401,)),
    ("post", "/r6/smbp/reading", {"systolic": 128, "diastolic": 82,
                                  "patient_ref": "Patient/qa913-pt"}, (401,)),
    ("post", "/r6/actions/no-such-action/review", {}, (401, 403)),
    ("post", "/r6/actions/no-such-action/commit", {}, (401,)),
    ("post", "/r6/actions/no-such-action/confirm", {}, (401,)),
    ("post", "/r6/fhir/internal/seed", {"tenant_id": TENANT}, (403,)),
    ("post", "/r6/fhir/Condition/c-1/$curatr-apply-fix", {"fixes": []}, (401, 403)),
    ("post", "/r6/fhir/demo/agent-loop", {}, (403,)),
])
def test_closed_tenant_is_not_revealed_without_a_credential(
        client, secret, monkeypatch, method, path, body, expected):  # noqa: F811
    """MUTATION: call require_open_tenant before the route's auth -> 409."""
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("SHC_WEBHOOK_SECRET", "qa913-shc-secret")
    _tombstone()
    r = getattr(client, method)(path, json=body,
                                headers={"X-Tenant-Id": TENANT,
                                         "X-Human-Confirmed": "true",
                                         "Content-Type": "application/fhir+json"})
    assert r.status_code in expected, (r.status_code, r.get_data(as_text=True))
