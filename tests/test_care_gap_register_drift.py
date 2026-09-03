"""The rule register must say what the rules say.

`docs/clinical/care-gap-rule-register.md` is a sign-off document: a clinician
initials it to say these populations and these cadences are what they are
willing to have shown to a patient. A cadence that changes in code and not on
that page turns the signature into cover for something nobody agreed to.

This is the #295 shape aimed at a clinical artifact — documentation describing
behaviour the system does not have — and the 08-02 retro's finding that
"documentation is part of the product surface and it is the only part with no
CI". Two guards exist for endpoint and tool-catalogue drift; this is the third.

Deliberately NOT a full-text comparison. It pins the values a reviewer signs
against — rule ids, age bands, cadence months, the codes that close each gap —
and leaves the prose free to be edited.

EVERY assertion is scoped to the rule's OWN section. The first version of this
file searched the whole document, which made it a check that a number appeared
*somewhere*: moving cervical screening from 36 to 60 months left it green,
because lipid screening is 60 months and the register said so. That is
docs/2026-08-02-retro.md's pattern reproduced inside the guard written against
it — caught by mutating the rule table and watching nothing happen.
"""

from __future__ import annotations

import pathlib
import re

from r6.caregaps.evaluate import CARE_GAP_RULES

REGISTER = (pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "clinical" / "care-gap-rule-register.md")

#: `## 4. `colorectal-screening` — Colorectal cancer screening`
_HEADING = re.compile(r"^## \d+\. `([a-z0-9-]+)` — (.+)$", re.MULTILINE)


def _text():
    return REGISTER.read_text(encoding="utf-8")


def _sections():
    """{rule_id: (title, the text of that rule's section)}.

    A section runs to the next rule heading, so a value asserted below has to
    appear against the rule it belongs to and not in a neighbour's row.
    """
    body = _text()
    marks = list(_HEADING.finditer(body))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out[m.group(1)] = (m.group(2), body[m.start():end])
    return out


def test_the_register_exists_and_is_not_a_stub():
    """Guard the guard: an empty file passes every `in` assertion below."""
    assert REGISTER.exists(), f"{REGISTER} is missing"
    body = _text()
    assert len(body) > 2000, "the register collapsed to a stub"
    assert "Sign-off" in body


def test_the_sections_parse_and_cover_every_rule_exactly_once():
    """The other guard on the guard. If `_sections` stops matching — someone
    renumbers the headings, say — every per-rule test below would silently
    assert against nothing.
    """
    sections = _sections()
    assert set(sections) == {rule["id"] for rule in CARE_GAP_RULES}, (
        f"register documents {set(sections)}; "
        f"code has {{r['id'] for r in CARE_GAP_RULES}}")
    for rule in CARE_GAP_RULES:
        title, section = sections[rule["id"]]
        assert title == rule["title"], (
            f"{rule['id']}: register calls it {title!r}, code calls it "
            f"{rule['title']!r}")
        assert len(section) > 200, f"{rule['id']}: section is empty"


def test_every_age_band_is_stated_as_the_code_has_it():
    sections = _sections()
    for rule in CARE_GAP_RULES:
        applies = rule["applies"]
        span = f"{applies['min_age']}–{applies['max_age']}"
        assert span in sections[rule["id"]][1], (
            f"{rule['id']}: age range {span} not in its own section")


def test_every_cadence_is_stated_in_the_months_the_code_uses():
    """Months, not the prose form: "yearly" is shared across rules, and the
    number has to sit in the section it describes.

    MUTATION: change any rule's `cadence_months` -> red for that rule.
    """
    sections = _sections()
    for rule in CARE_GAP_RULES:
        section = sections[rule["id"]][1]
        months = rule["cadence_months"]
        assert re.search(rf"\b{months} months\b", section), (
            f"{rule['id']}: cadence of {months} months is not stated in its "
            "own section")
        for band in rule.get("cadence_bands") or ():
            assert re.search(rf"\b{band['cadence_months']} months\b", section), (
                f"{rule['id']}: age band cadence of "
                f"{band['cadence_months']} months is not stated")
            assert f"{band['min_age']}–{band['max_age']}" in section, (
                f"{rule['id']}: age band "
                f"{band['min_age']}-{band['max_age']} is not stated")


def test_no_cadence_is_claimed_that_the_rule_does_not_have():
    """The reverse: a stale month figure left behind after a cadence moved.

    Without this, correcting a cadence and ADDING the new number while leaving
    the old sentence in place reads as two cadences and passes the test above.
    """
    sections = _sections()
    for rule in CARE_GAP_RULES:
        section = sections[rule["id"]][1]
        allowed = {rule["cadence_months"]}
        allowed |= {b["cadence_months"] for b in rule.get("cadence_bands") or ()}
        claimed = {int(n) for n in re.findall(r"\b(\d+) months\b", section)}
        assert claimed <= allowed, (
            f"{rule['id']}: register states {sorted(claimed - allowed)} "
            f"months, which the rule does not use (it uses {sorted(allowed)})")


def test_every_code_that_closes_a_gap_is_listed_against_its_own_rule():
    """The column a reviewer checks hardest: what actually counts as done.

    MUTATION: add a code to any rule's `satisfied_by` -> red for that rule.
    """
    sections = _sections()
    for rule in CARE_GAP_RULES:
        section = sections[rule["id"]][1]
        satisfied = rule["satisfied_by"]
        assert satisfied["resource"] in section, rule["id"]
        for code in satisfied["codes"]:
            assert f"`{code}`" in section, (
                f"{rule['id']}: code {code} closes this gap and its section "
                "does not list it")


def test_no_code_is_listed_that_does_not_close_that_gap():
    """A code removed from a rule but left on the page tells a clinician a
    test counts when it no longer does."""
    sections = _sections()
    for rule in CARE_GAP_RULES:
        section = sections[rule["id"]][1]
        # Backticked tokens that look like a code value: LOINC (1234-5), CPT
        # and CVX (digits). Excludes prose and field names.
        listed = {t for t in re.findall(r"`([0-9]+-?[0-9]*)`", section)}
        extra = listed - set(rule["satisfied_by"]["codes"])
        # The A1c section also lists the diagnosis codes that select the
        # population, which are not codes that CLOSE the gap.
        if rule["id"] == "diabetes-a1c":
            from r6.caregaps.evaluate import (_DIABETES_PREFIXES,
                                              _DIABETES_SNOMED)
            extra -= set(_DIABETES_SNOMED) | set(_DIABETES_PREFIXES)
        assert not extra, (
            f"{rule['id']}: register lists {sorted(extra)} as closing this "
            "gap; the rule does not match them")


def test_a_rule_that_declines_to_decide_says_so_where_it_is_documented():
    """`unread_evidence` is the whole reason a rule reports indeterminate
    rather than due. It must reach the page a clinician signs, verbatim."""
    sections = _sections()
    for rule in CARE_GAP_RULES:
        evidence = rule.get("unread_evidence")
        if evidence:
            assert evidence in sections[rule["id"]][1], (
                f"{rule['id']} declines to decide because it cannot read "
                f"'{evidence}', and its section does not say so")


def test_the_register_carries_no_guideline_year_it_cannot_source():
    """`REFERENCES` encodes no years, so every Source row says "not encoded".

    A four-digit year beside a source would be recalled rather than read, and
    is exactly the value a clinician would trust hardest. If a year is ever
    encoded in the rule table, this test is what tells you to update the page.

    MUTATION: write "USPSTF 2021" into a source row -> red.
    """
    sections = _sections()
    for rule in CARE_GAP_RULES:
        rows = [ln for ln in sections[rule["id"]][1].splitlines()
                if ln.startswith("| Source ")]
        assert len(rows) == 1, f"{rule['id']}: expected one Source row"
        assert "not encoded" in rows[0], rows[0]
        assert not re.search(r"\b(19|20)\d{2}\b", rows[0]), (
            f"{rule['id']}: a guideline year appears in a source row that no "
            f"code encodes: {rows[0]}")
