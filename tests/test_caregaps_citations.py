"""Every care-gap rule cites its guideline, or says that it could not.

A citation is static data keyed by rule id (r6/caregaps/citations.py). It is
copied from the issuing body's own page and reviewed in the pull request that
adds it; nothing here is fetched at run time and nothing is written by a
model. A rule whose statement could not be read on the issuing body's page
carries no citation and is named in `UNVERIFIED` with the reason, so "no
citation" is always a recorded decision and never an oversight.

`FIXTURES` is the second copy on purpose. It was typed from the pages
separately from the table, so a statement edited in one place and not the
other fails here rather than reaching a person as a quotation nobody said.
"""

from __future__ import annotations

import json
import pathlib
import re
from urllib.parse import urlsplit

from r6.caregaps.citations import (
    CITATIONS, CLINICIAN_SIGNOFF, UNVERIFIED, citation_for)
from r6.caregaps.evaluate import CARE_GAP_RULES, evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from r6.models import R6Resource, db
import pytest

pytestmark = pytest.mark.extension

RULE_IDS = {r["id"] for r in CARE_GAP_RULES}

#: The issuing bodies' own hosts. A citation anywhere else is a mirror.
ALLOWED_HOSTS = {"www.uspreventiveservicestaskforce.org", "www.cdc.gov",
                 "diabetesjournals.org"}

#: {rule_id: (url, date, [(grade, statement), ...])} as read on the page.
_USPSTF = "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
FIXTURES = {
    "bp-screening": (_USPSTF + "hypertension-in-adults-screening", "2021-04-27", [
        ("A", "The USPSTF recommends screening for hypertension in adults 18 "
              "years or older with office blood pressure measurement (OBPM)."),
    ]),
    "colorectal-screening": (_USPSTF + "colorectal-cancer-screening", "2021-05-18", [
        ("A", "The USPSTF recommends screening for colorectal cancer in all "
              "adults aged 50 to 75 years."),
        ("B", "The USPSTF recommends screening for colorectal cancer in "
              "adults aged 45 to 49 years."),
    ]),
    "cervical-screening": (_USPSTF + "cervical-cancer-screening", "2018-08-21", [
        ("A", "The USPSTF recommends screening for cervical cancer every 3 "
              "years with cervical cytology alone in women aged 21 to 29 "
              "years."),
        ("A", "Screen for cervical cancer every 3 years with cytology alone, "
              "every 5 years with hrHPV testing alone, or every 5 years with "
              "cotesting."),
    ]),
    "mammography": (_USPSTF + "breast-cancer-screening", "2024-04-30", [
        ("B", "The USPSTF recommends biennial screening mammography for "
              "women aged 40 to 74 years."),
    ]),
    "flu-immunization": (
        "https://www.cdc.gov/mmwr/volumes/74/wr/mm7432a2.htm", "2025-08-28", [
            (None, "Routine annual influenza vaccination is recommended for "
                   "all persons aged ≥6 months who do not have a "
                   "contraindication to vaccination."),
        ]),
}


def test_every_rule_is_cited_or_named_as_unverified():
    """MUTATION: delete one entry from either table."""
    assert set(CITATIONS) | set(UNVERIFIED) == RULE_IDS
    assert not set(CITATIONS) & set(UNVERIFIED)
    assert all(isinstance(why, str) and why for why in UNVERIFIED.values())


def test_the_cited_rules_are_the_ones_read_on_the_page():
    """A citation added without a fixture has not been read twice."""
    assert set(CITATIONS) == set(FIXTURES)


@pytest.mark.parametrize("rule_id", sorted(FIXTURES))
def test_a_citation_is_complete_and_matches_the_page(rule_id):
    cite = CITATIONS[rule_id]
    url, when, rows = FIXTURES[rule_id]
    assert set(cite) == {"issuing_body", "title", "url", "date",
                         "recommendations"}
    assert cite["issuing_body"] and cite["title"]
    assert cite["url"] == url and cite["date"] == when
    parts = urlsplit(cite["url"])
    assert parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cite["date"])
    assert [(r["grade"], r["statement"])
            for r in cite["recommendations"]] == rows
    for row in cite["recommendations"]:
        # `grade` is a key on every row, and None where the body assigns none.
        assert set(row) == {"population", "grade", "statement"}
        assert row["population"]
        assert row["grade"] in {"A", "B", None}
        # A citation, not a reproduction.
        assert len(row["statement"].split()) <= 25


def test_only_the_task_force_rows_carry_a_grade():
    for rule_id, cite in CITATIONS.items():
        graded = [r["grade"] is not None for r in cite["recommendations"]]
        if "Task Force" in cite["issuing_body"]:
            assert all(graded), rule_id
        else:
            assert not any(graded), rule_id


def test_sign_off_is_pending_for_every_rule():
    """Nobody has signed the rule table yet, and this table must not say
    otherwise. Changing a value here is the record of a signature."""
    assert CLINICIAN_SIGNOFF == {rule_id: "pending" for rule_id in RULE_IDS}


def test_a_caller_cannot_edit_the_table_through_a_result():
    citation_for("mammography")["recommendations"][0]["statement"] = "x"
    assert CITATIONS["mammography"]["recommendations"][0]["statement"] != "x"
    assert citation_for("lipid-screening") is None
    assert citation_for("no-such-rule") is None


# --- the agent output and the page payload -------------------------------

_PATIENT = {"resourceType": "Patient", "id": "p1", "gender": "female",
            "birthDate": "1968-05-01"}
_CANARY = "Zzcanary Quillfeather"


def _results():
    conditions = [{"resourceType": "Condition", "code": {
        "text": _CANARY, "coding": [{"code": "E11.9", "display": _CANARY}]}}]
    return evaluate_care_gaps({**_PATIENT, "name": [{"family": _CANARY}]},
                              conditions=conditions, as_of="2026-06-01")


def test_every_detail_row_carries_its_citation_or_none():
    rows = {r["rule_id"]: r for r in _results()}
    assert set(rows) == RULE_IDS
    for rule_id, row in rows.items():
        assert "citation" in row
        assert row["citation"] == CITATIONS.get(rule_id)
        # Sign-off is a fact about the rule table. It is not sent anywhere.
        assert "clinician_signoff" not in row


def test_a_consumer_line_carries_the_citation_and_its_words_do_not_change():
    results = _results()
    bare = [{k: v for k, v in r.items() if k != "citation"} for r in results]
    lines = build_consumer_summary(results)["lines"]
    before = build_consumer_summary(bare)["lines"]
    assert lines and len(lines) == len(before)
    for line, old in zip(lines, before):
        assert line["citation"] == CITATIONS.get(line["rule_id"])
        assert "clinician_signoff" not in line
        # The message is the gap's existing wording, to the byte: a citation
        # sits beside it and is never folded into it.
        assert {k: v for k, v in line.items() if k != "citation"} == {
            k: v for k, v in old.items() if k != "citation"}


def test_a_citation_never_carries_anything_from_the_record():
    """The table is static, so nothing a record holds can reach it."""
    payload = json.dumps([r["citation"] for r in _results()])
    assert _CANARY not in payload
    assert payload == json.dumps(
        [CITATIONS.get(r["id"]) for r in CARE_GAP_RULES])


def test_the_operation_returns_citations_in_both_payloads(
        client, app, tenant_id, tenant_headers):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Patient", resource_json=json.dumps(_PATIENT),
            resource_id="p1", tenant_id=tenant_id))
        db.session.commit()
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    params = {p["name"]: p.get("valueString")
              for p in r.get_json()["parameter"]}
    detail = json.loads(params["detail"])
    assert {d["rule_id"]: d["citation"] for d in detail} == {
        rule_id: CITATIONS.get(rule_id) for rule_id in RULE_IDS}
    lines = json.loads(params["consumerSummary"])["lines"]
    cited = [ln for ln in lines if ln["citation"]]
    assert cited
    for line in cited:
        assert line["citation"]["url"].startswith("https://")
    assert "pending" not in params["detail"] + params["consumerSummary"]


# --- the page ------------------------------------------------------------

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "mcp_apps" / "care_gaps.html")


def _cite_fn():
    src = re.sub(r"<!--.*?-->", "", TEMPLATE.read_text(encoding="utf-8"),
                 flags=re.S)
    src = re.sub(r"(?<!:)//[^\n]*", "", src)
    match = re.search(r"function cite\(.*?\n  }\n", src, flags=re.S)
    assert match, "cite() not found in the template"
    return src, match.group(0)


def test_the_page_draws_the_citation_escaped_and_attributed():
    """CI has no browser for this job, so, as in test_mcp_app_care_gaps.py,
    this pins the shape of the code. MUTATION: interpolate `c.url` bare."""
    src, fn = _cite_fn()
    for field in ("c.url", "c.issuing_body", "c.date", "row.statement",
                  "row.population", "row.grade"):
        assert f"esc({field})" in fn, field
    # Every interpolation in the function goes through esc() or is markup
    # the function built itself.
    for expr in re.findall(r"\$\{([^}]*)\}", fn):
        assert expr.strip().startswith(("esc(", "row.grade ?", "rows")), expr
    assert 'rel="noopener noreferrer"' in fn
    assert "cite(r.citation)" in src


def test_the_page_says_nothing_about_sign_off():
    src, _ = _cite_fn()
    assert "signoff" not in src.lower() and "sign-off" not in src.lower()
