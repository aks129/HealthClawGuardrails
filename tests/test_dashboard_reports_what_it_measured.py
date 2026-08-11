"""The dashboard may not claim a guardrail that no probe checked.

Two defect shapes met on the old /r6-dashboard, and this file guards the
places they would come back.

docs/defect-catalogue.md §1 — a reassuring word doing a check's job. The page
carried a "Security Posture" panel: nine rows of hand-written HTML reading
"Tenant Isolation — Enforced", "PHI Redaction — All Reads", "Audit Trail —
Immutable", each beside a green check. Nothing measured any of them. The rows
would have stayed green through a total redaction failure, on the most-linked
page the project has.

docs/defect-catalogue.md §0 — a confident report from a check that did not
run. The replacement fetches a real measurement, which means it now has a way
to fail that the hand-written version did not: the fetch can come back empty.
A template handed an empty report renders a scorecard with no failures on it,
which reads exactly like a clean bill of health. "Examined nothing" and "found
nothing" have to look different, and on this page that difference is the whole
product.
"""

import re

import pytest

from r6.conformance import snapshot as snap


@pytest.fixture
def unmeasured(monkeypatch):
    """Make the measurement fail the way production would."""
    def boom(*a, **k):
        raise snap.HarnessUnavailable("upstream refused the connection")
    monkeypatch.setattr(snap, "local_report", boom)
    monkeypatch.setattr(snap, "remote_report", boom)


class TestFailureIsNotAPass:

    def test_a_failed_measurement_does_not_render_a_grade(self, client, unmeasured):
        """MUTATION: render the page normally when snapshot.measured is False -> red."""
        html = client.get('/r6-dashboard').data.decode()
        assert 'No measurement. This is not a pass.' in html
        # The verdict block must be absent, not merely empty. An empty one
        # still prints "Grade" over blank space, which reads as a pass.
        assert 'cf-verdict' not in html
        assert 'Properties passed' not in html

    def test_a_failed_measurement_says_why(self, client, unmeasured):
        """A refusal states its reason — the same rule the step-up gate follows."""
        html = client.get('/r6-dashboard').data.decode()
        assert 'upstream refused the connection' in html

    def test_a_failed_measurement_still_publishes_the_limits(self, client, unmeasured):
        """The scope section does not depend on the run, so losing the run
        must not quietly drop the caveats along with the grade."""
        html = client.get('/r6-dashboard').data.decode()
        assert 'What this grade does not cover' in html

    def test_the_page_never_reports_zero_failures_without_a_measurement(
            self, client, unmeasured):
        """The specific §0 shape: '0 checks failed' over a run that never
        happened. `summary` is None on this path precisely so the template
        cannot print a zero it did not count.

        MUTATION: pass `summarize({})` instead of None in app.py -> red.
        """
        html = client.get('/r6-dashboard').data.decode()
        assert 'Checks failed' not in html


class TestNoHandWrittenPosture:

    #: The words the deleted panel used to pair with a green check. Each one
    #: asserts an outcome, which is the harness's job to report.
    CLAIMS = ('Enforced', 'Immutable', 'Required', 'All Reads')

    def test_the_posture_panel_does_not_come_back(self, client):
        """MUTATION: re-add any 'Tenant Isolation … Enforced' row -> red.

        Matched against the rendered page rather than the template source so
        that reintroducing the claim from a Python constant, a macro or an
        include is caught too. The claim is the defect; where it was typed is
        not.

        The results table is excluded, and the reason is the whole point of
        the rule. One property really is called "Immutable Audit Trail" — that
        is its name in the harness, and the page prints it beside a pass/fail
        chip and one tape mark per check, three of them here. The same word in
        the old right-hand panel had nothing behind it at all. A claim next to
        its measurement is a result; the identical claim on its own is the
        defect. So this checks the prose, where nothing is measured.
        """
        html = client.get('/r6-dashboard').data.decode()
        body = html.split('<main', 1)[1].split('</main>', 1)[0]
        results = re.search(r'<div class="cf-props">.*?</section>', body, re.S)
        assert results, 'results table not found — the exclusion below would ' \
                        'silently widen to the whole page'
        prose = body.replace(results.group(0), '')
        text = re.sub(r'<[^>]+>', ' ', prose)
        found = [w for w in self.CLAIMS if re.search(rf'\b{w}\b', text)]
        assert not found, (
            f'the page asserts {found} in prose. Guarantees on this page come '
            'from the conformance report, which names a check for each one; '
            'see docs/defect-catalogue.md §1')

    def test_every_property_shown_carries_its_check_count(self, client):
        """A property row without checks behind it is the posture panel again
        in a different font.

        MUTATION: render a property row without its check tape -> red.
        """
        html = client.get('/r6-dashboard').data.decode()
        rows = html.count('class="cf-prop__name"')
        tapes = html.count('class="cf-tape"')
        assert rows > 0, 'no properties rendered'
        assert rows == tapes, (
            f'{rows} properties rendered but {tapes} have a check tape')


class TestNoDeadControls:

    def test_the_page_posts_nowhere(self, client):
        """MUTATION: add a <form method="post"> or an onclick fetch -> red.

        The page it replaced shipped fifteen buttons that POST to /r6/…. On
        healthclaw.io every one returned 405, because that host refuses writes
        to stateful paths. A control that cannot work where it is served is
        worse than no control: it reads as a broken product rather than a
        deliberately read-only one.
        """
        html = client.get('/r6-dashboard').data.decode()
        body = html.split('<main', 1)[1].split('</main>', 1)[0]
        assert 'method="post"' not in body.lower()
        assert 'onclick' not in body.lower()
        assert 'fetch(' not in body

    def test_the_rerun_control_is_absent_where_it_would_fail(
            self, client, monkeypatch):
        """MUTATION: render the re-run link unconditionally -> red.

        On the read-only host the harness cannot run, so a "run it again"
        control would do nothing. It is not rendered there at all.
        """
        monkeypatch.setenv('READ_ONLY_DEPLOYMENT', '1')
        html = client.get('/r6-dashboard').data.decode()
        assert 'Run a fresh check now' not in html

    def test_the_rerun_control_is_present_where_it_works(self, client):
        html = client.get('/r6-dashboard').data.decode()
        assert 'Run a fresh check now' in html
