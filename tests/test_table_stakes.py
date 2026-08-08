"""The table-stakes checker has to catch what it claims and stay quiet otherwise.

Two failure modes, and the second is the one that kills linters: a check that
never fires is decoration, and a check that fires on ordinary writing gets
switched off within a week. Both are tested here.

Constitution §1: one control, one property — say which property, then ask what
else could make it pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_table_stakes import (  # noqa: E402
    check_prose, check_style,
)


def _prose(text: str):
    return check_prose("docs/x.md", [(1, text)])


def _style(text: str, whole: str = ""):
    return check_style("a.css", [(1, text)], whole or text)


def _rules(findings):
    return {f.rule for f in findings}


# --- writing: must fire ----------------------------------------------------

@pytest.mark.parametrize("text,rule", [
    ("This provides a seamless experience.", "no-marketing-adjectives"),
    ("A robust pipeline handles it.", "no-marketing-adjectives"),
    ("Our cutting-edge engine is fast.", "no-marketing-adjectives"),
    ("This may potentially help you.", "one-helper-verb"),
    ("It could potentially fail here.", "one-helper-verb"),
    ("Please reach out if it breaks.", "no-chatty-phrasal-verbs"),
    ("Let us circle back on that.", "no-chatty-phrasal-verbs"),
])
def test_prose_rules_fire(text, rule):
    assert rule in _rules(_prose(text)), f"{rule} missed: {text}"


def test_a_long_sentence_is_flagged():
    text = ("The system will " + "process every single record carefully "
            * 6 + "today.")
    assert "sentence-length" in _rules(_prose(text))


# --- writing: must NOT fire ------------------------------------------------

@pytest.mark.parametrize("text", [
    "Redact the record before the model sees it.",
    "The tenant comes from the header, never the body.",
    "Writes need a step-up token.",
    "",
    "# A heading that happens to be quite long but is still just a heading",
    "> Quoted text from somewhere else; not our prose.",
    "| col | col |",
    "```",
    "    indented_code(); with_semicolon();",
    "Run `init(); serve()` before the first request.",
    "Use the [guide](https://example.com/a;b) for detail.",
    "The tenant is resolved; the record is then read.",
])
def test_ordinary_writing_is_left_alone(text):
    assert not _prose(text), f"false positive on: {text!r}"


def test_a_sentence_at_the_cap_passes():
    assert not _rules(_prose(" ".join(["word"] * 25) + "."))


def test_code_spans_do_not_trigger_marketing_words():
    # A variable genuinely named `robust_mode` is not marketing copy.
    assert not _prose("Set `robust_mode` to true.")


# --- design: must fire -----------------------------------------------------

def test_banned_primary_font_is_flagged():
    assert "banned-primary-font" in _rules(
        _style('  font-family: "Inter", sans-serif;'))


def test_cdn_asset_is_flagged_because_the_csp_blocks_it():
    assert "csp-external-asset" in _rules(
        _style('<script src="https://cdn.example.com/x.js"></script>'))


def test_small_font_on_a_form_control_is_flagged_for_ios_zoom():
    """Updated with the rule. This previously asserted that ANY font-size
    under 16px was flagged, which is not what iOS does and not what the rule
    or its message claimed: Safari zooms when a FORM CONTROL under 16px is
    focused. Flagging every declaration banned each new caption and axis
    label, while the existing stylesheet is full of 13-14px body copy.
    """
    css = "input.tenant {\n  font-size: 14px;\n}"
    assert "ios-zoom-font-size" in _rules(
        check_style("a.css", [(2, "  font-size: 14px;")], css))


def test_the_selector_is_found_across_a_multi_line_selector_list():
    css = "textarea,\nselect {\n  font-size: 13px;\n}"
    assert "ios-zoom-font-size" in _rules(
        check_style("a.css", [(3, "  font-size: 13px;")], css))


def test_an_inline_style_on_an_input_is_flagged():
    line = '<input class="x" style="font-size:13px">'
    assert "ios-zoom-font-size" in _rules(_style(line))


def test_small_font_on_static_text_is_not_flagged():
    """MUTATION: drop the selector check -> red.

    iOS does not zoom for static text. A rule that says otherwise makes every
    caption, axis label and helper line a CI failure — which is how a guard
    stops being read as signal.
    """
    css = ".spark-axis {\n  font-size: 10px;\n}"
    assert "ios-zoom-font-size" not in _rules(
        check_style("a.css", [(2, "  font-size: 10px;")], css))


def test_a_16px_control_is_fine():
    css = "input {\n  font-size: 16px;\n}"
    assert "ios-zoom-font-size" not in _rules(
        check_style("a.css", [(2, "  font-size: 16px;")], css))


def test_motion_without_the_media_query_is_flagged():
    assert "reduced-motion" in _rules(
        _style("  transition: opacity 150ms ease-out;"))


# --- design: must NOT fire -------------------------------------------------

def test_apple_system_in_a_fallback_chain_is_fine():
    # design.md is explicit: -apple-system in the FALLBACK is a performance
    # decision, and our primary faces are distinctive.
    assert not _rules(_style('  font-family: "Public Sans", '
                             '-apple-system, sans-serif;'))


def test_a_host_the_csp_permits_is_not_flagged():
    """The rule tracks app.py's policy instead of carrying its own list.

    This test used to be `test_google_fonts_is_allowed_because_the_csp_allows_it`
    and asserted that fonts.googleapis.com passes. It no longer does, because
    the policy no longer allows it and the faces are vendored under
    static/fonts/. That is the point: the pin moved because the thing it pins
    moved, in the same change.

    Written against a host the policy names right now, resolved at run time, so
    it cannot rot into asserting a host nobody allows any more.
    """
    from check_table_stakes import csp_allowed_hosts

    allowed = csp_allowed_hosts()
    if not allowed:
        pytest.skip("policy permits no asset hosts — nothing to assert here")
    host = sorted(allowed)[0].lstrip("*.")
    assert not _rules(_style(f'<link href="https://{host}/x.css" rel="stylesheet">'))


def test_a_host_the_csp_does_not_permit_is_flagged():
    """MUTATION: widen style-src in app.py and this stops firing.

    The companion above can pass vacuously when the policy is strict, so the
    load-bearing assertion is this one — a check that has never been seen to
    fail proves nothing.
    """
    assert "csp-external-asset" in _rules(_style(
        '<link href="https://cdn.example-not-allowed.net/x.css" rel="stylesheet">'))


def test_an_unreadable_policy_denies_rather_than_permits():
    """Fail closed. A parse error must not license every CDN on the internet."""
    from check_table_stakes import csp_allowed_hosts

    assert csp_allowed_hosts(root="/nonexistent-path") == set()


def test_sixteen_px_is_the_floor_not_a_violation():
    assert not _rules(_style("  font-size: 16px;"))


def test_motion_with_the_media_query_present_is_fine():
    css = ("  transition: opacity 150ms ease-out;\n"
           "@media (prefers-reduced-motion: reduce) { * { transition: none } }")
    assert not _rules(check_style("a.css", [(1, "  transition: opacity 150ms"
                                             " ease-out;")], css))


# --- the rules the docs promise actually exist -----------------------------

def test_every_documented_rule_is_implemented():
    """`--explain` output is a promise; keep it true."""
    from check_table_stakes import RULES
    implemented = {
        "no-marketing-adjectives", "one-helper-verb",
        "no-chatty-phrasal-verbs", "sentence-length",
        "banned-primary-font", "csp-external-asset", "ios-zoom-font-size",
        "reduced-motion",
    }
    for rule in implemented:
        assert rule in RULES, f"{rule} is enforced but undocumented"
    for line in RULES.splitlines():
        head = line.strip().split(" ")[0]
        if head and head[0].islower() and "-" in head:
            assert head in implemented, f"{head} is documented but not enforced"
