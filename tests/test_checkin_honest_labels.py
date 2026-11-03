"""SMART Health Check-in wallet: honest statuses and record names.

Found running the connectathon's advanced track (narrowed-family) against a
local wallet on 2026-11-03. What these pin, by running the shipped
careagents/static/checkin_answer.js under node:

- An item that asks only for kinds of record this wallet never shares
  (Observation labs and vitals, say) is `unsupported`, and the page says
  "This wallet can't answer this", not "Nothing on file": the account may
  well hold those records. `unavailable` stays for kinds we do share that
  the person has none of.
- Nothing about what is sent changes: an item that today matches a record
  is still answered from it.
- A Patient is named by every given name, and an Organization by its own
  `name` (as a Coverage already names its payer), never by "A record" or by
  upstream `text`.

Synthetic data only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "careagents" / "static"
CANARY = "CANARY-UPSTREAM-WORDS"

_RUN = """
const m = await import(process.argv[1]);
const request = JSON.parse(process.argv[2]);
const bundle = JSON.parse(process.argv[3]);
const matches = m.matchItems(request, [], m.prepareRecords(bundle));
const share = Object.fromEntries(request.items.map((i) => [i.id, true]));
console.log(JSON.stringify({
  types: m.requestedTypes(request, []),
  unsupported: matches.map((x) => x.unsupported),
  names: matches.map((x) => m.recordNames(x)),
  response: m.buildResponse(request, matches, share),
}));
"""


def _run(items, entries=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    request = {"type": "smart-health-checkin-request", "version": "1",
               "id": "honest-labels", "fhirVersions": ["4.0.1"],
               "items": items}
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"fullUrl": f"urn:uuid:{r['resourceType']}-{r['id']}",
                         "resource": r} for r in entries]}
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN,
         str(STATIC / "checkin_answer.js"), json.dumps(request),
         json.dumps(bundle)],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _item(item_id, **content):
    return {"id": item_id, "title": item_id,
            "content": {"kind": "selection.fhir", **content},
            "accept": ["application/fhir+json"]}


def _status(out):
    return {s["item"]: s["status"] for s in out["response"]["requestStatus"]}


# --- unsupported, not "Nothing on file" -------------------------------------

def test_an_item_for_a_kind_of_record_never_sent_is_unsupported():
    # narrowed-family: labs and vitals only. The relay is never asked.
    out = _run([_item("labs-vitals",
                      profilesFrom=["http://hl7.org/fhir/us/core"],
                      resourceTypes=["Observation"])])
    assert out["types"] == []
    assert out["unsupported"] == [True]
    assert _status(out) == {"labs-vitals": "unsupported"}


def test_a_profile_only_for_a_kind_never_sent_is_unsupported():
    lab = ("http://hl7.org/fhir/us/core/StructureDefinition/"
           "us-core-observation-lab")
    out = _run([_item("labs", profiles=[lab])])
    assert _status(out) == {"labs": "unsupported"}


def test_a_profile_this_wallet_does_not_know_is_unsupported():
    # Deliberate: a Condition under a profile outside PROFILE_TYPES asks the
    # relay for nothing (requestedTypes), so the wallet can't claim it.
    out = _run([_item("other", resourceTypes=["Condition"],
                      profiles=["http://example.org/StructureDefinition/c"])])
    assert out["types"] == []
    assert _status(out) == {"other": "unsupported"}


def test_a_kind_we_share_with_none_on_file_is_still_unavailable():
    out = _run([_item("problems", resourceTypes=["Condition"]),
                _item("mixed", resourceTypes=["Observation", "Condition"])])
    assert out["types"] == ["Condition"]
    assert _status(out) == {"problems": "unavailable", "mixed": "unavailable"}


def test_what_is_sent_does_not_change():
    # A no-selector item fetches everything, a Coverage brings its payer, and
    # an item naming only Organization matches that payer. It was answered
    # from it before this change and still is.
    org = {"resourceType": "Organization", "id": "payer-1",
           "name": "Example Health Plan"}
    cov = {"resourceType": "Coverage", "id": "cov-1", "status": "active",
           "subscriberId": "SYN12345678",
           "payor": [{"reference": "Organization/payer-1"}]}
    out = _run([_item("everything"), _item("orgs", resourceTypes=["Organization"])],
               [cov, org])
    assert out["unsupported"] == [False, False]
    assert _status(out) == {"everything": "fulfilled", "orgs": "fulfilled"}
    assert out["names"][1] == ["Example Health Plan"]


# --- names -------------------------------------------------------------------

def test_a_patient_is_named_by_every_given_name():
    patient = {"resourceType": "Patient", "id": "p1",
               "name": [{"given": ["Casey", "Jordan"], "family": "Synthetic"}],
               "birthDate": "1985-07-04"}
    out = _run([_item("me", resourceTypes=["Patient"])], [patient])
    assert out["names"] == [["Casey Jordan Synthetic, born 4 July 1985"]]


def test_a_given_name_that_is_not_a_string_is_left_out():
    patient = {"resourceType": "Patient", "id": "p1",
               "name": [{"given": ["Casey", None, {"x": 1}, "  "],
                         "family": "Synthetic"}]}
    out = _run([_item("me", resourceTypes=["Patient"])], [patient])
    assert out["names"] == [["Casey Synthetic"]]


def test_an_organization_is_named_not_a_record():
    org = {"resourceType": "Organization", "id": "payer-1",
           "name": "Example Health Plan"}
    out = _run([_item("orgs", resourceTypes=["Organization"])], [org])
    assert out["names"] == [["Example Health Plan"]]


def test_an_unnamed_organization_never_reads_its_upstream_text():
    org = {"resourceType": "Organization", "id": "payer-1",
           "text": {"status": "generated", "div": f"<div>{CANARY}</div>"},
           "alias": [CANARY],
           "type": [{"text": CANARY,
                     "coding": [{"code": "ins", "display": CANARY}]}]}
    out = _run([_item("orgs", resourceTypes=["Organization"])], [org])
    assert out["names"] == [["An organization"]]
    assert CANARY not in json.dumps(out["names"])


@pytest.mark.parametrize("name, insurer", [
    ("   ", "Your insurer"),
    ("", "Your insurer"),
    ("  Example Health Plan  ", "Example Health Plan"),
])
def test_a_coverage_payer_name_is_trimmed_and_blank_is_missing(name, insurer):
    # The same rule as the Organization line above (#1105 QA E3).
    org = {"resourceType": "Organization", "id": "payer-1", "name": name}
    cov = {"resourceType": "Coverage", "id": "cov-1", "status": "active",
           "subscriberId": "SYN12345678",
           "payor": [{"reference": "Organization/payer-1"}]}
    out = _run([_item("cov", resourceTypes=["Coverage"])], [cov, org])
    assert out["names"] == [[f"{insurer}, member ID ending 5678"]]
