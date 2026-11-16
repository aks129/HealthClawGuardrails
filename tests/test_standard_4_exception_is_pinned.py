"""Standard 4's one exception must keep its heading and all five conditions.

The exception lets a write of person-attested configuration take the
internal secret without step-up. It is safe only as a closed list: a reviewer
checks a diff against conditions (a) to (e), and a condition that drops out
of the file widens the exception without anybody deciding to.

This pins the text, and nothing more. It does not check that any PR meets a
condition; the standard says so itself, and that sentence is pinned too.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STANDARDS = ROOT / ".github/REVIEW_STANDARDS.md"
DEVELOPMENT = ROOT / "docs/development.md"

HEADING = "**One exception: person-attested configuration.**"
LABELS = ["a", "b", "c", "d", "e"]


def _standard_4() -> str:
    """Item 4 alone, from its number to the start of item 5.

    Bind to the item, not to the file: the labels and the issue number are
    short strings, and a match anywhere else would keep this green after the
    exception lost them (catalogue §4).
    """
    text = STANDARDS.read_text(encoding="utf-8")
    start = text.index("\n4. **Writes require step-up auth")
    end = text.index("\n5. **", start)
    return text[start:end]


def _condition(label: str) -> str:
    """One condition's bullet, from its label to the next bullet or blank."""
    block = _standard_4()
    found = re.search(
        r"^   - \(%s\) \*\*.*?(?=^   - \(|^\s*$)" % label,
        block, re.MULTILINE | re.DOTALL)
    assert found, "standard 4's exception has no condition (%s)" % label
    return found.group(0)


def test_the_exception_is_inside_standard_4():
    """MUTATION: delete the exception's heading line -> red."""
    assert HEADING in _standard_4()


@pytest.mark.parametrize("label", LABELS)
def test_each_condition_is_present(label):
    """MUTATION: delete condition (c)'s bullet -> red, for `c` only."""
    assert _condition(label).strip()


def test_there_are_exactly_five_conditions():
    """MUTATION: add a condition (f), or delete one -> red.

    The standard says "all five" and "those five", so the count is part of
    the text.
    """
    found = re.findall(r"^   - \(([a-z])\) \*\*", _standard_4(), re.MULTILINE)
    assert found == LABELS


def test_the_refusal_audit_condition_names_its_issue():
    """MUTATION: drop "(#1131)" from condition (e)'s bold title -> red.

    The title, not the bullet: the bullet names the issue four times, and the
    first version of this test stayed green with the title's mention gone.
    """
    title = re.match(r"   - \(e\) \*\*(.*?)\*\*", _condition("e"), re.DOTALL)
    assert title, "condition (e) has no bold title"
    assert "(#1131)" in title.group(1)


def test_the_exception_waives_step_up_only():
    """MUTATION: delete the "Standards 3 and 6 still apply" sentence -> red."""
    block = _standard_4()
    assert "waives step-up only" in block
    assert "Standards 3 and 6 still apply" in block


def test_the_standard_says_no_condition_is_checked_mechanically():
    """MUTATION: delete the "None of the five is checked" sentence -> red.

    Without it this file reads as proof that the conditions are enforced.
    """
    assert "None of the five is checked mechanically" in _standard_4()


def test_the_development_guide_points_at_the_exception():
    """MUTATION: delete the exception bullet from docs/development.md -> red.

    The guide repeats standard 4, so it has to carry the exception or the two
    disagree.
    """
    guide = DEVELOPMENT.read_text(encoding="utf-8")
    start = guide.index("## Security invariants")
    end = guide.index("\n## ", start + 1)
    invariants = guide[start:end]
    assert "person-attested configuration" in invariants
    assert "REVIEW_STANDARDS.md" in invariants
