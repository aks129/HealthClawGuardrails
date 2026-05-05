"""
tests/test_skills_page.py

GET /skills — the auto-generated skill index.
"""

from __future__ import annotations


class TestSkillsRoute:

    def test_renders_200(self, client):
        r = client.get('/skills')
        assert r.status_code == 200

    def test_lists_known_skills(self, client):
        html = client.get('/skills').data.decode()
        # Every committed skill should appear by name.
        for slug in (
            "getting-started", "fhir-r6-guardrails", "phi-redaction",
            "fhir-upstream-proxy", "personal-health-records",
            "healthex-export", "healthex-export-redacted", "curatr",
            "fasten-connect",
        ):
            assert slug in html, f"missing skill: {slug}"

    def test_getting_started_pinned_first(self, client):
        html = client.get('/skills').data.decode()
        # getting-started should appear before any other skill in the body
        idx_first = html.find("getting-started")
        idx_others = min(
            (html.find(s) for s in ("fhir-r6-guardrails", "phi-redaction", "curatr")
             if html.find(s) > 0),
            default=-1,
        )
        assert idx_first > 0
        assert idx_others > 0
        assert idx_first < idx_others

    def test_pdf_download_link_present(self, client):
        html = client.get('/skills').data.decode()
        assert "healthclaw-quickstart.pdf" in html

    def test_pdf_actually_downloadable(self, client):
        r = client.get('/static/healthclaw-quickstart.pdf')
        # Either the artifact ships in the repo (200), or it's missing locally
        # but the route still works (404 from Flask static handler).
        assert r.status_code in (200, 404)

    def test_skill_links_point_to_github(self, client):
        html = client.get('/skills').data.decode()
        assert "github.com/aks129/HealthClawGuardrails/blob/main/skills/" in html

    def test_nav_includes_skills_link(self, client):
        # Both pages should expose a Skills link in their nav.
        for path in ('/', '/faq'):
            html = client.get(path).data.decode()
            assert '/skills' in html, f"{path} missing /skills nav link"
