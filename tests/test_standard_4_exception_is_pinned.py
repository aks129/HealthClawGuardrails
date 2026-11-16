"""Standard 4's one exception must keep its heading, labels and key phrases.

The exception lets a write of person-attested configuration take the
internal secret without step-up. It is safe only as a closed list: a reviewer
checks a diff against conditions (a) to (e). A condition can leave that list
two ways, by being deleted or by being reworded under a surviving label, and
either one widens the exception without anybody deciding to.

What this file checks, and all it checks:

  - the heading sits inside standard 4
  - the labels are (a) to (e), in order, and no others
  - each condition's own bullet still holds the phrases in `PHRASES`
  - the sentences around the list that limit it are still there
  - standard 21, the development guide and CONTRIBUTING.md still point at it

It does not check any other wording in a condition, and it does not check
that any PR meets a condition. The standard says the second itself, and that
sentence is one of the ones checked here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STANDARDS = ROOT / ".github/REVIEW_STANDARDS.md"
DEVELOPMENT = ROOT / "docs/development.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

HEADING = "**One exception: person-attested configuration.**"
LABELS = ["a", "b", "c", "d", "e"]

# The words in each condition that carry it. Read from the condition's own
# bullet, with line breaks folded, so a phrase elsewhere in the file cannot
# stand in for one that was reworded here (catalogue §4).
PHRASES = [
    ("a", "every scope"),
    ("b", "handler on the approval surface"),
    ("b", "login-required, CSRF-protected and ownership-checked"),
    ("c", "never a clinical FHIR write"),
    ("d", "without truncation or normalisation"),
    ("e", "before the first real call"),
]


def _fold(text: str) -> str:
    """Line breaks and indentation folded to single spaces."""
    return re.sub(r"\s+", " ", text)


def _item(number: int) -> str:
    """One numbered standard, from its number to the next numbered item.

    Bind to the item, not to the file: the labels and the issue number are
    short strings, and a match anywhere else would keep this green after the
    exception lost them (catalogue §4).
    """
    text = STANDARDS.read_text(encoding="utf-8")
    head = "\n%d. **" % number
    start = text.index(head)
    end = re.compile(r"^(\d+\. \*\*|## )", re.MULTILINE).search(
        text, start + len(head))
    assert end, "standard %d has no end" % number
    return text[start:end.start()]


def _standard_4() -> str:
    block = _item(4)
    assert block.startswith("\n4. **Writes require step-up auth")
    return block


def _condition(label: str) -> str:
    """One condition's bullet, from its label to the next bullet or blank."""
    found = re.search(
        r"^   - \(%s\) \*\*.*?(?=^   - \(|^\s*$)" % label,
        _standard_4(), re.MULTILINE | re.DOTALL)
    assert found, "standard 4's exception has no condition (%s)" % label
    return found.group(0)


def test_the_exception_is_inside_standard_4():
    """MUTATION: delete the exception's heading line -> red."""
    assert HEADING in _standard_4()


def test_there_are_exactly_five_conditions():
    """MUTATION: add a condition (f), or delete condition (c) -> red.

    The standard says "all five" and "those five", so the count is part of
    the text.
    """
    found = re.findall(r"^   - \(([a-z])\) \*\*", _standard_4(), re.MULTILINE)
    assert found == LABELS


@pytest.mark.parametrize("label, phrase", PHRASES)
def test_each_condition_keeps_its_phrase(label, phrase):
    """A label that survives a rewrite is not the condition surviving.

    MUTATION, one per row, each keeping the label:
      (a) "every scope" -> "read scope" -> red
      (b) "is a handler" -> "is a service" -> red
      (b) drop "CSRF-protected" -> red
      (c) "never a clinical" -> "not usually a clinical" -> red
      (d) drop "without truncation or normalisation" -> red
      (e) "before the first real call" -> "soon" -> red
    """
    assert phrase in _fold(_condition(label)), (
        "condition (%s) of standard 4's exception no longer says %r"
        % (label, phrase))


def test_the_refusal_audit_condition_names_its_issue():
    """MUTATION: drop "(#1131)" from condition (e)'s bold title -> red.

    The title, not the bullet: the bullet names the issue several times, and
    the first version of this test stayed green with the title's mention
    gone.
    """
    title = re.match(r"   - \(e\) \*\*(.*?)\*\*", _condition("e"), re.DOTALL)
    assert title, "condition (e) has no bold title"
    assert "(#1131)" in title.group(1)


def test_the_exception_names_the_stores_it_covers():
    """MUTATION: delete "and no others" -> red.

    Two named stores, so a third needs an edit to the standard.
    """
    block = _fold(_standard_4())
    assert ("It covers two stores and no others: saved contacts and "
            "attested facts.") in block


def test_the_exception_waives_step_up_only():
    """MUTATION: delete the "Standards 3 and 6 still apply" sentence -> red."""
    block = _fold(_standard_4())
    assert "waives step-up only" in block
    assert "Standards 3 and 6 still apply" in block


def test_the_standard_says_no_condition_is_checked_mechanically():
    """MUTATION: delete the "None of the five is checked" sentence -> red.

    Without it this file reads as proof that the conditions are enforced.
    """
    assert "None of the five is checked mechanically" in _fold(_standard_4())


def test_standard_21_points_at_the_exception():
    """MUTATION: delete the carve-out sentence from standard 21 -> red.

    Condition (e) lets a store merge before its refusal audit. Standard 21
    says feature and hardening ship together, so it has to say where the one
    carve-out lives or the two standards disagree.
    """
    block = _fold(_item(21))
    assert "standard 4's exception" in block
    assert "#1131" in block


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


def test_contributing_points_at_the_exception():
    """MUTATION: delete the exception sentence from CONTRIBUTING.md -> red.

    Bound to the step-up bullet, which is the text the exception qualifies.
    """
    text = CONTRIBUTING.read_text(encoding="utf-8")
    start = text.index("- **Writes require a step-up token.**")
    end = text.index("\n- **", start + 1)
    bullet = _fold(text[start:end])
    assert "person-attested configuration" in bullet
    assert "REVIEW_STANDARDS.md" in bullet
