"""QA for #1039: the current-conditions view, through the real engine.

1. The PR's canary test plants a name only in `display`, and only on
   records whose code the terminology table knows. `label_codings`
   overwrites such a display with the table's label, and the records with
   unknown codes are all resolved, so held back before anything is named.
   Measured by mutation: with `display` dropped from the engine's
   free-text keys, or `CodeableConcept.text` kept, that test still passes.
   The rows here plant the name in `display` AND `code.text`, on every
   path a Condition can take (known code, unknown code, repeat, held
   back, withdrawn, no status), and fail under both mutations.

2. A tenant with more Conditions than one page (200): the filter can only
   see the page, so it must still read as cut, never as the whole list.

Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from careagents.agent import _execute_tool
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

SNOMED = "http://snomed.info/sct"
CLINICAL = ("http://terminology.hl7.org/CodeSystem/"
            "condition-clinical")
CANARY = "QZXQA1039 Wilhelmina Snorkendale"


def _cond(rid, code, status="active", verification=None):
    res = {"resourceType": "Condition", "id": rid,
           "subject": {"reference": "Patient/qa-1039", "display": CANARY},
           "code": {"coding": [{"system": SNOMED, "code": code,
                                "display": CANARY}],
                    "text": CANARY}}
    if status is not None:
        res["clinicalStatus"] = {"coding": [{"system": CLINICAL,
                                             "code": status}]}
    if verification:
        res["verificationStatus"] = {"coding": [{"code": verification}]}
    return res


def _plant(chain, tenant, recs):
    from models import db
    from r6.models import R6Resource
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with chain.engine_app.app_context():
        for i, res in enumerate(recs):
            row = R6Resource(res["resourceType"], json.dumps(res),
                             resource_id=res["id"], tenant_id=tenant)
            # Written in list order: the first is the oldest.
            row.last_updated = base + timedelta(minutes=i)
            db.session.add(row)
        db.session.commit()


@pytest.fixture
def chain(cfg, svc, monkeypatch):  # noqa: F811
    from tests.test_beta_acceptance_rows import Chain
    return Chain(cfg, svc, monkeypatch)


def test_a_planted_name_never_reaches_the_model_on_any_path(
        chain, monkeypatch):
    tenant = "ca-qa-1039-canary"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    _plant(chain, tenant, [
        _cond("known", "38341003"),                  # labelled by code
        _cond("known-repeat", "38341003"),           # deduped
        _cond("unknown", "987654321"),               # no label: unreadable
        _cond("unstated", "876543219", status=None),  # stays in view
        _cond("resolved", "765432198", status="resolved"),  # held back
        _cond("withdrawn", "654321987", verification="entered-in-error"),
    ])
    for tool, args in (("search_records", {"resource_type": "Condition"}),
                       ("get_health_summary", {})):
        payload = _execute_tool(chain.hc, tenant, tool, args, [])
        assert "QZXQA1039" not in payload, (tool, payload)
        assert "Snorkendale" not in payload, (tool, payload)
    conds = json.loads(_execute_tool(chain.hc, tenant, "search_records",
                                     {"resource_type": "Condition"}, []))
    named = [i["name"] for i in conds if i.get("type") == "Condition"]
    # The known code is named from the table; the two unknown current
    # ones are said to be unreadable, never named from the record.
    assert "Hypertensive disorder" in named
    assert len(named) == 3, conds
    assert sum(n.startswith("unlabeled record") for n in named) == 2, named


NULL_FLAVOR = "http://terminology.hl7.org/CodeSystem/v3-NullFlavor"
ABSENT = "http://terminology.hl7.org/CodeSystem/data-absent-reason"


@pytest.mark.parametrize("codings", [
    # C-CDA problem with no code: nullFlavor UNK beside free text that
    # redaction strips (the allergy form of this is live, see agent.py).
    lambda i: [{"system": NULL_FLAVOR, "code": "UNK"}],
    # A local code each, plus the same "unknown" absent-reason coding.
    lambda i: [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.2.696580",
                "code": str(1000 + i)},
               {"system": ABSENT, "code": "unknown"}],
], ids=["nullflavor-only", "local-code-plus-absent-reason"])
def test_distinct_uncoded_conditions_are_not_merged(chain, monkeypatch,
                                                    codings):
    tenant = "ca-qa-1039-uncoded"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    recs = []
    for i in range(5):
        res = _cond(f"u{i}", "0")
        res["code"] = {"coding": codings(i), "text": f"{CANARY} problem {i}"}
        recs.append(res)
    _plant(chain, tenant, recs)
    payload = _execute_tool(chain.hc, tenant, "search_records",
                            {"resource_type": "Condition"}, [])
    assert "QZXQA1039" not in payload
    conds = json.loads(payload)
    # Five different problems on file: five entries, not one. On main
    # (no dedupe) all five were listed.
    assert sum(1 for i in conds if i.get("type") == "Condition") == 5, conds


ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"


def _coded(rid, codings):
    res = _cond(rid, "0")
    res["code"] = {"coding": [dict(c, display=CANARY) for c in codings],
                   "text": CANARY}
    return res


@pytest.mark.xfail(strict=True, reason=(
    "#1039 fix-round QA: two problems that share one ICD/SNOMED code but "
    "differ in others are merged, and the dropped one's other diagnosis "
    "leaves the list with no truncation marker. Remove this marker when "
    "only records with the same identity codes merge."))
@pytest.mark.parametrize("older,newer,must_show", [
    # ICD-10-CM multiple coding: a combined problem carries the CKD code
    # too, and a separate CKD problem is newer. The combined record is
    # dropped and diabetes goes with it. (On a live feed the combined
    # concept is E11.22 or SNOMED 771000119108, labelled when the
    # terminology resolver is on; 44054006 stands in with a static label.)
    ([{"system": SNOMED, "code": "44054006"},
      {"system": ICD10CM, "code": "N18.9"}],
     [{"system": ICD10CM, "code": "N18.9"}],
     "Type 2 diabetes mellitus"),
], ids=["combined-problem-plus-ckd"])
def test_a_shared_code_does_not_hide_another_diagnosis(
        chain, monkeypatch, older, newer, must_show):
    tenant = "ca-qa-1039-shared"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    _plant(chain, tenant, [_coded("older", older), _coded("newer", newer)])
    payload = _execute_tool(chain.hc, tenant, "search_records",
                            {"resource_type": "Condition"}, [])
    assert "QZXQA1039" not in payload
    conds = json.loads(payload)
    named = [i.get("name") for i in conds if i.get("type") == "Condition"]
    assert must_show in named, conds


def test_repeats_with_the_same_codes_still_merge(chain, monkeypatch):
    # Control for the fix: the visit-after-visit repeat the PR targets.
    tenant = "ca-qa-1039-repeat"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    same = [{"system": SNOMED, "code": "44054006"},
            {"system": ICD10CM, "code": "E11.9"}]
    _plant(chain, tenant, [_coded(f"r{i}", same) for i in range(4)])
    conds = json.loads(_execute_tool(chain.hc, tenant, "search_records",
                                     {"resource_type": "Condition"}, []))
    assert [i.get("type") for i in conds] == ["Condition"], conds


def test_a_tenant_past_one_page_still_reads_as_cut(chain, monkeypatch):
    """30 current conditions written first, then 200 resolved ones: the
    engine's page (newest 200) holds only the resolved. Nothing current is
    on the page, so the list must say it is longer than shown, not read
    as an empty problem list."""
    tenant = "ca-qa-1039-overpage"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    recs = [_cond(f"cur-{i}", str(500_000 + i)) for i in range(30)]
    recs += [_cond(f"old-{i}", str(600_000 + i), status="resolved")
             for i in range(200)]
    _plant(chain, tenant, recs)
    conds = json.loads(_execute_tool(chain.hc, tenant, "search_records",
                                     {"resource_type": "Condition"}, []))
    assert not [i for i in conds if i.get("type") == "Condition"], conds
    assert any(i.get("truncated") for i in conds), conds
