"""A passing check must not print a sentence describing a failure.

The scorecard shipped with fifteen checks that read, in JSON:

    {"name": "no raw SSN in the audit trail",
     "passed": true,
     "detail": "PHI leaked into audit"}

The checks were right. The report was not. `detail` carried two different
things in one field — what the probe MEASURED ("status 200") and what a
failure would MEAN ("PHI leaked into audit") — and the JSON emitted it beside
`passed` without distinguishing them. The text renderer hid `detail` on a pass,
which kept that one output honest and left every other consumer to rediscover
the rule on its own. None did.

Two readers hit it independently and both concluded the deployment was leaking
PHI. The second was the physician advisor about to record a demo, roughly
thirty hours before the recording. That is the cost being guarded against here:
not a wrong grade, a report that reads as the opposite of the truth to the
person best placed to act on it.

These tests are behavioural — they run the real harness and read what it
actually emits — because the defect was invisible in the probe logic and
visible only in the output.
"""

from __future__ import annotations

import json

import pytest

from r6.conformance.probes import Check

#: Words that only make sense about a check that FAILED. If one of these turns
#: up in a passing check's rendered output, the report is contradicting itself.
#:
#: Deliberately drawn from the sentences that actually shipped, not invented:
#: an aspirational list of "bad words" would drift, and a check that fires on
#: ordinary phrasing gets switched off.
_FAILURE_WORDS = (
    "leaked",
    "did not",
    "does not",
    "no auditevent",
    "no disclaimer",
    "prove nothing",
    "authorized a write",
    "was not attached",
    "passes trivially",
    "without isolating",
)


def _contradictions(text: str) -> list[str]:
    lowered = text.lower()
    return [w for w in _FAILURE_WORDS if w in lowered]


#: Separates a check's name from its detail in the text scorecard.
_SEP = " — "


def _detail_of(line: str) -> str:
    """The detail half of a rendered check line, or "" if it has none.

    Scanning the whole line was wrong on the first run: the check named "the
    upstream display did not survive" states a desired outcome, and "did not"
    in a NAME is not a contradiction. Only the detail is a claim about how
    this run went.
    """
    _, sep, detail = line.partition(_SEP)
    return detail if sep else ""


# --- the type itself --------------------------------------------------------

def test_the_measurement_is_the_third_positional_argument():
    """The safe field is the easy one to reach.

    Thirty-odd call sites pass a measurement positionally. Making `observed`
    third means every one of them is correct without being touched, and a
    failure explanation has to be named to get in — which is where the
    author has to think about it.

    MUTATION: swap the order of `observed` and `on_failure` -> red.
    """
    c = Check("some check", True, "status 200")
    assert c.observed == "status 200"
    assert c.on_failure == ""


def test_detail_on_a_passing_check_is_only_the_measurement():
    """MUTATION: return `on_failure` unconditionally from `detail` -> red."""
    c = Check("no raw SSN in the audit trail", True,
              "status 200", on_failure="PHI leaked into audit")
    assert c.detail == "status 200"
    assert "leaked" not in c.detail


def test_detail_on_a_failing_check_carries_both_halves():
    """The explanation is the whole reason it exists — it must survive a fail.

    MUTATION: drop `on_failure` from the failing branch of `detail` -> red.
    """
    c = Check("no raw SSN in the audit trail", False,
              "status 200", on_failure="PHI leaked into audit")
    assert "status 200" in c.detail
    assert "PHI leaked into audit" in c.detail


def test_detail_is_derived_and_cannot_be_set_independently():
    """One owner. A stored `detail` is a third copy that drifts from two.

    MUTATION: make `detail` a dataclass field again -> red.
    """
    with pytest.raises((AttributeError, TypeError)):
        Check("x", True, "status 200", detail="something else")  # type: ignore[call-arg]


# --- what the live harness actually emits -----------------------------------

@pytest.fixture
def report(client):
    return client.get("/r6/fhir/$conformance?fresh=1").get_json()


def test_the_harness_produced_something_to_check(report):
    """A pass over an empty scorecard is not a pass.

    The defect this file exists for lived in fifteen checks. A guard that
    silently examined zero of them would print the same word as one that
    examined all of them — the failure shape this repo keeps finding.
    """
    checks = [c for p in report["properties"] for c in p["checks"]]
    assert len(checks) >= 30, f"only {len(checks)} checks ran; the harness is not exercising the stack"
    assert any(c["passed"] for c in checks), "no check passed, so nothing here is being tested"


def test_no_passing_check_carries_a_failure_explanation_in_json(report):
    """The defect, stated as a property of the JSON a consumer reads.

    MUTATION: emit `on_failure` unconditionally in `to_dict` -> red on 15 checks.
    """
    offenders = []
    for prop in report["properties"]:
        for c in prop["checks"]:
            if not c["passed"]:
                continue
            # The name states what SHOULD be true and may legitimately be
            # phrased in the negative ("the upstream display did not
            # survive"). Only the values are claims about this run.
            values = {k: v for k, v in c.items() if k != "name"}
            words = _contradictions(json.dumps(values))
            if words:
                offenders.append(f"{prop['key']}/{c['name']}: {words} in {values}")

    assert not offenders, (
        "these checks PASSED while their own payload describes a failure:\n  "
        + "\n  ".join(offenders))


def test_no_passing_check_carries_a_failure_explanation_in_text(client):
    """The same property of the human scorecard, which is what gets screenshotted."""
    text = client.get("/r6/fhir/$conformance?format=text").get_data(as_text=True)

    offenders = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("✓"):
            continue
        words = _contradictions(_detail_of(stripped))
        if words:
            offenders.append(f"{stripped}  ->  {words}")

    assert not offenders, (
        "these lines mark a check PASSED and then describe a failure:\n  "
        + "\n  ".join(offenders))


def test_the_two_formats_agree_about_every_check(client):
    """JSON and text are two renderers of one fact, so they must not disagree.

    They did. The text renderer suppressed `detail` on a pass and the JSON did
    not, so the same run said different things depending on which URL you
    opened. Whichever rule is right, one rule has to own it — the shape
    docs/2026-08-02-retro.md describes.

    MUTATION: re-add `and not c.passed` to the text renderer's detail
    suffix -> red.
    """
    body = client.get("/r6/fhir/$conformance").get_json()
    text = client.get("/r6/fhir/$conformance?format=text").get_data(as_text=True)

    missing = []
    for prop in body["properties"]:
        for c in prop["checks"]:
            if not c["detail"]:
                continue
            if c["detail"] not in text:
                missing.append(f"{prop['key']}/{c['name']}: {c['detail']!r}")

    assert not missing, (
        "the JSON states a detail the text scorecard does not:\n  "
        + "\n  ".join(missing))


def test_the_failure_explanations_still_exist_where_they_belong():
    """Suppressing the sentences on a pass must not delete them.

    The cheap way to make every test above green is to remove the
    explanations entirely, which would cost the report the thing that makes a
    FAIL actionable. These are the five that mattered most; they are asserted
    against the probe source so the guard survives a run in which they all
    pass.

    MUTATION: delete any of these strings from probes.py -> red.
    """
    import pathlib
    import re

    source = pathlib.Path(
        __file__).resolve().parents[1].joinpath("r6/conformance/probes.py").read_text()
    # Python joins adjacent string literals, so a sentence long enough to wrap
    # is stored as `"...asked " \n "for; ..."` and a naive substring search
    # misses it. Rejoin them first — otherwise this guard would go red on
    # reflowing a line, which is how a gate that fires on ordinary work gets
    # switched off.
    source = re.sub(r'"\s*\n\s*"', "", source)

    for sentence in (
        "PHI leaked into audit",
        "no disclaimer on the response",
        "no AuditEvent with action=R references the resource",
        "the read did not return the Patient that was asked for",
        "authorized a write",
    ):
        assert sentence in source, (
            f"the failure explanation {sentence!r} is gone from probes.py — a "
            f"FAIL now says only what status came back, not what it means")
