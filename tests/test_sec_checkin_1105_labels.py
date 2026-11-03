"""Security probes for PR #1105 (check-in wallet: `unsupported` for kinds
never shared; Patient and Organization labels). Synthetic data only.

What these pin, running the shipped careagents/static/checkin_answer.js
under node, against bundles produced by the real engine export where it
matters:

- The new Organization label shows nothing the website is not already
  sent: it is the exact `name` the engine's check-in export carries
  (r6/access.py _checkin_organization), and the Coverage line already showed
  it on the same review. Upstream `text`, `alias` and type words never reach
  the label or the export.
- The new `unsupported` status is not a record oracle: whether an item reads
  `unsupported` rather than `unavailable` depends on the request alone,
  never on which records the account holds. The one item whose
  `unsupported` varies with records is one asking for Organization, which
  is held only as a Coverage's payer: it reads `unsupported` or is answered,
  the same split it showed before #1105 as `unavailable` or answered.
- So an Organization item is answered exactly when the Coverage item is,
  and tells the website nothing the Coverage item's own status does not.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import checkin_fixtures as f

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "careagents" / "static" / "checkin_answer.js"

PERSONISH = "CANARY-Guarantor-Jordan-Rivera"
CANARY_TEXT = "CANARY-org-narrative"
CANARY_ALIAS = "CANARY-org-alias"
CANARY_TYPE = "CANARY-org-type-words"

_RUN = """
const m = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const out = cases.map(({ request, bundle, share }) => {
  const matches = m.matchItems(request, [], m.prepareRecords(bundle));
  const shareMap = Object.fromEntries(request.items.map((i) => [i.id, share]));
  return {
    types: m.requestedTypes(request, []),
    names: matches.map((x) => m.recordNames(x)),
    response: m.buildResponse(request, matches, shareMap),
  };
});
console.log(JSON.stringify(out));
"""


def _node(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN, str(JS), json.dumps(cases)],
        capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout)


def _item(item_id, **content):
    return {"id": item_id, "title": item_id,
            "content": {"kind": "selection.fhir", **content},
            "accept": ["application/fhir+json"]}


def _request(items):
    return {"type": "smart-health-checkin-request", "version": "1",
            "id": "sec-1105", "fhirVersions": ["4.0.1"], "items": items}


def _status(res):
    return {s["item"]: s["status"] for s in res["response"]["requestStatus"]}


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)


# --- the Organization label vs what the engine sends --------------------------

def test_the_org_label_is_exactly_what_the_engine_export_already_sends(
        app, client):
    org = f.organization(name=PERSONISH)
    org['text'] = {'status': 'generated', 'div': f'<div>{CANARY_TEXT}</div>'}
    org['alias'] = [CANARY_ALIAS]
    org['type'] = [{'text': CANARY_TYPE,
                    'coding': [{'code': 'pay', 'display': CANARY_TYPE}]}]
    f.seed(app, f.patient(), f.coverage(), org)
    types = ['Patient', 'Coverage']
    resp = f.export(client, f.token(client, types), types)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    bundle = resp.get_json()
    exported = json.dumps(bundle)
    # Pre-existing, by design: the website is sent the stored name verbatim.
    exported_org, = f.resources(resp, 'Organization')
    assert exported_org['name'] == PERSONISH
    for canary in (CANARY_TEXT, CANARY_ALIAS, CANARY_TYPE):
        assert canary not in exported

    items = [_item("everything"), _item("coverage", resourceTypes=["Coverage"]),
             _item("orgs", resourceTypes=["Organization"])]
    res, = _node([{"request": _request(items), "bundle": bundle,
                   "share": True}])
    everything, coverage, orgs = res["names"]
    # The Coverage line named it before this PR; the new line is the same
    # name, on the same review.
    assert any(PERSONISH in line for line in coverage)
    assert orgs == [PERSONISH]
    assert PERSONISH in everything
    shown = json.dumps(res["names"])
    for canary in (CANARY_TEXT, CANARY_ALIAS, CANARY_TYPE):
        assert canary not in shown
    # Nothing the review shows is added to what leaves: the answer carries
    # exactly the exported resources, and no label string.
    sent = json.dumps(res["response"])
    assert "member ID" not in sent and "An organization" not in sent
    sent_orgs = [e["resource"] for a in res["response"]["artifacts"]
                 for e in a["value"]["entry"]
                 if e["resource"]["resourceType"] == "Organization"]
    assert sent_orgs and all(o == exported_org for o in sent_orgs)


# --- `unsupported` is not a record oracle --------------------------------------

UC = "http://hl7.org/fhir/us/core/StructureDefinition/"


def _r(rtype, rid, profile, **kw):
    return {"resourceType": rtype, "id": rid,
            "meta": {"profile": [UC + profile]}, **kw}


RECORDS = {
    "patient": _r("Patient", "p1", "us-core-patient",
                  name=[{"given": ["Ana"], "family": "Testperson"}]),
    "condition": _r("Condition", "c1",
                    "us-core-condition-problems-health-concerns",
                    subject={"reference": "Patient/p1"}),
    "coverage": _r("Coverage", "cov1", "us-core-coverage", status="active",
                   subscriberId="SYN12345678",
                   beneficiary={"reference": "Patient/p1"},
                   payor=[{"reference": "Organization/org1"}]),
    "org": _r("Organization", "org1", "us-core-organization", active=True,
              name="Synthetic Mutual Health Plan"),
}
HOLDINGS = [[], ["patient"], ["patient", "condition"],
            ["patient", "coverage", "org"],
            ["patient", "condition", "coverage", "org"]]


def _bundle(keys):
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": f"urn:uuid:{RECORDS[k]['resourceType']}-{RECORDS[k]['id']}",
         "resource": RECORDS[k]} for k in keys]}


PROBES = [
    _item("labs", resourceTypes=["Observation"]),
    _item("lab-profile", profiles=[UC + "us-core-observation-lab"]),
    _item("unknown-profile", resourceTypes=["Condition"],
          profiles=["http://example.org/StructureDefinition/c"]),
    _item("encounter", resourceTypes=["Encounter"]),
    _item("conditions", resourceTypes=["Condition"]),
    _item("mixed", resourceTypes=["Observation", "Condition"]),
    _item("stem", profilesFrom=["http://hl7.org/fhir/us/core"]),
    _item("coverage", resourceTypes=["Coverage"]),
    # Organization is the one kind that is never asked for yet can be held:
    # it rides along as a Coverage's payer.
    _item("orgs", resourceTypes=["Organization"]),
    _item("org-profile", profiles=[UC + "us-core-organization"]),
    _item("obs-or-org", resourceTypes=["Observation", "Organization"]),
]
ORG_PROBES = {"orgs", "org-profile", "obs-or-org"}


@pytest.mark.parametrize("share", [True, False])
def test_unsupported_versus_unavailable_never_depends_on_the_records(share):
    cases = [{"request": _request([item]), "bundle": _bundle(keys),
              "share": share} for item in PROBES for keys in HOLDINGS]
    results = _node(cases)
    seen = {}
    for case, res in zip(cases, results):
        item_id = case["request"]["items"][0]["id"]
        seen.setdefault(item_id, set()).add(_status(res)[item_id])
    for item_id, statuses in seen.items():
        assert not {"unsupported", "unavailable"} <= statuses, (item_id,
                                                                statuses)
    # An item for a kind never shared reads the same whatever is held.
    for item_id in ("labs", "lab-profile", "unknown-profile", "encounter"):
        assert seen[item_id] == {"unsupported"}, item_id
    # The only record-dependent `unsupported`: an Organization item, which
    # is answered when a payer is held and otherwise reads `unsupported`.
    # Before #1105 the same split read `unavailable`; the bit is unchanged
    # (see the parity test below).
    answered = "fulfilled" if share else "declined"
    for item_id in ORG_PROBES:
        assert seen[item_id] == {"unsupported", answered}, item_id
    varying = {i for i, s in seen.items() if "unsupported" in s and len(s) > 1}
    assert varying == ORG_PROBES


@pytest.mark.parametrize("share", [True, False])
def test_an_organization_item_says_no_more_than_the_coverage_item(share):
    # Organization is never fetched by type: it is in the records only as a
    # Coverage's payer, so a website learns from it only what the Coverage
    # item's own status already says.
    items = [_item("coverage", resourceTypes=["Coverage"]),
             _item("orgs", resourceTypes=["Organization"])]
    cases = [{"request": _request(items), "bundle": _bundle(keys),
              "share": share} for keys in HOLDINGS]
    for res in _node(cases):
        st = _status(res)
        assert (st["coverage"] in ("fulfilled", "declined")) == \
            (st["orgs"] in ("fulfilled", "declined")), st


@pytest.mark.parametrize("share", [True, False])
def test_beside_everything_an_organization_item_says_only_payer_held(share):
    # Without a Coverage item, the Organization item still says only
    # "a payer is held", which it said before #1105 as fulfilled versus
    # unavailable. Pre-existing; pinned so a change to it is seen.
    items = [_item("everything"), _item("orgs", resourceTypes=["Organization"])]
    cases = [{"request": _request(items), "bundle": _bundle(keys),
              "share": share} for keys in HOLDINGS]
    for keys, res in zip(HOLDINGS, _node(cases)):
        st = _status(res)
        assert (st["orgs"] in ("fulfilled", "declined")) == ("org" in keys), st
        assert st["orgs"] in ("fulfilled", "declined", "unsupported"), st
