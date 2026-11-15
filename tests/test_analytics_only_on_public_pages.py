"""Guard: Vercel Web Analytics loads on the public information pages only.

`base.html` wraps both the marketing pages and the dashboards, the command
center and /connect. Analytics records the page path, and those app paths and
query strings can carry tenant identifiers, so the script is gated by an
explicit path allowlist. /privacy section 7 names the same pages; if this
list changes, change that text too.
"""

from __future__ import annotations

import pathlib

import pytest

SCRIPT = "/_vercel/insights/script.js"
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

PUBLIC = ("/", "/about", "/faq", "/wiki", "/security", "/privacy", "/terms", "/skills")
PRIVATE = ("/command-center", "/r6-dashboard", "/fhir-control-panel")
SELF_CONTAINED = (
    "review_base.html",
    "connect_patient_base.html",
    "demo_intake_form.html",
)


@pytest.mark.parametrize("path", PUBLIC)
def test_public_page_loads_analytics(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.data.decode().count(SCRIPT) == 1


@pytest.mark.parametrize("path", PRIVATE)
def test_app_page_never_loads_analytics(client, path):
    r = client.get(path, follow_redirects=True)
    assert SCRIPT not in r.data.decode()


@pytest.mark.parametrize("name", SELF_CONTAINED)
def test_patient_facing_shell_never_references_analytics(name):
    assert "<script defer src=\"/_vercel/" not in (TEMPLATES / name).read_text()
