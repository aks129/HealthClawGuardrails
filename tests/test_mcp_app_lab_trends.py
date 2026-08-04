"""Guards for the Lab Trends MCP App.

Reported live 2026-08-04: "give me a timeline of my cholesterol results" made
eight tool calls and then died. Prose is the wrong shape for that question —
four numbers across four dates is a picture — so the answer is a view, not a
better sentence.

These assert the route contract and the source of the view's numbers. What
they cannot assert is a rendered chart: the SVG is built in the browser, and
CI has no browser here. That limit is stated rather than papered over, and the
things most likely to go wrong (wrong data path, a second copy of the
reference ranges, absence claimed from a connected-records read) are all
checkable in the source and are checked below.
"""
from __future__ import annotations

import pathlib
import re

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "mcp_apps" / "lab_trends.html")


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _code_only() -> str:
    """The template with comments stripped.

    Stripping matters, and this file learned it the hard way twice over: the
    comments here deliberately QUOTE the wording and thresholds they forbid,
    to explain why, so a naive search finds the quote and reports the defect
    as present. A guard that reads its own documentation as evidence is not a
    guard — the same trap that hit tests/test_fasten_dialog_reachable.py.
    """
    src = re.sub(r"<!--.*?-->", "", _source(), flags=re.S)
    # Line comments, without eating the // in an http:// URL.
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def test_the_route_renders_and_declares_itself_an_mcp_app(client):
    r = client.get("/r6/fhir/mcp-apps/lab-trends")
    assert r.status_code == 200
    assert r.headers["X-MCP-App"] == "lab-trends"
    assert "profile=mcp-app" in r.headers["Content-Type"]


def test_both_trailing_slash_forms_work(client):
    """MCP clients build this URI by string concatenation; a 404 on one form
    is a broken embed that looks like a broken product."""
    assert client.get("/r6/fhir/mcp-apps/lab-trends").status_code == 200
    assert client.get("/r6/fhir/mcp-apps/lab-trends/").status_code == 200


def test_the_tenant_arrives_from_the_query_and_is_escaped(client):
    """The tenant reaches the page as a value, never as markup.

    MUTATION: render tenant_id with |safe -> red.
    """
    r = client.get("/r6/fhir/mcp-apps/lab-trends?tenant_id=%22%3E%3Cscript%3Ex")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "<script>x" not in body
    assert "&#34;&gt;&lt;script&gt;x" in body or "&quot;&gt;&lt;script&gt;x" in body


def test_the_page_renders_without_a_tenant_rather_than_erroring(client):
    """The HTML is public; the DATA behind it is not. Rendering a shell with
    an empty tenant box is correct — every number on the page still comes
    from a tenant-authenticated fetch the browser makes afterwards."""
    r = client.get("/r6/fhir/mcp-apps/lab-trends")
    assert r.status_code == 200
    assert "tenant id" in r.get_data(as_text=True)


# --- where the numbers come from -----------------------------------------

def test_the_view_reads_the_engines_own_interpret_operation():
    """MUTATION: point the fetch at a raw Observation search -> red.

    Going through $interpret is what makes redaction, audit and tenant
    scoping apply by construction, and what makes the flags below the
    ENGINE's verdict rather than a browser's guess.
    """
    src = _source()
    assert "/r6/fhir/Observation/$interpret" in src
    assert "X-Tenant-Id" in src


def test_the_reference_ranges_are_not_reimplemented_in_the_browser():
    """MUTATION: add a `if (value > 200)` style threshold -> red.

    r6/labs/interpret.py is the single home of reference ranges. A second
    copy in a view drifts silently and would show a patient a different
    verdict from the one the engine audited.
    """
    src = _code_only()
    # No numeric clinical thresholds: the view reads `interpretation`, which
    # the engine already computed.
    assert "interpretation" in src
    for banned in ("> 200", ">200", "< 40", "<40", "5.7", "129", "239"):
        assert banned not in src, (
            f"the view appears to carry its own threshold ({banned!r}); "
            "reference ranges belong to r6/labs/interpret.py alone")


def test_an_analyte_is_a_set_of_codes_not_a_single_code():
    """The same test arrives as different LOINCs from different labs.
    Plotting one of them draws a confident line through part of the data —
    the label-table failure (#343) one level up.

    MUTATION: collapse LDL to a single code -> red.
    """
    src = _source()
    match = re.search(r'name:\s*"LDL cholesterol",\s*codes:\s*\[([^\]]*)\]', src)
    assert match, "LDL panel not found"
    assert len([c for c in match.group(1).split(",") if c.strip()]) >= 2


def test_a_single_reading_is_not_drawn_as_a_trend():
    """MUTATION: plot a line through one point -> red. One point has no
    direction, and drawing one invents a story the record does not support."""
    src = _source()
    assert "dated.length < 2" in src
    assert "not enough for a trend line" in src


def test_an_empty_result_is_not_reported_as_absence():
    """SAFETY_CORE: this reads the CONNECTED record, which is not the same as
    the person's history. MUTATION: say 'you have no cholesterol results'
    -> red."""
    assert "not the same as never having had one" in _source()
    assert "you have no" not in _code_only().lower()


def test_the_engines_disclaimer_is_surfaced_not_dropped():
    """$interpret returns a disclaimer for a reason; a view that silently
    drops it presents decision support as fact."""
    src = _source()
    assert "disclaimer" in src


def test_readings_without_a_number_are_skipped_not_zeroed():
    """MUTATION: default a missing valueQuantity to 0 -> red. A zero plotted
    on a cholesterol chart is a clinical claim nobody made."""
    src = _source()
    assert 'typeof vq.value !== "number"' in src
