"""A count restated from a transcript must equal what the transcript shows.

The 2026-08-16 set-2 pack measured `$conformance` at Grade F, 1/7 against a
live HAPI and explained that the F came from the probe rather than from the
guardrails. That explanation carried a count — "four of the six failures
naming gates that were working" — and the count was copied into five places,
including a docstring in `r6/conformance/probes.py`. It was never true. The
transcript printed directly beneath the sentence shows five FAIL blocks, two
of which carry `on_failure` text blaming a gate. Six, five and two are all
findable in that transcript; four is not.

The defect is not the arithmetic. It is that a number describing a transcript
sat next to the transcript for three weeks, in production code and in the
spec other implementers are told to follow, with nothing comparing the two.
This file is that comparison.

Deliberately NOT pinned: the wording of either docstring. Prose about a
measurement should be free to improve. What is pinned is the transcript's own
counts, the two `on_failure` strings in the probe source that the prose
describes, and that no document restates the count that was wrong.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "docs" / "evidence" / "2026-08-16-set2-connectors.md"
PROBES = REPO / "r6" / "conformance" / "probes.py"

#: The count that was wrong, in the forms it was written in. Kept narrow on
#: purpose: "Four of the six feature sets have never been run end to end"
#: (hard-truths) and "Four of the six sets are unmeasured" (prd/README) are
#: different, correct claims about something else, and a looser pattern would
#: fail on them and teach the next person to delete this test.
WRONG_COUNT = re.compile(
    r"[Ff]our of (?:the )?six (?:failures|failing)"
    r"|[Ff]our properties then fail"
    r"|[Ff]our of the six failures naming",
)


def _fail_block_transcript() -> str:
    """The fenced block in the pack that lists the failing properties."""
    text = PACK.read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(.*?)```", text, re.DOTALL)
    matching = [b for b in blocks if "### FAIL " in b]
    assert len(matching) == 1, (
        f"expected exactly one FAIL-block transcript in {PACK.name}, "
        f"found {len(matching)}"
    )
    return matching[0]


def test_the_transcript_shows_five_fail_blocks_and_two_gate_blames():
    """The two numbers the corrected prose claims, read off the transcript."""
    block = _fail_block_transcript()

    fail_blocks = re.findall(r"^### FAIL (\w+)$", block, re.MULTILINE)
    assert len(fail_blocks) == 5, fail_blocks

    # A gate blame is `on_failure` text asserting the gate itself is broken.
    # Both read "...so its NNNs prove nothing", which is the phrasing that
    # turns a probe-setup failure into an accusation against the product.
    gate_blames = re.findall(r"on_failure: the gate .*prove nothing", block)
    assert len(gate_blames) == 2, gate_blames

    # Four is not any of them, and is not the harness's six either.
    assert not WRONG_COUNT.search(block), "the transcript itself must not be edited"


def test_the_harness_named_six_failing_properties_not_five():
    """Why "six" was defensible while "four" never was.

    The harness grades seven properties and named six as failing. The
    walkthrough asserted on five of those, excluding `error_fidelity` — the
    known #498 failure, which fails against Firely too and so is not the
    collision. Both numbers are real and describe different things; a
    correction that flattened them into one would lose that.
    """
    text = PACK.read_text(encoding="utf-8")
    listed = re.search(
        r"failing properties: \[(.*?)\]", text, re.DOTALL,
    )
    assert listed, "the step-4 failing-properties list is gone from the pack"
    names = re.findall(r"'(\w+)'", listed.group(1))
    assert len(names) == 6, names
    assert "error_fidelity" in names

    # And the walkthrough's own assertion line names the other five.
    asserted = re.search(
        r"properties that should hold did not: (.*?)\n", text,
    )
    assert asserted
    assert "error_fidelity" not in asserted.group(1)


def test_exactly_two_probe_checks_blame_the_gate_in_their_failure_text():
    """The prose count, checked against the code that produces it.

    Written against string literals rather than by running the probes: the
    point is that the number in the docstring above them is the number of
    accusing strings below it, and that stays checkable with no server.
    """
    source = PROBES.read_text(encoding="utf-8")
    # Strip the module's own explanatory comments and docstrings' quoted
    # examples would be caught too, so count only the literals that are
    # passed as `on_failure=`.
    on_failure_args = re.findall(
        r"on_failure=\(?\s*((?:\"[^\"]*\"\s*)+)", source,
    )
    blaming = [a for a in on_failure_args if "prove nothing" in a]
    assert len(blaming) == 2, blaming


@pytest.mark.parametrize(
    "relative",
    [
        "r6/conformance/probes.py",
        "tests/test_conformance_probe_can_run_twice.py",
        "docs/evidence/2026-08-16-set2-connectors.md",
        "docs/specs/guardrail-spec-0.1.0-draft.md",
        "docs/2026-08-16-hard-truths.md",
    ],
)
def test_the_wrong_count_is_not_restated(relative):
    """All five sites it was copied to.

    Listed by name rather than swept repo-wide: a sweep would go green the
    day someone writes the number in a sixth file this list does not know
    about, and would give a false sense that the class is guarded. It is not
    — only these five are.
    """
    # Whitespace-normalised, NOT line by line. In `probes.py` the original
    # read "...with four of the\n    six failures naming gates...", so every
    # line-oriented check goes green on the exact text this test exists to
    # catch. That was this file's first version, and it passed against the
    # unfixed docstring.
    flat = re.sub(r"\s+", " ", (REPO / relative).read_text(encoding="utf-8"))

    hits = []
    for m in WRONG_COUNT.finditer(flat):
        context = flat[max(0, m.start() - 220):m.end() + 60]
        # A dated correction note has to quote the count it is correcting.
        # `Count corrected` is the marker; anything else is a live restatement.
        if "Count corrected" in context:
            continue
        hits.append(context)
    assert not hits, hits


def test_the_docstring_does_not_present_the_f_as_current():
    """The F was measured on 2026-08-16 and fixed the same day by #514.

    A grade in a present-tense docstring is read as today's grade. This pins
    only that a stated F is dated and carries its fix — NOT that the F must
    be stated at all. Deleting an expired grade from production code is the
    tidiest possible improvement to this docstring, and a test that forced
    the number to stay would be the same defect one level up: a guard
    demanding that an out-of-date claim remain in the tree.
    """
    source = PROBES.read_text(encoding="utf-8")
    doc = re.search(r'def _synthetic_patient\(\):\n    """(.*?)"""', source, re.DOTALL)
    assert doc, "_synthetic_patient lost its docstring"
    body = doc.group(1)
    if "Grade F" not in body:
        pytest.skip("the docstring no longer states the grade; nothing to date")
    assert "#514" in body, "the docstring states the F without saying it was fixed"
    assert "2026-08-16" in body, "the F is stated without the date it was measured"
