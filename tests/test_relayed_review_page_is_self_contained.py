"""Guard: a page relayed to another origin may not reference that origin's assets.

`action_review.html` is fetched server-side by CareAgents and served from
careagents.cloud. It extended the site's `base.html`, which sent three things
across that boundary that only resolve on HealthClaw (#396):

  - `url_for('static', filename='css/r6-dashboard.css')` -> the absolute path
    /static/css/r6-dashboard.css. `careagents/static/` has no such file, so
    the patient got an UNSTYLED page.
  - a HealthClaw navbar — Home, Dashboard, FAQ, Wiki, Skills — five links that
    404 on careagents.cloud.
  - /_vercel/insights/script.js, not served there either.

All three at the moment a patient is asked to approve a clinical form. Beyond
cosmetics: someone who cannot tell whose page they are on is being asked to
attest on a surface that does not look like the product they signed into.

These assert the SOURCE. The relay's own tests fake the page with a one-line
HTML string (`tests/test_careagents.py`), so nothing there can see this — the
repo trap where a fake proves a call is made, not that the result is usable.
Rendering the real template through CareAgents in CI would need both apps up;
this catches the regression shape instead, which is a template edit
reintroducing a same-origin reference.
"""

from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

#: Pages served from an origin other than the one that renders them.
RELAYED_PAGES = ("action_review.html",)

#: The shell they must use. `base.html` is the site chrome and is correct for
#: every page NOT relayed.
RELAY_SHELL = "review_base.html"


#: Jinja comments, JS line and block comments. Scans must run over CODE, not
#: over the paragraph explaining why the code is the way it is — three
#: separate guards written today first failed against a CORRECT file because
#: their own rationale named the thing they forbid.
_COMMENTS = re.compile(r"{#.*?#}|/\*.*?\*/|^\s*//.*$", re.S | re.M)


def _read(name: str) -> str:
    """Template source with comments stripped."""
    raw = (TEMPLATES / name).read_text(encoding="utf-8")
    return _COMMENTS.sub("", raw)


def _rendered_chain(name: str) -> str:
    """The page plus whatever shell it extends — what the patient receives."""
    source = _read(name)
    match = re.search(r'{%\s*extends\s+"([^"]+)"', source)
    return source + (_read(match.group(1)) if match else "")


def test_the_review_page_uses_the_relay_shell():
    """MUTATION: extend base.html again -> red, and every check below with it."""
    pattern = r'{%\s*extends\s+"' + re.escape(RELAY_SHELL) + '"'
    assert re.search(pattern, _read("action_review.html")), (
        "action_review.html must extend review_base.html; base.html carries "
        "a navbar and a same-origin stylesheet that only exist on HealthClaw")


def test_no_relayed_page_references_a_same_origin_asset():
    """`url_for('static', ...)` renders an absolute path that 404s on the
    relayed origin. A CDN URL is fine — it resolves identically from either
    host. The defect was never "a CDN", it was a path that exists on one host.
    """
    for page in RELAYED_PAGES:
        chain = _rendered_chain(page)
        assert "url_for('static'" not in chain and 'url_for("static"' not in chain, (
            f"{page} references a same-origin static asset; it is served from "
            f"careagents.cloud, where that path does not exist")
        assert "/_vercel/" not in chain, (
            f"{page} references a Vercel-only script path")


def test_no_relayed_page_shows_another_products_navigation():
    """Five dead links and someone else's brand, mid-approval."""
    for page in RELAYED_PAGES:
        chain = _rendered_chain(page)
        assert "navbar" not in chain, (
            f"{page} carries a navbar; on the relayed origin its links 404, "
            f"and it tells the patient they are on a different product")
        for endpoint in ("'index'", "'r6_dashboard'", "'faq'", "'wiki'",
                         "'skills_index'"):
            assert "url_for(" + endpoint + ")" not in chain, (
                f"{page} links to url_for({endpoint}), which has no route on "
                f"careagents.cloud")


def test_the_shell_still_provides_the_layout_the_page_uses():
    """Stripping chrome must not strip the CSS the page's markup depends on.

    The page is built from Bootstrap classes (card, alert, btn-group, row).
    Removing Bootstrap along with the nav would trade a broken nav for a
    broken page — the same defect with better intentions.
    """
    shell = _read(RELAY_SHELL)
    assert "bootstrap" in shell.lower(), (
        "the relay shell must still load Bootstrap; action_review.html is "
        "built from its classes")
    assert "{% block content %}" in shell and "{% block scripts %}" in shell, (
        "the shell must provide the blocks action_review.html fills")
