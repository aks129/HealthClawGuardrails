"""QA for PR #1105: the cases test_checkin_honest_labels.py leaves open.

- "unsupported" is decided per item, not per request: a clinic that asks
  for problems AND labs in one request gets "can't answer" for the labs
  only. A fix that asked requestedTypes about the whole request would pass
  every single-item test in the Dev file.
- A shared kind asked for by a versioned profile, or by the whole US Core
  family, with nothing on file, stays "unavailable".
- An Organization's name is used only when it is a non-blank string, and
  trimmed; anything else reads "An organization".
- An item asking only for Organization, with no payer on file, now reads
  unsupported (Organization is not in CHECKIN_TYPES, so the relay is never
  asked for one on its own). Pinned so the change is a decision, not drift.

Runs the shipped careagents/static/checkin_answer.js under node. Synthetic
data only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parent.parent
ANSWER = ROOT / "careagents" / "static" / "checkin_answer.js"
US_CORE = "http://hl7.org/fhir/us/core"
SD = US_CORE + "/StructureDefinition/"

_RUN = """
const m = await import(process.argv[1]);
const request = JSON.parse(process.argv[2]);
const bundle = JSON.parse(process.argv[3]);
const matches = m.matchItems(request, [], m.prepareRecords(bundle));
const share = Object.fromEntries(request.items.map((i) => [i.id, true]));
console.log(JSON.stringify({
  names: matches.map((x) => m.recordNames(x)),
  response: m.buildResponse(request, matches, share),
}));
"""


def _run(items, entries=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    request = {"type": "smart-health-checkin-request", "version": "1",
               "id": "honest-labels-qa", "fhirVersions": ["4.0.1"],
               "items": items}
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"fullUrl": f"urn:uuid:{r['resourceType']}-{r['id']}",
                         "resource": r} for r in entries]}
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN, str(ANSWER),
         json.dumps(request), json.dumps(bundle)],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _item(item_id, **content):
    return {"id": item_id, "title": item_id,
            "content": {"kind": "selection.fhir", **content},
            "accept": ["application/fhir+json"]}


def _status(out):
    return {s["item"]: s["status"] for s in out["response"]["requestStatus"]}


CONDITION = {"resourceType": "Condition", "id": "c1",
             "meta": {"profile": [SD + "us-core-condition-problems-health-concerns"]},
             "code": {"coding": [{"system": "http://snomed.info/sct",
                                  "code": "38341003"}]}}


def test_unsupported_is_decided_per_item_not_per_request():
    # The narrowed-family item beside an ordinary problems item.
    labs = _item("labs-vitals", profilesFrom=[US_CORE],
                 resourceTypes=["Observation"])
    problems = _item("problems", resourceTypes=["Condition"])
    out = _run([labs, problems], [CONDITION])
    assert _status(out) == {"labs-vitals": "unsupported",
                            "problems": "fulfilled"}
    # And with nothing on file for the shared kind.
    out = _run([labs, problems])
    assert _status(out) == {"labs-vitals": "unsupported",
                            "problems": "unavailable"}


def test_a_shared_kind_by_versioned_profile_with_none_on_file_is_unavailable():
    out = _run([_item("problems", profiles=[SD + "us-core-condition|6.1.0"])])
    assert _status(out) == {"problems": "unavailable"}


def test_the_whole_us_core_family_with_none_on_file_is_unavailable():
    # uscdi.json's shape: profilesFrom US Core, no resourceTypes.
    out = _run([_item("uscdi", profilesFrom=[US_CORE])])
    assert _status(out) == {"uscdi": "unavailable"}


@pytest.mark.parametrize("name", ["   ", "", {"text": "X"}, ["X"], 42, None])
def test_an_organization_name_that_is_not_a_real_string_is_not_used(name):
    org = {"resourceType": "Organization", "id": "payer-1", "name": name}
    out = _run([_item("orgs", resourceTypes=["Organization"])], [org])
    assert out["names"] == [["An organization"]]


def test_an_organization_name_is_trimmed():
    org = {"resourceType": "Organization", "id": "payer-1",
           "name": "  Example Health Plan  "}
    out = _run([_item("orgs", resourceTypes=["Organization"])], [org])
    assert out["names"] == [["Example Health Plan"]]


def test_an_organization_only_item_with_no_payer_on_file_is_unsupported():
    out = _run([_item("orgs", resourceTypes=["Organization"])])
    assert _status(out) == {"orgs": "unsupported"}


def test_a_non_string_first_given_name_does_not_hide_the_rest():
    # The Dev file's case keeps "Casey" first, so main's given[0] passes it
    # too. With the bad entry first, main shows "Synthetic" alone.
    patient = {"resourceType": "Patient", "id": "p1",
               "name": [{"given": [None, "Casey", 7, "Jordan"],
                         "family": "Synthetic"}]}
    out = _run([_item("me", resourceTypes=["Patient"])], [patient])
    assert out["names"] == [["Casey Jordan Synthetic"]]
