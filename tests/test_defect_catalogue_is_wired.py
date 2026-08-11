"""The defect catalogue must stay wired to the gate, and stay evidenced.

A catalogue of past mistakes that nothing consults is exactly the shape it
documents in §1: a file asserting a practice, with no mechanism behind the
assertion. So the wiring is a test rather than an intention.

Three properties, each cheap and each load-bearing:

  - the review standard that makes it a gate still points at the file
  - the CI reviewer is still told to read it
  - every shape in it still carries evidence

The third is the one that decays. A catalogue grows by category when someone
adds a plausible-sounding failure mode with no incident behind it, and a
reviewer trained on imagined failures looks in the wrong place. Every entry
here cost this project a day, a wrong answer to a partner, or a defect in
front of a clinician; that is the bar.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "docs/defect-catalogue.md"
STANDARDS = ROOT / ".github/REVIEW_STANDARDS.md"
WORKFLOW = ROOT / ".github/workflows/claude-pr-review.yml"
TEMPLATE = ROOT / ".github/PULL_REQUEST_TEMPLATE.md"


@pytest.fixture(scope="module")
def catalogue() -> str:
    return CATALOGUE.read_text(encoding="utf-8")


def _shapes(text: str) -> list[str]:
    """The numbered shape headings, `## N. title`."""
    return re.findall(r"^## (\d+\..+)$", text, re.MULTILINE)


def test_the_catalogue_exists_and_has_shapes(catalogue):
    """A pass over an empty catalogue is not a pass."""
    shapes = _shapes(catalogue)
    assert len(shapes) >= 8, f"only {len(shapes)} shapes: {shapes}"


def test_the_review_standard_points_at_the_catalogue():
    """MUTATION: drop standard 27 from REVIEW_STANDARDS.md -> red.

    Standard 27 is what turns the catalogue from a document into a gate.
    """
    standards = STANDARDS.read_text(encoding="utf-8")
    assert "docs/defect-catalogue.md" in standards, (
        "REVIEW_STANDARDS.md no longer references the catalogue, so nothing "
        "makes a reviewer read it")


def test_the_ci_reviewer_is_told_to_read_the_catalogue():
    """MUTATION: remove the catalogue line from the reviewer prompt -> red.

    The standard can say "read this" and the automated reviewer still not be
    handed it. Both halves have to hold, which is the same two-owners
    problem the catalogue's own §3 describes.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # Bind to the INSTRUCTIONS, not to the filename. The first version of
    # this test asserted the string appeared anywhere in the file, and the
    # workflow mentions the catalogue in two places — so deleting one left
    # the test green. That is catalogue §4 (a guard blind to its own
    # subject) inside the guard for §4, found by mutation-testing this file.
    required = [
        # the reviewer is handed the catalogue at all
        "docs/defect-catalogue.md",
        # and told the specific thing that catches the other shapes
        "silently examine nothing",
        # and told to name the section it matched, so the author can read
        # what the shape cost last time
        "name the section",
    ]
    missing = [r for r in required if r not in workflow]
    assert not missing, (
        f"the PR-review workflow prompt no longer instructs the reviewer to "
        f"use the catalogue: missing {missing}. Standard 27 can require it "
        f"and the automated reviewer still never be handed it.")


def test_the_pr_template_asks_for_mutation_results():
    """Standard 28 wants evidence, not an assertion that tests pass.

    MUTATION: delete the "Mutations run" section from the template -> red.
    """
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "Mutations run" in template
    assert "defect-catalogue.md" in template


@pytest.mark.parametrize("shape", _shapes(CATALOGUE.read_text(encoding="utf-8")))
def test_every_shape_carries_evidence(shape, catalogue):
    """Each shape names a PR, an issue, or a dated incident.

    MUTATION: add a shape with no #PR and no date -> red.

    This is the guard against the catalogue growing by category. A shape
    without evidence is a guess about where the next defect will be, and it
    trains the reviewer to look there instead of where they actually happen.
    """
    start = catalogue.index(f"## {shape}")
    nxt = catalogue.find("\n## ", start + 1)
    body = catalogue[start:nxt if nxt != -1 else len(catalogue)]

    has_pr = re.search(r"#\d{2,4}\b", body)
    has_date = re.search(r"20\d\d-\d\d-\d\d", body)
    assert has_pr or has_date, (
        f"shape {shape!r} carries no evidence. Every entry needs a PR "
        f"number, an issue number, or a dated incident — a catalogue of "
        f"imagined failures trains a reviewer to look in the wrong place.")


def test_the_reviewers_own_failure_mode_is_first(catalogue):
    """§0 is deliberately ordered first, because it catches the others.

    Most code and most review here is AI-authored, which makes "a confident
    report from a check that did not run" structural rather than incidental.
    A reviewer who reads it last has already trusted six claims.

    MUTATION: renumber it below another shape -> red.
    """
    zero = catalogue.find("## 0.")
    one = catalogue.find("## 1.")
    assert zero != -1, "the catalogue has no §0"
    assert zero < one, "§0 must come first — it is the one that catches the rest"
