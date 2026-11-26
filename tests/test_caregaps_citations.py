"""Every care-gap rule cites its guideline, or says that it could not.

A citation is static data keyed by rule id (r6/caregaps/citations.py). It is
copied from the issuing body's own page and reviewed in the pull request that
adds it; nothing is fetched at run time and no statement is written by a
model. A rule whose statement could not be read on the issuing body's page
carries no citation and is named in `UNVERIFIED` with the reason, so "no
citation" is always a recorded decision and never an oversight.

`FIXTURES` is a second copy of each url, date, grade and statement. It has the
same author as the table, so it shows one thing only: a value edited in one
place and not the other fails here. A wrong quotation typed into both passes.
Whether the table matches the pages was checked by hand once; the
r6/caregaps/citations.py docstring records how, and that the CDC row was not
string-matched.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
from urllib.parse import urlsplit

from r6.brief.engine import build_care_gaps
from r6.caregaps import citations as citations_module
from r6.caregaps.citations import (
    CITATIONS, CLINICIAN_SIGNOFF, UNVERIFIED, citation_for)
from r6.caregaps.evaluate import CARE_GAP_RULES, evaluate_care_gaps
from r6.caregaps.report import build_consumer_summary
from r6.models import R6Resource, db
import pytest

pytestmark = pytest.mark.extension

RULE_IDS = {r["id"] for r in CARE_GAP_RULES}

#: The issuing bodies' own hosts, one per body the table cites today. A
#: citation anywhere else is a mirror.
ALLOWED_HOSTS = {"www.uspreventiveservicestaskforce.org", "www.cdc.gov"}

#: {rule_id: (url, date, [(grade, statement), ...])}: the second copy.
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

#: Our own population labels, in row order. They are not quotations; this
#: copy only makes a changed label a deliberate edit in two places.
POPULATIONS = {
    "bp-screening": ["Adults 18 years or older without known hypertension"],
    "colorectal-screening": ["Adults aged 50 to 75 years",
                             "Adults aged 45 to 49 years"],
    "cervical-screening": ["Women aged 21 to 29 years",
                           "Women aged 30 to 65 years"],
    "mammography": ["Women aged 40 to 74 years"],
    "flu-immunization": ["All persons aged ≥6 months"],
}


def test_every_rule_is_cited_or_named_as_unverified():
    """MUTATION: delete one entry from either table."""
    assert set(CITATIONS) | set(UNVERIFIED) == RULE_IDS
    assert not set(CITATIONS) & set(UNVERIFIED)
    assert all(isinstance(why, str) and why for why in UNVERIFIED.values())


def test_every_cited_rule_has_a_fixture():
    """MUTATION: add a `lipid-screening` entry to CITATIONS only."""
    assert set(CITATIONS) == set(FIXTURES)


def test_the_table_is_static_data_and_fetches_nothing():
    """MUTATION: add `import urllib.request` to r6/caregaps/citations.py."""
    tree = ast.parse(pathlib.Path(citations_module.__file__).read_text(
        encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "copy"}


@pytest.mark.parametrize("rule_id", sorted(FIXTURES))
def test_a_citation_is_complete_and_the_table_agrees_with_fixtures(rule_id):
    """MUTATION: edit one statement, date or url in the table only. The
    host, grade-set and word-count asserts are checked by editing BOTH
    copies (a mirror host, grade "C", a 26-word statement), since an edit to
    one copy stops at the equality first. MUTATION: reword one `population`
    label in the table only."""
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
    assert [r["population"]
            for r in cite["recommendations"]] == POPULATIONS[rule_id]
    for row in cite["recommendations"]:
        # `grade` is a key on every row, and None where the body assigns none.
        assert set(row) == {"population", "grade", "statement"}
        assert row["population"]
        assert row["grade"] in {"A", "B", None}
        # A citation, not a reproduction.
        assert len(row["statement"].split()) <= 25


def test_only_the_task_force_rows_carry_a_grade():
    """MUTATION: grade the influenza row "A" in the table and FIXTURES."""
    for rule_id, cite in CITATIONS.items():
        graded = [r["grade"] is not None for r in cite["recommendations"]]
        if "Task Force" in cite["issuing_body"]:
            assert all(graded), rule_id
        else:
            assert not any(graded), rule_id


def test_sign_off_is_pending_for_every_rule():
    """Nobody has signed the rule table yet, and this table must not say
    otherwise. Changing a value here is the record of a signature.
    MUTATION: set one rule to "signed"."""
    assert CLINICIAN_SIGNOFF == {rule_id: "pending" for rule_id in RULE_IDS}


def test_a_caller_cannot_edit_the_table_through_a_result():
    """MUTATION: return `cite` from citation_for without the deepcopy."""
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


def _key_names(obj):
    """Every dict key at any depth, lowercased, letters and digits only."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield re.sub(r"[^a-z0-9]", "", str(key).lower())
            yield from _key_names(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _key_names(value)


_SIGNOFF_SENTINEL = "zz-signoff-sentinel-7f3a"


@pytest.fixture
def signoff_sentinel(monkeypatch):
    """Every sign-off value becomes a string found nowhere else, so a
    response that carries one under ANY key name is caught by its value."""
    for rule_id in list(CLINICIAN_SIGNOFF):
        monkeypatch.setitem(CLINICIAN_SIGNOFF, rule_id, _SIGNOFF_SENTINEL)


def _assert_no_sign_off(payload):
    """Sign-off is a fact about the rule table. It is not sent anywhere:
    not under a key that names it, and not by value under any other key."""
    names = list(_key_names(payload))
    assert "citation" in names, "walked nothing"
    assert not [n for n in names if "signoff" in n]
    assert _SIGNOFF_SENTINEL not in json.dumps(payload)


def test_every_detail_row_carries_its_citation_or_none(signoff_sentinel):
    """MUTATION: send `citation: None` from the engine; or add
    `"clinicianSignoff": CLINICIAN_SIGNOFF[rule["id"]]` to `base`; or the
    same value under `"reviewed"`."""
    results = _results()
    rows = {r["rule_id"]: r for r in results}
    assert set(rows) == RULE_IDS
    for rule_id, row in rows.items():
        assert "citation" in row
        assert row["citation"] == CITATIONS.get(rule_id)
    _assert_no_sign_off(results)


def test_a_consumer_line_carries_the_citation_and_its_words_do_not_change(
        signoff_sentinel):
    """MUTATION: append the citation's statement to `line["message"]` in
    r6/caregaps/report.py; or copy the row's sign-off onto the line."""
    results = _results()
    bare = [{k: v for k, v in r.items() if k != "citation"} for r in results]
    lines = build_consumer_summary(results)["lines"]
    before = build_consumer_summary(bare)["lines"]
    assert lines and len(lines) == len(before)
    for line, old in zip(lines, before):
        assert line["citation"] == CITATIONS.get(line["rule_id"])
        # The message is the gap's existing wording, to the byte: a citation
        # sits beside it and is never folded into it.
        assert {k: v for k, v in line.items() if k != "citation"} == {
            k: v for k, v in old.items() if k != "citation"}
    _assert_no_sign_off(lines)


def test_the_appointment_brief_does_not_carry_the_citation():
    """The brief builds its care-gap fields from the same lines
    (r6/brief/routes.py) and picks title, message and rule id from each.
    MUTATION: append the line's citation statement to the field's value in
    r6/brief/engine.py::build_care_gaps."""
    consumer = build_consumer_summary(_results())
    section = build_care_gaps({"consumer": consumer})
    cited = {ln["rule_id"] for ln in consumer["lines"] if ln["citation"]}
    assert cited & {f.source_id for f in section.fields}
    sent = json.dumps(dataclasses.asdict(section))
    assert "citation" not in sent
    for cite in CITATIONS.values():
        assert cite["url"] not in sent
        for row in cite["recommendations"]:
            assert row["statement"] not in sent


def test_a_citation_never_carries_anything_from_the_record():
    """The table is static, so nothing a record holds can reach it.
    MUTATION: add `"for": patient.get("name")` to the row's citation."""
    payload = json.dumps([r["citation"] for r in _results()])
    assert _CANARY not in payload
    assert payload == json.dumps(
        [CITATIONS.get(r["id"]) for r in CARE_GAP_RULES])


def test_the_operation_returns_citations_in_both_payloads(
        client, app, tenant_id, tenant_headers, signoff_sentinel):
    """MUTATION: as for the detail rows, seen through the route."""
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
    # `detail` and `consumerSummary` are JSON inside a string, so the walk
    # runs on the parsed objects and the value check on the raw response.
    _assert_no_sign_off({"detail": detail, "lines": lines})
    assert _SIGNOFF_SENTINEL not in r.get_data(as_text=True)


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
    this pins the shape of the code. MUTATION: interpolate `c.url` bare;
    or drop `rel="noopener noreferrer"`."""
    src, fn = _cite_fn()
    for field in ("c.url", "c.issuing_body", "c.date", "row.statement",
                  "row.population", "row.grade"):
        assert f"esc({field})" in fn, field
    # Every interpolation in the function goes through esc() or is markup
    # the function built itself.
    for expr in re.findall(r"\$\{([^}]*)\}", fn):
        assert expr.strip().startswith(("esc(", "row.grade ?", "rows")), expr
    assert 'rel="noopener noreferrer"' in fn


def test_the_page_draws_a_citation_only_on_a_rule_that_applies():
    """A guideline for women aged 40 to 74 is not drawn on the card that
    says the rule does not apply to this person. MUTATION: call
    `cite(r.citation)` without the status test."""
    src, _ = _cite_fn()
    calls = re.findall(r"\$\{([^}]*cite\(r\.citation\)[^}]*)\}", src)
    assert len(calls) == 1
    assert re.sub(r"\s+", " ", calls[0]).strip() == (
        "r.status === 'not_applicable' ? '' : cite(r.citation)")


def test_the_page_reads_the_citation_fields_and_nothing_else():
    """An allowlist, not a search for the word: a sign-off read as
    `c.review_status` has no "signoff" in it. MUTATION: draw
    `${esc(c.review_status)}` in cite()."""
    src, fn = _cite_fn()
    assert set(re.findall(r"\bc\.(\w+)", fn)) == {
        "url", "issuing_body", "date", "recommendations"}
    assert set(re.findall(r"\brow\.(\w+)", fn)) == {
        "population", "statement", "grade"}
    assert "signoff" not in src.lower() and "sign-off" not in src.lower()
