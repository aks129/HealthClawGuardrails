"""Guards for the Care Gaps MCP App.

Verified live 2026-08-31 (#538): a tenant holding more than one Patient gets
`unevaluated: "ambiguous-patient"` from $care-gaps and a note saying nothing
was examined. The page threw the note away and drew a large "0 Due" tile
over six cards reading "date of birth unknown" — every one of them an
artefact of the call, not a fact about the person (see the docstring on
r6/caregaps/report.py `_unevaluated_marker`). The cause was a render that
showed the note only inside a box gated on `consumer.lines`, which is empty
on exactly this path.

What these prove, and what they do not. CI has no browser for the Python
job, so, as in tests/test_mcp_app_lab_trends.py, they assert the route
contract and the SHAPE of the source: the caller-reason family is injected
from the engine rather than copied by hand, the placeholder branch exists,
the 401 copy no longer names the write gate, and the exact shape of the
defect — a ternary on `lines` wrapping the note — is gone. They do not prove
a browser paints any of it. That is e2e/tests/care-gaps-app.spec.ts, which
walks the ambiguous-patient path against the seeded demo tenant.
"""
from __future__ import annotations

import json
import pathlib
import re

from r6.caregaps.report import caller_reasons

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "mcp_apps" / "care_gaps.html")

#: The three reasons that mean the rules never read a record. Spelled out
#: here on purpose: a change to the engine's set should read as a change,
#: not vanish into `caller_reasons()` agreeing with itself.
CALLER_REASONS = ["ambiguous-patient", "check-incomplete", "no-patient"]


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _code_only() -> str:
    """The template with comments stripped — comments here quote the wording
    they forbid, and a guard that reads its own documentation as evidence is
    not a guard (the trap tests/test_mcp_app_lab_trends.py records)."""
    src = re.sub(r"<!--.*?-->", "", _source(), flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def _render_body(code: str) -> str:
    """The body of the page's `render()` function, up to `load()`."""
    match = re.search(r"function render\(.*?\n  }\n", code, flags=re.S)
    assert match, "render() not found in the template"
    return match.group(0)


# --- the route injects the caller-reason family --------------------------

def test_the_route_renders_and_injects_the_caller_reasons(client):
    """MUTATION: drop `not_evaluated_reasons` from render_template -> red.

    The page branches on which FAMILY `unevaluated` belongs to, and the
    families are the engine's. The rendered HTML must carry the engine's
    caller-reason set as a JSON array, so the page has one source of truth.
    """
    r = client.get("/r6/fhir/mcp-apps/care-gaps")
    assert r.status_code == 200
    assert r.headers["X-MCP-App"] == "care-gaps"
    body = r.get_data(as_text=True)
    match = re.search(r"const NOT_EVALUATED = (\[[^\]]*\]);", body)
    assert match, "the page does not declare NOT_EVALUATED as a JSON array"
    assert json.loads(match.group(1)) == CALLER_REASONS
    assert json.loads(match.group(1)) == caller_reasons()


def test_the_caller_reasons_are_not_copied_into_the_template_by_hand():
    """MUTATION: write the three strings into the template -> red.

    A second copy drifts: the engine adds a reason and the page keeps
    drawing "0 Due" for it. The strings must reach the page through the
    route (above) and appear nowhere in the template file itself.
    """
    src = _source()
    for reason in CALLER_REASONS:
        assert reason not in src, f"{reason!r} is hand-copied into the template"
    assert "not_evaluated_reasons | tojson" in src


# --- the note is never gated on the plain-terms list ----------------------

def test_the_note_is_not_gated_on_the_plain_terms_list():
    """MUTATION: restore `const consumerBox = lines ? ... unevaluated_note
    ... : ''` -> red.

    Proven here: the exact shape of #538 is absent. No ternary in render()
    branches on `lines`, and the note is read from the consumer summary
    before the box is assembled, so it cannot be the box's own conditional
    content. NOT proven here: that the note reaches the screen — the e2e
    spec asserts that in a browser.
    """
    body = _render_body(_code_only())
    assert "consumer.unevaluated_note" in body
    assert re.search(r"\blines\s*\?", body) is None, (
        "render() still branches on `lines` with a ternary — the shape of #538")
    assert body.index("consumer.unevaluated_note") < body.index("consumerBox"), (
        "the note must be read before the consumer box is built, not inside it")


def test_the_caller_reason_path_draws_placeholders_not_counts():
    """MUTATION: interpolate `summary.due || 0` on the caller path -> red.

    On a caller reason every count is zero because nothing was read, and a
    "0 Due" tile is the false statement the engine's docstring warns about.
    The tiles must carry a placeholder, labelled for assistive tech, and the
    cards must give way to a single line saying so.
    """
    body = _render_body(_code_only())
    assert "NOT_EVALUATED.includes(" in body
    assert "'—'" in body, "no placeholder branch for the stat tiles"
    assert 'aria-label="not evaluated"' in body
    assert 'title="not evaluated"' in body
    assert "Not evaluated" in body, "the caller-reason heading is missing"
    assert "No screenings were evaluated." in body


def test_every_interpolated_string_is_still_escaped():
    """The note and the heading are new interpolations; they go through
    `esc()` like everything else on the page. MUTATION: interpolate
    `${consumer.unevaluated_note}` bare -> red."""
    body = _render_body(_code_only())
    assert "${consumer.unevaluated_note}" not in body
    assert "esc(consumer.unevaluated_note)" in body
    assert "${consumer.note}" not in body
    assert "esc(consumer.note)" in body
    assert "esc(n || 0)" in body  # the tile counts (engine ints, still escaped)


# --- #535, the client half --------------------------------------------------

def test_the_401_message_does_not_name_the_write_gate():
    """MUTATION: restore "this tenant requires a step-up token" -> red.

    Step-up is the WRITE gate. This is a read, and the common cause of a
    401 here is a misspelled tenant. The server's answer stays the same for
    a missing tenant and an unauthorised one (existence disclosure, P5), so
    the client copy is what can help.
    """
    code = _code_only()
    assert "requires a step-up token" not in code
    assert "step-up" not in code.lower()
    assert "check the tenant id is spelled correctly" in code
    assert "or this tenant needs a credential" in code


def test_enter_in_the_tenant_input_submits():
    """MUTATION: drop the keydown listener -> red. The sibling pages wire
    Enter; a tenant box that needs a mouse click reads as a broken page."""
    code = _code_only()
    assert re.search(
        r"getElementById\('tenant-input'\)\.addEventListener\('keydown'", code), (
        "no keydown listener on #tenant-input")
    assert "e.key === 'Enter'" in code


def test_the_footer_disclaimer_is_still_there():
    """The page keeps its footer disclaimer whatever the render path."""
    src = _source()
    assert "not a diagnosis or a directive" in src
