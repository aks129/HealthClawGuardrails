"""The %patient bound holds on the form-fill rail, not only on `$populate` (#368).

#368 asked for an allowlist of the demographics the populate step may copy
into a draft QuestionnaireResponse unredacted, enforced so that a new
questionnaire item cannot silently widen the set. Council ruling D10 is that
allowlist: `r6/sdc/expressions.py::patient_projection`, pinned over the
`$populate` route by tests/test_sdc_populate_bounded.py.

The issue names two other exits, and neither is the `$populate` route:

  - the REVIEW PAGE (GET /r6/actions/<id>/review), which renders the draft
    that `r6/actions/review.py::_draft_qr` populates, and
  - the SIGNED DELIVERY LINK, which serves a PDF of the reviewed response
    that the review submit assembles from the same draft.

Both reach the engine through `populate_questionnaire` directly. The bound
lives in the engine's `build_context`, so they inherit it — but nothing
measured that, and a later change that populated the rail from the stored
Patient directly would have kept every existing test green. These tests run
the whole rail (propose -> commit -> review -> confirm -> download) against a
stored Questionnaire that asks for more than the allowlist in two ways:

  1. an EXISTING demographics item re-pointed at `%patient.identifier`, under
     a linkId the review page has a label for, so the page would print it;
  2. a NEW item asking for `%patient.contact`, which is exactly the "future
     questionnaire item asking for something else" the issue describes.

Each negative assertion is paired with a positive one on the same response:
the allowlisted family name must arrive, so a page that rendered nothing
cannot pass as a bounded one.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest

from models import db
from r6.models import R6Resource
from r6.sdc.intake import INITIAL_EXPRESSION_URL, intake_questionnaire
from tests.test_redaction_probes_multistep import pdf_text

ALLOWED_FAMILY = "Boundfamilyallowed"
IDENTIFIER_MARKER = "PHIIDENTIFIERMARKER368"
CONTACT_MARKER = "PHICONTACTMARKER368"
WITHHELD = (IDENTIFIER_MARKER, CONTACT_MARKER)

PATIENT_ID = "bound-rail-patient"
QUESTIONNAIRE_ID = "bound-rail-intake"


def _store(resource, tenant_id):
    row = R6Resource(resource_type=resource["resourceType"],
                     resource_json=json.dumps(resource),
                     resource_id=resource["id"], tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()


def _patient():
    return {
        "resourceType": "Patient", "id": PATIENT_ID,
        "name": [{"family": ALLOWED_FAMILY, "given": ["Rhea"]}],
        "gender": "female", "birthDate": "1971-05-06",
        "identifier": [{"system": "urn:oid:2.16.840.1.113883.4.1",
                        "value": IDENTIFIER_MARKER}],
        "contact": [{"name": {"family": CONTACT_MARKER}}],
    }


def _expr(expression):
    return [{"url": INITIAL_EXPRESSION_URL,
             "valueExpression": {"language": "text/fhirpath",
                                 "expression": expression}}]


def _widened_intake():
    """The canonical intake, asking for two things D10 does not allow."""
    questionnaire = copy.deepcopy(intake_questionnaire())
    questionnaire["id"] = QUESTIONNAIRE_ID
    demographics = next(g for g in questionnaire["item"]
                        if g["linkId"] == "demographics")
    phone = next(i for i in demographics["item"]
                 if i["linkId"] == "demographics.phone")
    # (1) The review page labels this linkId "Phone" and prints its answer.
    phone["extension"] = _expr("%patient.identifier.value.first()")
    # (2) A new item, the shape the issue says would widen the set silently.
    demographics["item"].append({
        "linkId": "demographics.emergency-contact",
        "type": "string",
        "text": "Emergency contact",
        "extension": _expr("%patient.contact.name.family.first()"),
    })
    return questionnaire


@pytest.fixture
def widened_rail(app, tenant_headers, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    with app.app_context():
        _store(_patient(), tenant_headers["X-Tenant-Id"])
        _store(_widened_intake(), tenant_headers["X-Tenant-Id"])


def _propose_and_commit(client, tenant_headers, auth_headers):
    proposed = client.post(
        "/r6/actions/propose", headers=tenant_headers,
        json={"kind": "form-fill",
              "payload": {"to": "Intake portal",
                          "questionnaire": "Questionnaire/%s" % QUESTIONNAIRE_ID,
                          "subject": {"reference": "Patient/%s" % PATIENT_ID},
                          "body": "new patient intake form"}})
    assert proposed.status_code == 201, proposed.get_data(as_text=True)
    action_id = proposed.get_json()["id"]
    committed = client.post("/r6/actions/%s/commit" % action_id,
                            headers=auth_headers)
    assert committed.status_code == 202, committed.get_data(as_text=True)
    return action_id


def test_the_review_page_shows_only_the_allowlisted_demographics(
        client, tenant_headers, auth_headers, widened_rail):
    """MUTATION: hand `build_context` the stored Patient instead of
    `patient_projection(subject)` -> red, the identifier printed as "Phone"."""
    action_id = _propose_and_commit(client, tenant_headers, auth_headers)

    page = client.get("/r6/actions/%s/review" % action_id,
                      headers=auth_headers)
    assert page.status_code == 200, page.get_data(as_text=True)[:300]
    html = page.get_data(as_text=True)

    assert ALLOWED_FAMILY in html, (
        "the allowlisted family name is missing from the review page, so the "
        "absence checks below would pass on a page that rendered nothing")
    leaked = [m for m in WITHHELD if m in html]
    assert not leaked, (
        "%s reached the review page: the form-fill rail populated an element "
        "outside the %%patient allowlist (council ruling D10, #368)" % leaked)


def test_the_signed_delivery_link_carries_only_the_allowlisted_demographics(
        app, client, tenant_headers, auth_headers, widened_rail):
    """MUTATION: as above -> red, both markers in the reviewed response and
    in the PDF the signed link serves.

    Checks two stores of the same answers: the reviewed QuestionnaireResponse
    (what the PDF is rendered from) and the PDF itself (what leaves over a
    link carrying no tenant or step-up header).
    """
    from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
    from r6.actions.models import ProposedAction
    from r6.stepup import generate_step_up_token
    from tests.approval_helpers import bound_operation

    action_id = _propose_and_commit(client, tenant_headers, auth_headers)

    # No allergy rows exist, so the attestation gate needs the explicit NKA
    # checkbox — the only way that answer is ever set.
    review = client.post("/r6/actions/%s/review" % action_id,
                         headers=auth_headers, json={"nka": "true"})
    assert review.status_code == 200, review.get_data(as_text=True)
    reviewed_qr_id = review.get_json()["reviewed_qr_id"]

    with app.app_context():
        row = R6Resource.query.filter_by(
            resource_type="QuestionnaireResponse", id=reviewed_qr_id,
            tenant_id=tenant_headers["X-Tenant-Id"]).first()
        reviewed = row.resource_json
    assert ALLOWED_FAMILY in reviewed, reviewed
    assert not [m for m in WITHHELD if m in reviewed], (
        "the reviewed QuestionnaireResponse carries an element outside the "
        "%patient allowlist; the delivery PDF is rendered from it")

    approval = dict(auth_headers)
    approval["X-Step-Up-Token"] = generate_step_up_token(
        tenant_headers["X-Tenant-Id"], audience=ACTION_APPROVAL_AUDIENCE,
        operation=bound_operation(client.application, action_id))
    confirm = client.post("/r6/actions/%s/confirm" % action_id,
                          headers=approval, json={})
    assert confirm.status_code == 200, confirm.get_data(as_text=True)
    assert confirm.get_json()["status"] == "completed", \
        confirm.get_data(as_text=True)

    with app.app_context():
        outcome = json.loads(
            db.session.get(ProposedAction, action_id).outcome_summary)
    parsed = urlparse(outcome["delivery_link"])
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    # No tenant/step-up headers: the signature is the whole credential.
    downloaded = client.get(parsed.path, query_string=query)
    assert downloaded.status_code == 200, downloaded.get_data(as_text=True)[:200]
    assert downloaded.mimetype == "application/pdf"

    text = pdf_text(downloaded.data)
    assert ALLOWED_FAMILY in text, (
        "the allowlisted family name is not in the delivered PDF, so the "
        "absence check below would pass on a PDF the extractor cannot read")
    leaked = [m for m in WITHHELD if m in text]
    assert not leaked, (
        "%s reached the PDF behind the signed delivery link: the form-fill "
        "rail populated an element outside the %%patient allowlist (#368)"
        % leaked)
