"""Round 3 security exploits for PR #945 (head f40229d): the narrowed
allergy-status rule (SNOMED 1631000175102 only, every coding) and the
"records don't say" decision taken after the bundle total. Synthetic only."""

from __future__ import annotations

import json

import pytest

from careagents.agent import _execute_tool, _summarize_bundle
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_sec_945_first_answer import CANARY, SNOMED, _plant

NA = "1631000175102"
SILENT = "don't say whether"


def _a(*codings, **extra):
    return {"resourceType": "AllergyIntolerance",
            "code": {"coding": list(codings)}, **extra}


def _na(**kw):
    return _a({"system": SNOMED, "code": NA, **kw})


def _sum(*resources, total=None):
    b = {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}
    if total is not None:
        b["total"] = total
    return _summarize_bundle(b)


@pytest.mark.parametrize("system", [
    "https://snomed.info/sct", "http://snomed.info/sct/", "HTTP://SNOMED.INFO/SCT",
    "http://snomed.info/sct/731000124108/version/20250301"])
def test_r3_near_miss_systems_are_allergy_records_not_status(system):
    [item] = _sum(_a({"system": system, "code": NA}))
    assert not item.get("not_an_allergy")
    assert SILENT not in json.dumps(item)


@pytest.mark.parametrize("code", ["1631000175102x", "1631000175102 x"])
def test_r3_near_miss_codes_are_allergy_records_not_status(code):
    [item] = _sum(_a({"system": SNOMED, "code": code}))
    assert not item.get("not_an_allergy")


#: The spellings the engine itself keys as SNOMED 1631000175102 before it
#: labels them "Patient not asked" (r6.terminology: stripped, OID read as
#: SNOMED, int code as its digits). Moved here from the two near-miss lists
#: above by #949 security F1: read as allergy records, their label became
#: an allergy's name.
@pytest.mark.parametrize("system,code", [
    ("urn:oid:2.16.840.1.113883.6.96", NA), (" http://snomed.info/sct", NA),
    (SNOMED + " ", NA), (SNOMED, NA + " "), (SNOMED, " " + NA),
    (SNOMED, int(NA))])
def test_r3_engine_spellings_of_status_are_status(system, code):
    [item] = _sum(_a({"system": system, "code": code}))
    assert item.get("not_an_allergy")


@pytest.mark.parametrize("other", ["penicillin", None, [], 7,
                                   {"code": NA}, {"system": SNOMED}])
def test_r3_any_odd_element_beside_status_keeps_an_allergy_record(other):
    [item] = _sum(_a({"system": SNOMED, "code": NA}, other))
    assert not item.get("not_an_allergy"), item


def test_r3_status_coding_display_and_extras_never_surface():
    [item] = _sum(_na(display=f"{CANARY} Zelda", version="x", userSelected=True))
    assert item.get("not_an_allergy")
    assert CANARY not in json.dumps(item)


def test_r3_a_full_status_only_list_says_silent_once_known_complete():
    out = _sum(*[_na()] * 12, total=12)
    assert not any(i.get("truncated") for i in out)
    assert all(SILENT in json.dumps(i) for i in out)


@pytest.mark.parametrize("total", [13, 50, 10_000])
def test_r3_a_stated_total_past_the_page_suppresses_silent(total):
    out = _sum(*[_na()] * 12, total=total)
    assert any(i.get("truncated") for i in out)
    assert not any(SILENT in json.dumps(i) for i in out)


@pytest.mark.parametrize("total", ["13", True, -1, None, 1.5e3])
def test_r3_a_malformed_total_never_makes_a_short_list_read_complete(total):
    # Only entries decide; 13 entries is cut regardless of what total says.
    out = _sum(*[_na()] * 13, total=total)
    assert not any(SILENT in json.dumps(i) for i in out)


def test_r3_an_operation_outcome_never_counts_as_an_allergy_or_a_cut():
    b = {"resourceType": "Bundle", "entry": [
        {"resource": _na()},
        {"resource": {"resourceType": "OperationOutcome", "issue": [
            {"diagnostics": f"{CANARY} diag"}]}}]}
    out = _summarize_bundle(b)
    assert CANARY not in json.dumps(out)
    assert not any(i.get("truncated") for i in out)


def test_r3_real_engine_page_cut_suppresses_silent(cfg, svc, monkeypatch):  # noqa: F811
    """60 'Patient not asked' records then penicillin, on the real engine:
    the page is 50, so penicillin may be past it. Silent must not appear."""
    chain = Chain(cfg, svc, monkeypatch)
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT,
                                   resource_type="AllergyIntolerance").delete()
        db.session.commit()
    _plant(chain, *[dict(_na(), id=f"r3na{i:02d}") for i in range(60)],
           dict(_a({"system": SNOMED, "code": "91936005"}), id="r3zpen"))
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                   {}, []))
    allergies = out["allergies"]
    assert any(i.get("truncated") for i in allergies)
    assert SILENT not in json.dumps(out)


def test_r3_real_engine_status_only_says_silent_and_nothing_else(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    from models import db
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        R6Resource.query.filter_by(tenant_id=TENANT,
                                   resource_type="AllergyIntolerance").delete()
        db.session.commit()
    _plant(chain, dict(_na(display=f"{CANARY} label"), id="r3only"))
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_health_summary",
                                   {}, []))
    [item] = out["allergies"]
    assert item.get("not_an_allergy") and SILENT in item["statement"]
    blob = json.dumps(out).lower()
    assert CANARY.lower() not in blob and "no known" not in blob
