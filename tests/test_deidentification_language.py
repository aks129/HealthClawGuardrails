"""Public surfaces must not overclaim a legal de-identification standard.

This guard existed and still let two claims through, which is the more useful
half of the story. It matched four exact phrases on nine files. `SECURITY.md`
was not one of the files, and

    "Reads pass through HIPAA Safe Harbor or patient-controlled redaction"

is not one of the phrases — so the flat claim sat in the security policy
document, which is the first thing a partner's reviewer reads, and the suite
was green. A published blog post said "identifiers are stripped using HIPAA
Safe Harbor rules" for the same reason.

What the code actually does (`r6/redaction.py:83`) is truncate every
identifier value to its last four characters. Safe Harbor
§164.514(b)(2)(i)(G) requires the Social Security number REMOVED, not
shortened, and a last-four SSN is a recognised re-identification vector. The
behaviour is defensible as a compensating control. The unhedged claim is not.

So the needle changed from four phrases to the claim itself: on a public
surface, the words "Safe Harbor" may appear only in a sentence that says it
is not the legal standard. Every hedged usage in the repository already
writes it hyphenated — Safe-Harbor-*style* — which is why that form is left
alone and the spaced form is what gets caught.

MUTATION: revert either corrected claim, or drop SECURITY.md from the list
-> red.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SURFACES = (
    "README.md",
    "SECURITY.md",
    "templates/faq.html",
    "templates/r6_dashboard.html",
    "templates/index.html",
    # static/js/r6-dashboard.js was on this list until the dashboard became a
    # server-rendered conformance report and stopped loading any script. The
    # file is deleted, not exempted — a missing path here raises
    # FileNotFoundError rather than passing quietly, which is what keeps this
    # list from shrinking by accident.
    "skills/phi-redaction/SKILL.md",
    "skills/healthex-export/SKILL.md",
    "r6/routes.py",
)

#: Published, and therefore public whatever directory they live in. Globbed,
#: not listed: a post added next month is a public surface on the day it is
#: written, and a list would have to be remembered.
_PUBLISHED_GLOBS = ("docs/blog/*.md", "docs/recipes/*.md",
                    # Added with the first spec that lives here. A
                    # specification is the MOST public surface we have —
                    # it is written for other implementers to build to —
                    # and this guard did not open the directory. Same
                    # shape as the gap it was widened to close earlier
                    # today: the scan was only as wide as its file list.
                    "docs/specs/*.md")

#: The ways the repository disclaims the legal standard. A sentence carrying
#: one of these may name Safe Harbor; nothing else may. `r6/routes.py:3519`
#: earns its mention exactly this way — "this is patient-controlled
#: redaction, not HIPAA Safe Harbor (birthDate is preserved, which Safe Harbor
#: strips)" is a better disclaimer than the boilerplate, and a guard that
#: rejected it would push writers toward vaguer text.
_QUALIFIERS = ("not a legal", "not HIPAA Safe Harbor", "not Safe Harbor",
               "not Expert Determination")


def _public_files():
    for relative_path in PUBLIC_SURFACES:
        # Missing path raises rather than passing quietly — see the note above.
        yield relative_path, (ROOT / relative_path).read_text()
    for pattern in _PUBLISHED_GLOBS:
        matched = sorted(ROOT.glob(pattern))
        assert matched, f"{pattern} matched nothing — the glob has gone stale"
        for path in matched:
            yield str(path.relative_to(ROOT)), path.read_text()


def test_public_surfaces_call_output_a_preview_not_safe_harbor():
    forbidden = (
        "HIPAA Safe Harbor de-identification",
        "Safe Harbor De-identified",
        "De-identify Patient (Safe Harbor)",
        "hipaa-safe-harbor",
    )

    for relative_path, text in _public_files():
        for phrase in forbidden:
            assert phrase not in text, f"{relative_path} still contains {phrase!r}"


def test_the_scan_cannot_be_narrowed_without_saying_so():
    """A guard is only as wide as its file list, and nothing watched the list.

    Both claims found today were in files this scan did not open. Fixing the
    text and leaving the coverage unpinned would mean the next narrowing is
    silent — and narrowing is indistinguishable from fixing, in a diff, if
    nothing counts.

    MUTATION: delete SECURITY.md from PUBLIC_SURFACES, or empty
    _PUBLISHED_GLOBS -> red.
    """
    covered = {relative_path for relative_path, _ in _public_files()}

    for required in ("README.md", "SECURITY.md", "r6/routes.py"):
        assert required in covered, (
            f"{required} is a public surface and is no longer scanned")

    published = {p for p in covered if p.startswith("docs/")}
    assert len(published) >= 5, (
        "docs/blog and docs/recipes hold 5 published documents today and the "
        f"scan reached {len(published)}; a glob that stops matching reads "
        "exactly like a clean pass")


def _unqualified_mentions(text):
    """Sentences that name Safe Harbor without disclaiming it.

    By SENTENCE, not by line. Both real disclaimers in this repository wrap
    across a line break — `r6/routes.py:3519-3520` puts "not HIPAA Safe
    Harbor" on one line and "(birthDate is preserved, which Safe Harbor
    strips)" on the next. A line-granular check calls the second line a
    violation, and the only way to satisfy it is to write worse prose.
    """
    flat = re.sub(r"\s+", " ", text)
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        if "Safe Harbor" not in sentence:
            continue
        if any(q in sentence for q in _QUALIFIERS):
            continue
        yield sentence.strip()[:120]


def test_naming_safe_harbor_on_a_public_surface_disclaims_it():
    """The claim, not four spellings of it.

    Redaction truncates identifiers rather than removing them, so an unhedged
    "Safe Harbor" is wrong about what the product does — and it is wrong in
    the direction a compliance reviewer will act on.
    """
    unqualified = []
    for relative_path, text in _public_files():
        unqualified += [f'{relative_path}: "{s}"'
                        for s in _unqualified_mentions(text)]

    assert not unqualified, (
        'a public surface names the legal standard without disclaiming it. '
        'Redaction truncates identifiers to their last four characters, which '
        'Safe Harbor does not permit for an SSN. Write it '
        '"Safe-Harbor-*style* field redaction", or say in the same sentence '
        'that it is "not a legal" determination:\n  ' + '\n  '.join(unqualified))


def test_the_qualifier_check_can_actually_fail():
    """A guard that cannot fail is the defect it was written to catch — and
    this file shipped one for months.

    Both halves proven on synthetic text: the claim is caught, and the real
    disclaimers — including the one that wraps across a line break — are not.
    """
    assert list(_unqualified_mentions(
        "Reads pass through HIPAA Safe Harbor redaction.")) == [
        "Reads pass through HIPAA Safe Harbor redaction."]

    assert not list(_unqualified_mentions(
        "De-identification preview (not a legal Safe Harbor determination)"))

    assert not list(_unqualified_mentions(
        "NOTE: this is patient-controlled redaction, not HIPAA Safe\n"
        "Harbor (birthDate is preserved, which Safe Harbor strips)."))

    # And the hyphenated house style is not a mention at all.
    assert not list(_unqualified_mentions(
        "Redaction is Safe-Harbor-style field redaction."))
