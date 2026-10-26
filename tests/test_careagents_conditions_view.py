"""The agent sees every current condition, once, and not the resolved ones.

Observed live: "I can only see 12 of 50 condition records". The default
view is now the whole active problem list (the engine's full page, not its
default 50), deduplicated by code, with resolved, inactive and
in-remission entries held back and counted. Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from careagents import conditions_view
from careagents.agent import (SUMMARY_LIMIT, _coding_key, _execute_tool,
                              _withdrawn)
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
CLINICAL = conditions_view.CLINICAL_SYSTEM


def _cond(rid, code, status="active", system=SNOMED, verification=None,
          label=None):
    res = {"resourceType": "Condition", "id": rid,
           "code": {"coding": [{"system": system, "code": code,
                                "display": label or f"Synthetic {code}"}]}}
    if status is not None:
        res["clinicalStatus"] = {"coding": [{"system": CLINICAL,
                                             "code": status}]}
    if verification:
        res["verificationStatus"] = {"coding": [{"code": verification}]}
    return res


def _bundle(*resources, total=None):
    b = {"resourceType": "Bundle", "type": "searchset",
         "total": len(resources) if total is None else total,
         "entry": [{"resource": r} for r in resources]}
    return b


class _FakeHC:
    """Records the params of each search; answers from per-type bundles."""

    def __init__(self, **bundles):
        self.bundles, self.calls = bundles, []

    def search(self, tenant, rt, params=None):
        self.calls.append((rt, params))
        return self.bundles.get(rt, _bundle())

    def record_count(self, tenant):
        return 0


def _view(*resources, total=None):
    return conditions_view.current_conditions(
        _bundle(*resources, total=total), _coding_key, _withdrawn)


def _ids(bundle):
    return [e["resource"]["id"] for e in bundle["entry"]]


# --- the filter ------------------------------------------------------------

@pytest.mark.parametrize("status", ["inactive", "remission", "resolved"])
def test_not_current_statuses_are_held_back_and_counted(status):
    out, held = _view(_cond("a", "1"), _cond("b", "2", status=status))
    assert _ids(out) == ["a"] and held == 1 and out["total"] == 1


@pytest.mark.parametrize("status", ["active", "recurrence", "relapse",
                                    "unknown", None])
def test_current_or_unstated_statuses_stay(status):
    out, held = _view(_cond("a", "1", status=status))
    assert _ids(out) == ["a"] and held == 0


def test_stu3_bare_status_and_systemless_coding_are_read():
    bare = _cond("a", "1", status=None)
    bare["clinicalStatus"] = "resolved"
    nosys = _cond("b", "2", status=None)
    nosys["clinicalStatus"] = {"coding": [{"code": "inactive"}]}
    other = _cond("c", "3", status=None)       # another system's "resolved"
    other["clinicalStatus"] = {"coding": [{"system": "urn:x",
                                           "code": "resolved"}]}
    out, held = _view(bare, nosys, other)
    assert _ids(out) == ["c"] and held == 2


def test_conflicting_status_codes_keep_the_record():
    res = _cond("a", "1")
    res["clinicalStatus"]["coding"].append({"system": CLINICAL,
                                            "code": "resolved"})
    assert _ids(_view(res)[0]) == ["a"]


@pytest.mark.parametrize("verification", ["entered-in-error", "refuted"])
def test_withdrawn_records_are_dropped_and_not_counted(verification):
    out, held = _view(_cond("a", "1", verification=verification),
                      _cond("b", "2", status="resolved",
                            verification=verification))
    assert _ids(out) == [] and held == 0 and out["total"] == 0


@pytest.mark.parametrize("bad", ["x", 7, None, [], {"coding": "x"},
                                 {"coding": [None, 3, {"code": 5}]}])
def test_malformed_status_never_raises_and_reads_as_unstated(bad):
    res = _cond("a", "1", status=None)
    res["clinicalStatus"] = bad
    out, held = _view(res)
    assert _ids(out) == ["a"] and held == 0


# --- dedupe ----------------------------------------------------------------

def test_repeats_of_one_code_collapse_to_the_newest():
    # The engine sorts newest first: the first record of a code is kept.
    out, held = _view(_cond("new", "38341003"), _cond("old", "38341003"),
                      _cond("other", "44054006"))
    assert _ids(out) == ["new", "other"] and out["total"] == 2


def test_a_shared_coding_makes_a_repeat_across_systems():
    both = _cond("a", "38341003")
    both["code"]["coding"].append({"system": ICD10, "code": "I10"})
    icd_only = _cond("b", "I10", system=ICD10)
    snomed_oid = _cond("c", "38341003", system="urn:oid:2.16.840.1.113883.6.96")
    assert _ids(_view(both, icd_only, snomed_oid)[0]) == ["a"]


def test_uncoded_records_are_never_merged():
    a, b = _cond("a", "1"), _cond("b", "1")
    a["code"] = {"text": "x"}
    b["code"] = {"coding": []}
    assert _ids(_view(a, b)[0]) == ["a", "b"]


def test_held_back_count_is_per_condition_not_per_record():
    out, held = _view(
        _cond("htn", "38341003"),
        _cond("htn-old", "38341003", status="resolved"),   # it came back
        _cond("flu-1", "6142004", status="resolved"),
        _cond("flu-2", "6142004", status="resolved"),      # same episode code
        _cond("sprain", "44465007", status="inactive"))
    assert _ids(out) == ["htn"] and held == 2


def test_a_resolved_record_does_not_hide_a_later_active_one():
    out, _ = _view(_cond("old", "1", status="resolved"), _cond("now", "1"))
    assert _ids(out) == ["now"]


# --- totals: a whole list reads whole, a cut list stays cut -----------------

def test_whole_page_is_not_marked_truncated():
    # Observed shape: 50 records, of which most are resolved or repeats.
    recs = [_cond(f"a{i}", str(i)) for i in range(12)]
    recs += [_cond(f"r{i}", str(100 + i), status="resolved")
             for i in range(20)]
    recs += [_cond(f"d{i}", str(i % 12)) for i in range(18)]
    hc = _FakeHC(Condition=_bundle(*recs))
    out = json.loads(_execute_tool(hc, "t", "get_health_summary", {}, []))
    conds = out["conditions"]
    assert not any(i.get("truncated") for i in conds)
    named = [i for i in conds if i.get("type") == "Condition"]
    assert len(named) == 12
    assert conds[-1]["not_current"] == 20
    assert "type" not in conds[-1] and "name" not in conds[-1]
    # Measured budget for the observed shape: smaller than the 50 rows the
    # model was handed before, not larger.
    assert len(json.dumps(conds)) < 1_500, len(json.dumps(conds))


def test_unseen_records_beyond_the_page_still_read_as_cut():
    hc = _FakeHC(Condition=_bundle(_cond("a", "1"), total=300))
    conds = json.loads(_execute_tool(hc, "t", "search_records",
                                     {"resource_type": "Condition"}, []))
    assert any(i.get("truncated") for i in conds)


def test_conditions_are_fetched_at_the_engine_ceiling():
    hc = _FakeHC()
    _execute_tool(hc, "t", "get_health_summary", {}, [])
    _execute_tool(hc, "t", "search_records", {"resource_type": "Condition"},
                  [])
    cond = [p for rt, p in hc.calls if rt == "Condition"]
    assert cond == [{"_count": 200}, {"_count": 200}]
    # Other types are untouched.
    assert all(p is None for rt, p in hc.calls if rt != "Condition")


def test_more_than_fifty_current_conditions_are_all_shown():
    recs = [_cond(f"a{i}", str(1000 + i)) for i in range(120)]
    hc = _FakeHC(Condition=_bundle(*recs))
    conds = json.loads(_execute_tool(hc, "t", "search_records",
                                     {"resource_type": "Condition"}, []))
    assert len(conds) == 120 > SUMMARY_LIMIT
    assert not any(i.get("truncated") for i in conds)


def test_no_note_when_nothing_was_held_back():
    hc = _FakeHC(Condition=_bundle(_cond("a", "1")))
    conds = json.loads(_execute_tool(hc, "t", "search_records",
                                     {"resource_type": "Condition"}, []))
    assert [i.get("type") for i in conds] == ["Condition"]


def test_other_record_types_are_not_filtered():
    med = {"resourceType": "MedicationStatement", "id": "m",
           "status": "completed",
           "medicationCodeableConcept": {"coding": [
               {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": "197361", "display": "Synthetic med"}]}}
    hc = _FakeHC(MedicationStatement=_bundle(med, med))
    out = json.loads(_execute_tool(hc, "t", "search_records",
                                   {"resource_type": "MedicationStatement"},
                                   []))
    assert len(out) == 2


# --- prompt budget -----------------------------------------------------------

def test_prompt_budget_for_a_full_page_of_conditions():
    """Measured: what the model is handed for the worst page the engine can
    return, 200 distinct active conditions, each with a 46-character label:
    16,200 characters, about 4k tokens at ~4 characters a token."""
    recs = [_cond(f"a{i}", str(10_000_000 + i),
                  label="Synthetic chronic condition of moderate length")
            for i in range(200)]
    hc = _FakeHC(Condition=_bundle(*recs))
    payload = _execute_tool(hc, "t", "search_records",
                            {"resource_type": "Condition"}, [])
    assert len(json.loads(payload)) == 200
    assert len(payload) < 17_000, len(payload)


# --- through the real engine -------------------------------------------------

def test_real_engine_serves_the_full_page_and_labels_by_code(
        cfg, svc, monkeypatch):  # noqa: F811
    """The real client onto the real engine: `_count=200` is accepted (60
    records, past the default page of 50), redaction still strips the
    upstream display, and names come from the terminology table by code."""
    from models import db
    from r6.models import R6Resource
    from tests.test_beta_acceptance_rows import Chain

    chain = Chain(cfg, svc, monkeypatch)
    tenant = "ca-conditions-view"
    monkeypatch.setenv("PUBLIC_TENANTS", f"test-tenant,{tenant}")
    canary = "QZXCOND Zelda Quornwhistle"
    recs = [_cond("htn", "38341003", label=canary),
            _cond("dm", "44054006", label=canary)]
    recs += [_cond(f"htn-{i}", "38341003", label=canary) for i in range(30)]
    recs += [_cond(f"old-{i}", str(900_000 + i), status="resolved",
                   label=canary) for i in range(28)]
    with chain.engine_app.app_context():
        for res in recs:
            db.session.add(R6Resource(res["resourceType"], json.dumps(res),
                                      resource_id=res["id"],
                                      tenant_id=tenant))
        db.session.commit()
    payload = _execute_tool(chain.hc, tenant, "search_records",
                            {"resource_type": "Condition"}, [])
    assert "QZXCOND" not in payload
    conds = json.loads(payload)
    names = sorted(i["name"] for i in conds if i.get("type") == "Condition")
    assert names == ["Hypertensive disorder", "Type 2 diabetes mellitus"]
    assert not any(i.get("truncated") for i in conds)
    assert conds[-1]["not_current"] == 28
