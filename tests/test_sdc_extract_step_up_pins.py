"""Characterization pins for $extract's step-up gate (#655).

Written against the direct validate_step_up_token call, before the site
moves to the kernel. The move must keep every answer here byte-identical.

Today the gate says two different sentences. A missing or empty header gets
the one that names dryRun=true; every other refusal, a whitespace-only
header included, gets "Invalid step-up token". The kernel strips the header,
so it files a whitespace-only token as absent: a move that branched on that
would send it the dryRun sentence. The whitespace row is here to catch that.

dryRun skips the gate, so every token variant reaches extraction and gets
the same preview. The Questionnaire travels in the request so the preview is
deterministic.
"""

import pytest

from r6.stepup import generate_step_up_token

_URL = "/r6/fhir/QuestionnaireResponse/$extract?dryRun={}"

_OBS_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                "sdc-questionnaire-observationExtract")

_PARAMS = {
    "resourceType": "Parameters",
    "parameter": [
        {"name": "questionnaire-response",
         "resource": {"resourceType": "QuestionnaireResponse",
                      "status": "completed",
                      "item": [{"linkId": "weight",
                                "answer": [{"valueQuantity":
                                            {"value": 70}}]}]}},
        {"name": "questionnaire",
         "resource": {"resourceType": "Questionnaire", "status": "active",
                      "item": [{"linkId": "weight", "type": "quantity",
                                "code": [{"system": "http://loinc.org",
                                          "code": "29463-7"}],
                                "extension": [{"url": _OBS_EXTRACT,
                                               "valueBoolean": True}]}]}},
    ],
}

_DRY_RUN_SENTENCE = (
    '{"issue":[{"code":"security","diagnostics":"$extract requires '
    'X-Step-Up-Token (use dryRun=true to preview without committing)",'
    '"severity":"error"}],"resourceType":"OperationOutcome"}\n')

_INVALID = (
    '{"issue":[{"code":"security","diagnostics":"Invalid step-up token",'
    '"severity":"error"}],"resourceType":"OperationOutcome"}\n')

_PREVIEW = (
    '{"parameter":[{"name":"return","resource":{"entry":[{"request":'
    '{"method":"POST","url":"Observation"},"resource":{"code":{"coding":'
    '[{"code":"29463-7","system":"http://loinc.org"}]},"resourceType":'
    '"Observation","status":"final","valueQuantity":{"value":70}}}],'
    '"resourceType":"Bundle","type":"transaction"}}],'
    '"resourceType":"Parameters"}\n')

_COMMIT_REFUSED = (
    '{"issue":[{"code":"business-rule","diagnostics":"This bundle carries '
    'rows $extract does not commit on a step-up token alone. The form-fill '
    'rail commits them after human confirmation; $extract does not. Use '
    'dryRun=true to preview them.","severity":"error"}],'
    '"resourceType":"OperationOutcome"}\n')


def _token(kind, tenant_id):
    """The X-Step-Up-Token value for a row, or None for no header at all."""
    return {
        "missing": None,
        "empty": "",
        "whitespace": "   ",
        "invalid": "not-a-real-token",
        "wrong-tenant": generate_step_up_token("some-other-tenant"),
        "read-scoped": generate_step_up_token(tenant_id, scope="read"),
        "valid": generate_step_up_token(tenant_id),
    }[kind]


def _post(client, tenant_id, kind, dry_run):
    headers = {"X-Tenant-Id": tenant_id}
    token = _token(kind, tenant_id)
    if token is not None:
        headers["X-Step-Up-Token"] = token
    return client.post(_URL.format(dry_run), headers=headers, json=_PARAMS)


@pytest.mark.parametrize("kind,status,body", [
    ("missing", 401, _DRY_RUN_SENTENCE),
    ("empty", 401, _DRY_RUN_SENTENCE),
    ("whitespace", 401, _INVALID),
    ("invalid", 401, _INVALID),
    ("wrong-tenant", 401, _INVALID),
    ("read-scoped", 401, _INVALID),
    # The control: a valid token opens the gate, and the commit-mode
    # allowlist (#572) is what refuses the Observation row next.
    ("valid", 422, _COMMIT_REFUSED),
])
def test_commit_mode_answers_exactly_as_before(client, tenant_id,
                                               kind, status, body):
    resp = _post(client, tenant_id, kind, "false")
    assert (resp.status_code, resp.get_data(as_text=True)) == (status, body)


@pytest.mark.parametrize("kind", [
    "missing", "empty", "whitespace", "invalid", "wrong-tenant",
    "read-scoped", "valid",
])
def test_dry_run_skips_the_gate_for_every_token(client, tenant_id, kind):
    resp = _post(client, tenant_id, kind, "true")
    assert (resp.status_code, resp.get_data(as_text=True)) == (200, _PREVIEW)
