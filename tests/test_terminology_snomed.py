"""SNOMED CT labels in the server's terminology table (#577).

Allergies, reactions and conditions arrive SNOMED-coded. With no SNOMED rows
in r6/terminology.py, redaction stripped the upstream `display`/`text` and
nothing put a name back, so those records read as blank. These tests pin the
fix and its two limits:

1. A SNOMED-coded record read back through the redacting read carries the
   SERVER's label, and the upstream display planted beside it does not
   survive. Same property test_terminology_labels.py pins for LOINC.
2. Every SNOMED row is a code the product can actually meet — in seed or demo
   data, a fixture, or a rule set. The table is a label list for what we
   carry, not a general SNOMED dictionary; growing it means a code shows up
   in the product first.
"""

import json
import re
from pathlib import Path

from models import db
from r6.models import R6Resource
from r6.redaction import apply_redaction
from r6.terminology import _LABELS, SNOMED

SCT = "http://snomed.info/sct"
PLANTED = "Penicillin for Jane Secret"
REPO = Path(__file__).resolve().parents[1]


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id,
        ))
        db.session.commit()


def test_a_snomed_allergy_read_back_gets_the_server_label_not_upstream(
        client, app, tenant_id, tenant_headers):
    """THE PROPERTY, over HTTP: store an allergy whose upstream display and
    text carry a name, read it back, and the only label is ours.

    MUTATION: delete the (SNOMED, "91936005") row -> the allergen comes back
    unlabelled and this goes red.
    """
    _store(app, {
        "resourceType": "AllergyIntolerance", "id": "snomed-577",
        "patient": {"reference": "Patient/p-577"},
        "code": {"coding": [{"system": SCT, "code": "91936005",
                             "display": PLANTED}],
                 "text": PLANTED},
    }, tenant_id)

    resp = client.get("/r6/fhir/AllergyIntolerance/snomed-577",
                      headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()

    assert "Jane" not in json.dumps(body) and "Secret" not in json.dumps(body)
    assert body["code"]["coding"][0]["display"] == "Allergy to penicillin"
    assert body["code"]["text"] == "Allergy to penicillin"


def test_a_snomed_condition_is_labelled_by_the_redaction_pass():
    cond = {"resourceType": "Condition",
            "code": {"coding": [{"system": "urn:oid:2.16.840.1.113883.6.96",
                                 "code": "44054006",
                                 "display": "Diabetes for Jane Secret"}]}}
    out = apply_redaction(cond)
    assert "Jane" not in str(out)
    assert out["code"]["coding"][0]["display"] == "Type 2 diabetes mellitus"


def _codes_the_product_can_meet() -> set[str]:
    """Every digit run in a file that mentions SNOMED, across the code and
    data the product runs or is tested with. This file and the terminology
    table itself are excluded, or every row would vouch for itself."""
    excluded = {REPO / "r6" / "terminology.py"}
    roots = ["r6", "scripts", "tests", "careagents"]
    found: set[str] = set()
    for root in roots:
        for path in (REPO / root).rglob("*"):
            if (path in excluded or path.name.startswith("test_terminology")
                    or path.suffix not in {".py", ".json", ".ndjson"}):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "snomed" not in text.lower():
                continue
            found.update(re.findall(r"\b\d{6,18}\b", text))
    return found


def test_every_snomed_label_is_for_a_code_the_product_meets():
    """The guard that keeps this a label list, not a dictionary.

    MUTATION: add a SNOMED row for a code that appears nowhere else in the
    repo (e.g. ("SNOMED", "22298006") myocardial infarction) -> red.
    """
    snomed_codes = {code for system, code in _LABELS if system == SNOMED}
    assert snomed_codes, "no SNOMED rows at all — #577 has regressed"
    orphans = sorted(snomed_codes - _codes_the_product_can_meet())
    assert not orphans, (
        f"SNOMED labels for codes nothing in the product carries: {orphans}")
