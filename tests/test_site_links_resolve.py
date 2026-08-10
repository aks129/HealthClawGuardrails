"""Guard: every internal link on the site goes somewhere.

The pages that carry the project's credibility are the ones most damaged by a
dead link. `/security` tells an evaluator "Terms, section 5 is the governing
text" and sends them to an anchor; if that anchor has been renamed, the reader
lands at the top of a legal page and has to hunt. That is a small failure with
a large effect on someone deciding whether to trust the claims around it.

Two classes are checked:

  - `url_for('endpoint')` naming a route that does not exist. Flask raises at
    RENDER time, so a template nobody rendered in a test can ship broken. The
    site has pages that no test opens.
  - `url_for('endpoint') }}#anchor` where the target template has no element
    with that id. Flask cannot catch this at all — the URL is valid and the
    fragment is silently ignored by the browser.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

#: `{{ url_for('faq') }}#hipaa` and friends.
_LINK = re.compile(r"url_for\(\s*'([a-z_0-9]+)'\s*\)\s*}}(#[A-Za-z0-9_-]+)?")

#: Endpoint -> template it renders. Read from app.py so a new page cannot be
#: added to one and forgotten in the other.
_ROUTE = re.compile(
    r"@web_blueprint\.route\('(?P<rule>/[^']*)'\)\s*\n"
    r"def (?P<endpoint>\w+)\([^)]*\):(?P<body>(?:\n(?:    .*)?)+?)"
    r"(?=\n@|\n\ndef |\Z)")


def _endpoint_templates() -> dict[str, str]:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in _ROUTE.finditer(source):
        rendered = re.search(r"render_template\(\s*'([^']+)'", m.group("body"))
        if rendered:
            out[m.group("endpoint")] = rendered.group(1)
    return out


def _ids(template: str) -> set[str]:
    path = TEMPLATES / template
    if not path.is_file():
        return set()
    return set(re.findall(r'\bid="([^"]+)"', path.read_text(encoding="utf-8")))


def _templates() -> list[pathlib.Path]:
    return sorted(TEMPLATES.glob("*.html"))


def test_there_are_templates_and_routes_to_check():
    """A pass over an empty glob is not a pass."""
    assert len(_templates()) >= 8, "templates/ looks wrong"
    endpoints = _endpoint_templates()
    assert {"about", "security", "faq", "terms", "privacy"} <= set(endpoints), (
        f"could not read the route table out of app.py; found {sorted(endpoints)}")


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_internal_anchor_links_point_at_a_real_id(template: pathlib.Path):
    """MUTATION: change `#hipaa` in security.html to `#hippa` -> red."""
    endpoints = _endpoint_templates()
    source = template.read_text(encoding="utf-8")

    broken: list[str] = []
    for endpoint, fragment in _LINK.findall(source):
        target = endpoints.get(endpoint)
        if target is None:
            # An endpoint outside the web blueprint (r6_dashboard, skills_index
            # and friends live elsewhere). Route existence is Flask's job and
            # is covered by rendering every page in the suite; only the
            # fragment is checkable here.
            continue
        if not fragment:
            continue
        anchor = fragment.lstrip("#")
        if anchor not in _ids(target):
            broken.append(f"{fragment} -> {target} (no element with that id)")

    assert not broken, (
        f"{template.name} links to anchors that do not exist:\n  "
        + "\n  ".join(broken))
