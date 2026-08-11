"""What the harness does not grade is published beside what it does (#401).

The conformance grade is read as a product claim. "Grade A, 7/7, 35 checks"
with no list of exclusions reads as "these seven are the surface" — and the
first thing a skeptical adopter checks, whether a stranger can read patient
records, is not one of them. READ_AUTH_ENABLED defaults off and is off in this
harness's own fixture, so a deployment serving records to anyone who asks
scores exactly what a correctly-gated one scores.

That gap needs a declared-posture design before a probe can grade it (#401
explains why adding a naive probe reproduces the #213 defect). Until then the
honest move is to say so, on the page that publishes the grade.

The failure mode this file guards is the SECOND one: a stale exclusion. A
list of "we do not measure X" that outlives the probe for X is as dishonest as
a stale claim — it just fails in the direction that looks modest, so nobody
goes looking for it.
"""

import html as html_mod
import re
from pathlib import Path

from r6.conformance.probes import PROPERTIES, UNGRADED

ROOT = Path(__file__).resolve().parents[1]


def test_no_property_is_both_graded_and_listed_as_ungraded():
    """MUTATION: add 'phi_redaction' to UNGRADED -> red.

    Removing an entry from UNGRADED is part of adding the probe that grades
    it. This is the line that makes that a rule rather than a hope.
    """
    graded = {p.replace('_', ' ') for p in PROPERTIES}
    listed = {name.lower() for name, _ in UNGRADED}
    both = {n for n in listed if n in graded}
    assert not both, (
        f'{sorted(both)} appear in both PROPERTIES and UNGRADED. If the '
        'probe now grades it, delete the UNGRADED entry in the same PR.')


def test_every_entry_says_what_is_not_covered():
    """A name with no explanation is a word, not a disclosure."""
    for name, why in UNGRADED:
        assert name.strip(), 'an UNGRADED entry has no name'
        assert len(why.strip()) > 40, (
            f'{name!r} is listed as ungraded without saying what that means '
            'for a reader')


def test_read_auth_is_named_explicitly():
    """The one an adopter checks first, and the reason #401 exists.

    Pinned by name rather than by count so that trimming the list cannot
    quietly drop the entry that matters most.
    """
    names = ' '.join(name for name, _ in UNGRADED).lower()
    whys = ' '.join(why for _, why in UNGRADED).lower()
    assert 'read auth' in names, 'read authentication is no longer listed'
    assert '#401' in whys, 'the read-auth entry no longer cites its issue'


class TestThePagePublishesThem:

    def test_each_ungraded_entry_reaches_the_dashboard(self, client):
        """MUTATION: drop the {% for %} over `ungraded` -> red."""
        # Unescaped before comparing: Jinja renders the apostrophe in "the
        # action rail's separation" as &#39;, so a raw substring test fails on
        # a page that is perfectly correct. Comparing against the wrong
        # representation of the thing you are checking is the same mistake as
        # not checking at all — it just fails loudly instead of quietly.
        html = html_mod.unescape(
            client.get('/r6-dashboard').data.decode())
        for name, _ in UNGRADED:
            assert name in html, f'{name!r} is not on the page'

    def test_they_survive_a_failed_measurement(self, client, monkeypatch):
        """The exclusions do not depend on today's run.

        A page that drops its scope limits when the fetch fails is at its
        least honest exactly when it has least to show — the state where a
        reader most needs to know what was never covered.

        MUTATION: move the {% for %} inside `{% if snapshot.measured %}` -> red.
        """
        from r6.conformance import snapshot as snap

        def boom(*a, **k):
            raise snap.HarnessUnavailable('no measurement in this test')
        monkeypatch.setattr(snap, 'local_report', boom)
        monkeypatch.setattr(snap, 'remote_report', boom)

        html = html_mod.unescape(
            client.get('/r6-dashboard').data.decode())
        assert 'No measurement. This is not a pass.' in html
        for name, _ in UNGRADED:
            assert name in html, (
                f'{name!r} vanished from the page when the run failed')


def test_the_issue_is_still_open_or_the_entry_is_gone():
    """Documentation-only: the read-auth entry cites #401, so the two move
    together. Asserted as a source-level cross-reference rather than a
    network call — a test that reaches GitHub fails on a plane.
    """
    probes = (ROOT / 'r6' / 'conformance' / 'probes.py').read_text()
    block = re.search(r'UNGRADED = \((.*?)\n\)\n', probes, re.S)
    assert block, 'UNGRADED tuple not found'
    assert '#401' in block.group(1), (
        'the read-auth exclusion no longer points at the issue that explains '
        'why it cannot simply be probed')
