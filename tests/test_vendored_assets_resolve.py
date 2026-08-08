"""Guard: every url() in a stylesheet we serve must actually resolve.

The CSP is `default-src 'self'` and every third-party asset is vendored under
`static/` and `careagents/static/`. That closes a privacy hole — no page a
patient opens announces itself to Google or Cloudflare — but it also removes
the safety net that hid path mistakes.

This bug shipped once, in the change that vendored the assets:

    static/css/vendor/fontawesome.min.css
    upstream:  url(../webfonts/fa-solid-900.woff2)
    resolves:  static/css/webfonts/...   404
    actual:    static/webfonts/...

Every icon on the dashboards silently fell back to a placeholder glyph. It was
invisible to the Flask test client, which renders HTML and never fetches a
subresource, and invisible to CI, which does the same. Only a real browser
asked for the file.

The vendoring script has its own check now, but that check runs only when
someone re-vendors. This runs on every commit, and it covers every stylesheet
rather than the one that happened to break — a missing font degrades to a
fallback rather than an error, so nothing else in the stack reports it.

Not covered here: whether the FILE is a valid font. That needs a browser, and
`e2e/` is where a browser lives.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Static trees whose stylesheets are served to a browser.
STATIC_ROOTS = (ROOT / "static", ROOT / "careagents" / "static")

#: url(...) with optional quotes, ignoring data: and absolute URLs.
_URL_REF = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""")

_SKIP_SCHEMES = ("data:", "http://", "https://", "//", "#")


def _stylesheets() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in STATIC_ROOTS:
        if root.is_dir():
            out.extend(sorted(root.rglob("*.css")))
    return out


def _unresolved(css_path: pathlib.Path) -> list[str]:
    """Refs that do not resolve, resolved the way a browser does.

    Relative to the STYLESHEET's own directory — not the static root, and not
    the page that links it. Getting that wrong is the bug this file exists
    for; the first version of the vendoring check took the directory as an
    argument and so could not see it.
    """
    text = css_path.read_text(encoding="utf-8", errors="replace")
    bad: list[str] = []
    for ref in _URL_REF.findall(text):
        ref = ref.strip()
        if not ref or ref.startswith(_SKIP_SCHEMES):
            continue
        target = ref.split("?")[0].split("#")[0]
        if target.startswith("/"):
            # Root-relative: resolved against the repo root, which is where
            # both apps mount their static trees (/static/..., and CareAgents'
            # own /static/... under careagents/).
            resolved = ROOT / target.lstrip("/")
        else:
            resolved = css_path.parent / target
        if not resolved.is_file():
            bad.append(f"{ref} -> {resolved}")
    return bad


def test_there_are_stylesheets_to_check():
    """A pass because the glob found nothing is not a pass.

    Without this, moving or renaming the static tree turns every assertion
    below into a vacuous loop over an empty list, and the suite goes green on
    a codebase where nothing was verified.
    """
    sheets = _stylesheets()
    assert len(sheets) >= 5, f"expected the vendored stylesheets, found {sheets}"
    names = {p.name for p in sheets}
    assert "fontawesome.min.css" in names, "the sheet that broke is not covered"
    assert "fonts.css" in names, "the self-hosted webface sheet is not covered"


@pytest.mark.parametrize("css", _stylesheets(), ids=lambda p: p.name)
def test_every_url_reference_resolves(css: pathlib.Path):
    """MUTATION: rewrite fontawesome's url() prefix to ../webfonts/ -> red."""
    bad = _unresolved(css)
    assert not bad, (
        f"{css.relative_to(ROOT)} references files that do not exist. Under "
        f"default-src 'self' these 404 instead of falling through to a CDN, "
        f"and a missing font renders as a placeholder rather than an error:\n  "
        + "\n  ".join(bad))
