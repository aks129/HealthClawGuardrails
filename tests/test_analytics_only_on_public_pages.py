"""Guard: Vercel Web Analytics loads on the public information pages only.

`base.html` wraps both the marketing pages and the dashboards, the command
center and the closed-tenant /connect page. Analytics records the page
address, and those app paths and query strings can carry tenant identifiers,
so the script is gated by a path allowlist in `base.html`.

The allowlist has one source, the tuple in `base.html`. This file reads it
from there, and checks /privacy section 7 against it.

What this file does not check: that Vercel serves the script, or what Vercel
does with what the script sends. Neither is decided in this repository.

MUTATION, each run against the committed tree:
  add '/command-center' to the tuple in base.html
      -> test_an_app_page_never_loads_analytics[command_center.html],
         test_no_app_path_is_allowlisted
  remove the `{% if %}` around the script in base.html
      -> every test_an_app_page_never_loads_analytics row
  remove the script from base.html
      -> every test_an_allowlisted_page_loads_analytics row
  remove the script from index.html -> test_the_landing_page_loads_analytics
  take the session away from the command-center row
      -> test_an_app_page_never_loads_analytics[command_center.html]
  add the script, single-quoted and without `defer`, to
  connect_patient_base.html
      -> test_no_other_template_references_analytics,
         test_the_live_connect_page_never_loads_analytics
  drop a page name from the list in privacy.html section 7
      -> test_privacy_names_the_allowlisted_pages
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from models import db
from r6.fasten.models import TenantClosure
from r6.read_auth import TENANT_SESSION_KEY

NEEDLE = "_vercel/insights"
SCRIPT = '<script defer src="/_vercel/insights/script.js"></script>'
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

# Printed by base.html only: neither standalone shell loads the vendored
# Bootstrap bundle. Seeing it means the response is a page in that shell and
# not an error body or a redirect.
BASE_SHELL_MARKER = "js/vendor/bootstrap.bundle.min.js"

TENANT = "analytics-guard-tenant"
CLOSED_TENANT = "analytics-guard-closed"

# Every template that extends base.html and is not a public information
# page: the URL that renders it, and words only that template prints. This
# list is written out, not derived, so that adding one of these paths to the
# allowlist fails here. A new template in the shell fails
# test_every_base_shell_template_is_public_or_app until it is on one list.
APP_PAGES = {
    "r6_dashboard.html": ("/r6-dashboard", "Guardrail conformance"),
    "fhir_control_panel.html": ("/fhir-control-panel", "FHIR Control Panel"),
    "command_center.html": ("/command-center",
                            'data-tenant="%s"' % TENANT),
    "command_center_login.html": (
        "/command-center/login",
        "The dashboard for private tenants is protected"),
    "fasten_connect_disconnected.html": ("/connect/" + CLOSED_TENANT,
                                         "Connection disconnected"),
}

# How /privacy section 7 names each page that loads the script.
PRIVACY_NAMES = {
    "home": "/",
    "about": "/about",
    "FAQ": "/faq",
    "wiki": "/wiki",
    "skills": "/skills",
    "security": "/security",
    "privacy": "/privacy",
    "terms": "/terms",
}


def _allowlist() -> tuple:
    """The paths base.html loads the script on, read from base.html."""
    found = re.findall(r"\{% if request\.path in (\([^)]*\)) %\}\s*"
                       + re.escape(SCRIPT) + r"\s*\{% endif %\}",
                       (TEMPLATES / "base.html").read_text(encoding="utf-8"))
    assert len(found) == 1, (
        "base.html must load the analytics script once, inside one "
        "`request.path in (...)` test")
    paths = ast.literal_eval(found[0])
    assert paths, "the allowlist in base.html is empty"
    return paths


def _extends_base() -> set:
    names = {path.name for path in TEMPLATES.rglob("*.html")
             if re.search(r"""\{%-?\s*extends\s+["']base\.html["']""",
                          path.read_text(encoding="utf-8"))}
    assert names, "no template extends base.html: this guard read nothing"
    return names


def _markup(path) -> str:
    """A template's text without its Jinja comments, which are never sent.

    review_base.html names the script in a comment about why it is absent.
    """
    return re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"),
                  flags=re.DOTALL)


def test_the_allowlist_has_one_source():
    """base.html references the script once, and only behind the path test."""
    _allowlist()
    assert _markup(TEMPLATES / "base.html").count(NEEDLE) == 1


def test_every_base_shell_template_is_public_or_app():
    """An allowlisted path /name is served from name.html."""
    public = {path.lstrip("/") + ".html" for path in _allowlist()}
    assert not public & set(APP_PAGES)
    assert _extends_base() == public | set(APP_PAGES)


def test_no_app_path_is_allowlisted():
    assert not set(_allowlist()) & {url for url, _ in APP_PAGES.values()}


@pytest.mark.parametrize("path", _allowlist())
def test_an_allowlisted_page_loads_analytics(client, path):
    response = client.get(path)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert BASE_SHELL_MARKER in body
    assert body.count(NEEDLE) == 1
    assert SCRIPT in body


@pytest.mark.parametrize("template", sorted(APP_PAGES))
def test_an_app_page_never_loads_analytics(client, template):
    url, words = APP_PAGES[template]
    if template == "command_center.html":
        # A private tenant's dashboard, which only a session opens. Without
        # one this URL answers with the demo tenant's.
        with client.session_transaction() as session:
            session[TENANT_SESSION_KEY] = TENANT
    if template == "fasten_connect_disconnected.html":
        db.session.add(TenantClosure(tenant_id=CLOSED_TENANT))
        db.session.commit()

    response = client.get(url)
    body = response.get_data(as_text=True)

    # The page itself, in the shell that carries the script: an error body
    # or a redirect also lacks the script, and would pass the last line.
    assert response.status_code == 200
    assert BASE_SHELL_MARKER in body
    assert words in body
    assert NEEDLE not in body


def test_the_landing_page_loads_analytics(client):
    """index.html is its own shell and carries the script itself."""
    response = client.get("/")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert BASE_SHELL_MARKER not in body
    assert body.count(NEEDLE) == 1
    assert SCRIPT in body


def test_the_live_connect_page_never_loads_analytics(client):
    """/connect/<tenant> for an open tenant: the tenant id is in the path."""
    response = client.get("/connect/" + TENANT)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert TENANT in body
    assert NEEDLE not in body


def test_no_other_template_references_analytics():
    """Every template but the two that load it, however the tag is spelled."""
    others = [path for path in TEMPLATES.rglob("*.html")
              if path.name not in ("base.html", "index.html")]
    assert len(others) >= 20, "the template glob matched too few files"
    assert [path.name for path in others if NEEDLE in _markup(path)] == []
    for name in ("base.html", "index.html"):
        assert _markup(TEMPLATES / name).count(NEEDLE) == 1


def test_privacy_names_the_allowlisted_pages():
    """Section 7 lists the pages that load the script, and no others."""
    text = " ".join(
        (TEMPLATES / "privacy.html").read_text(encoding="utf-8").split())
    found = re.findall(r"The public information pages \(([^)]*)\) load "
                       r"Vercel Web Analytics", text)
    assert len(found) == 1, "section 7 no longer lists the pages"
    names = re.split(r", | and ", found[0])
    assert sorted(PRIVACY_NAMES[name] for name in names) == sorted(
        _allowlist() + ("/",))
