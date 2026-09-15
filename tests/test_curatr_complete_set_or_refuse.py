"""An approved fix set is applied completely, once, or refused whole (#413 P1-B).

Root vocabulary was the only check (#740). The applier walked any remaining
path, created missing parents, skipped a `null`, and reported the count of
fixes it managed rather than the set that was approved — so a mixed request
could half-apply and say it succeeded. Now every path is parsed against a
code-owned grammar, every value is shape-checked, the whole set is applied
to a copy, and the record is written once only if every fix applied. The
outcome names exactly what changed, in normalised paths.
"""
import json

import pytest

from models import db
from r6.curatr import FixRefused, apply_fix, plan_fixes
from r6.models import R6Resource

RID = "c-set-1"


def _record():
    return {"resourceType": "Condition", "id": RID,
            "subject": {"reference": "Patient/alice"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.fixture
def seeded(app, tenant_id):
    with app.app_context():
        db.session.add(R6Resource("Condition", json.dumps(_record()),
                                  resource_id=RID, tenant_id=tenant_id))
        db.session.commit()


def _stored(app, tenant_id):
    with app.app_context():
        row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                         tenant_id=tenant_id).first()
        provs = R6Resource.query.filter_by(resource_type="Provenance",
                                           tenant_id=tenant_id).count()
        return json.loads(row.resource_json), row.version_id, provs


def _apply(app, tenant_id, fixes):
    with app.app_context():
        return apply_fix("Condition", RID, fixes, "test", tenant_id)


GOOD = {"field_path": "Condition.clinicalStatus.coding[0].code",
        "new_value": "resolved"}

BAD_CASES = {
    "index-out-of-range": {"field_path": "Condition.code.coding[3].code",
                           "new_value": "x"},
    "index-on-a-root-without-codings": {"field_path": "Condition.onsetDateTime.coding[0]",
                                        "new_value": {"code": "x"}},
    "unknown-leaf": {"field_path": "Condition.code.coding[0].userSelected",
                     "new_value": True},
    "sub-path-off-the-grammar": {"field_path": "Condition.subject.reference",
                                 "new_value": "Patient/mallory"},
    "null-value": {"field_path": "Condition.recordedDate", "new_value": None},
    "wrong-shape-date": {"field_path": "Condition.onsetDateTime",
                         "new_value": "last spring"},
    "wrong-shape-codeable-with-text": {"field_path": "Condition.code",
                                       "new_value": {"coding": [{"code": "x"}],
                                                     "text": "free text"}},
    "coding-with-extra-key": {"field_path": "Condition.code.coding[0]",
                              "new_value": {"code": "x", "extension": []}},
    "overlapping-paths": {"field_path": "Condition.clinicalStatus",
                          "new_value": {"coding": [{"code": "inactive"}]}},
    "relink-a-linked-record": {"field_path": "Condition.subject",
                               "new_value": {"reference": "Patient/alice"}},
    "other-type-path": {"field_path": "Patient.name", "new_value": []},
    "control-characters": {"field_path": "Condition.code.coding[0].display",
                           "new_value": "bad\x00value"},
}


@pytest.mark.parametrize("bad", BAD_CASES.values(), ids=BAD_CASES.keys())
def test_a_valid_fix_followed_by_an_invalid_one_changes_nothing(
        app, tenant_id, seeded, bad):
    result = _apply(app, tenant_id, [GOOD, bad])
    assert result.get("refused") is True, result
    stored, version, provs = _stored(app, tenant_id)
    assert stored == _record()
    assert version == 1
    assert provs == 0


def test_the_refusal_names_the_position_not_the_submitted_text(app, tenant_id, seeded):
    result = _apply(app, tenant_id, [GOOD, {"field_path": "Condition.note",
                                            "new_value": "SECRET-7781"}])
    assert result["refused"] is True
    assert "fix 2" in result["error"]
    assert "note" not in result["error"]
    assert "SECRET" not in result["error"]


def test_the_whole_valid_set_is_applied_once_and_reported_exactly(
        app, tenant_id, seeded):
    fixes = [GOOD,
             {"field_path": "Condition.code.coding[0].system",
              "new_value": "http://hl7.org/fhir/sid/icd-10-cm"},
             {"field_path": "Condition.onsetDateTime", "new_value": "2024-03-01"},
             {"field_path": "Condition.category",
              "new_value": [{"coding": [{"code": "problem-list-item"}]}]}]
    result = _apply(app, tenant_id, fixes)
    assert "error" not in result, result
    assert result["issues_fixed"] == 4
    stored, version, provs = _stored(app, tenant_id)
    assert version == 2 and provs == 1
    assert stored["clinicalStatus"]["coding"][0]["code"] == "resolved"
    assert stored["code"]["coding"][0]["system"] == "http://hl7.org/fhir/sid/icd-10-cm"
    assert stored["code"]["coding"][0]["code"] == "44054006"   # untouched sibling
    assert stored["onsetDateTime"] == "2024-03-01"
    assert stored["category"] == [{"coding": [{"code": "problem-list-item"}]}]
    assert stored["subject"] == {"reference": "Patient/alice"}
    # The reported summary is the normalised path set, all of it, nothing else.
    assert result["change_summary"] == (
        "Condition.clinicalStatus.coding[0].code updated; "
        "Condition.code.coding[0].system updated; "
        "Condition.onsetDateTime updated; Condition.category updated")
    ext = {e["url"]: e for e in result["provenance"]["extension"][0]["extension"]}
    assert ext["changes_applied"]["valueInteger"] == 4
    assert ext["change_summary"]["valueString"] == result["change_summary"]


def test_a_set_that_changes_nothing_is_refused(app, tenant_id, seeded):
    result = _apply(app, tenant_id, [{"field_path": "Condition.clinicalStatus.coding[0].code",
                                      "new_value": "active"}])
    assert result["refused"] is True
    assert _stored(app, tenant_id)[1] == 1


def test_a_missing_codeable_root_can_be_given_codings(app, tenant_id, seeded):
    with app.app_context():
        result = apply_fix("Condition", RID,
                           [{"field_path": "Condition.verificationStatus.coding",
                             "new_value": [{"code": "confirmed"}]}],
                           "test", tenant_id)
    assert "error" not in result, result
    assert _stored(app, tenant_id)[0]["verificationStatus"] == {
        "coding": [{"code": "confirmed"}]}


def test_the_plan_never_touches_the_record_it_was_given():
    current = _record()
    before = json.dumps(current, sort_keys=True)
    with pytest.raises(FixRefused):
        plan_fixes("Condition", current, [GOOD, BAD_CASES["null-value"]], "t")
    fixed, paths = plan_fixes("Condition", current, [GOOD], "t")
    assert json.dumps(current, sort_keys=True) == before
    assert fixed["clinicalStatus"]["coding"][0]["code"] == "resolved"
    assert paths == ["Condition.clinicalStatus.coding[0].code"]


def test_more_than_twenty_fixes_is_refused():
    with pytest.raises(FixRefused):
        plan_fixes("Condition", _record(), [GOOD] * 21, "t")


def test_a_null_is_refused_as_a_removal_not_as_a_wrong_shape(app, tenant_id, seeded):
    result = _apply(app, tenant_id, [GOOD, BAD_CASES["null-value"]])
    assert result["refused"] is True
    assert "removing a field is not an operation" in result["error"]


def test_the_summary_is_the_normalised_path_not_the_submitted_spelling(
        app, tenant_id, seeded):
    result = _apply(app, tenant_id, [{"field_path": "Condition.clinicalStatus.coding[00].code",
                                      "new_value": "resolved"}])
    assert "error" not in result, result
    assert result["change_summary"] == "Condition.clinicalStatus.coding[0].code updated"
    assert "[00]" not in json.dumps(result["provenance"])
