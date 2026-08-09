"""Guard: a page relayed to another origin must fetch nothing at all.

`action_review.html` is fetched server-side by CareAgents and served from
careagents.cloud. That puts it between two rules which look contradictory
until you satisfy both:

  - **No same-origin asset** (#396). Extending the site's `base.html` sent
    `url_for('static', ...)` -> /static/css/r6-dashboard.css, a five-link
    HealthClaw navbar, and /_vercel/insights/script.js across the boundary.
    None resolve on careagents.cloud, so the patient got an unstyled page
    wearing another product's dead navigation at the moment they were asked
    to attest.

  - **No third-party asset.** The CSP in app.py is `default-src 'self'`. The
    first fix for #396 reached for Bootstrap and Font Awesome on CDNs, and
    the lint gate rejected it.

The only thing that satisfies both is a page that fetches nothing, so
`review_base.html` inlines its styles and uses a system font stack.

That has a cost worth pinning. The inlined block reimplements, by hand, the
subset of Bootstrap class names the page uses — the names were kept so that
fixing this did not mean rewriting every attribute on a patient-facing
clinical form. Bootstrap is NOT loaded, so a class nobody implemented renders
as nothing: a card with no border, a button with no tap target. Silent, and
only on the origin no test previously looked at.
`test_every_class_the_page_uses_is_defined_by_the_shell` is what stops that.

These assert the SOURCE. The relay's own tests fake the page with a one-line
HTML string (`tests/test_careagents.py`), so nothing there can see this — the
repo trap where a fake proves a call is made, not that the result is usable.
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

#: Classes that exist for behaviour, not appearance — JavaScript hooks in
#: action_review.html. They are deliberately unstyled, so the coverage check
#: below must not demand a rule for them.
BEHAVIOUR_ONLY = {"med-choice", "allergy-choice"}

#: Jinja comments, JS line and block comments. Scans must run over CODE, not
#: over the paragraph explaining why the code is the way it is — three
#: separate guards written for this change first failed against a CORRECT
#: file because their own rationale named the thing they forbid.
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
    relayed origin."""
    for page in RELAYED_PAGES:
        chain = _rendered_chain(page)
        assert "url_for('static'" not in chain and 'url_for("static"' not in chain, (
            f"{page} references a same-origin static asset; it is served from "
            f"careagents.cloud, where that path does not exist")
        assert "/_vercel/" not in chain, (
            f"{page} references a Vercel-only script path")


def test_the_relayed_page_fetches_nothing_at_all():
    """MUTATION: re-add the Bootstrap <link> -> red.

    The earlier version of this file asserted the OPPOSITE — that the shell
    must load Bootstrap — because a CDN was then the only way to style a page
    that could not use same-origin paths. The CSP tightened to
    `default-src 'self'` when the assets were vendored, so that assertion
    became a pin on a rule that no longer holds. It moved with the thing it
    pins, in the same change.
    """
    for page in RELAYED_PAGES:
        chain = _rendered_chain(page)
        assert "<link" not in chain.lower(), (
            f"{page} carries a <link>; a relayed page must fetch nothing — "
            f"same-origin paths 404 there and the CSP forbids third parties")
        assert not re.search(r"<script[^>]+\bsrc\s*=", chain, re.I), (
            f"{page} loads an external script")
        assert not re.search(r"url\(\s*['\"]?(?!data:)[^)]", chain), (
            f"{page} has a CSS url() — inline the asset or drop it")
        assert "@import" not in chain, f"{page} uses @import"


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


def test_every_class_the_page_uses_is_defined_by_the_shell():
    """The load-bearing one, because Bootstrap is no longer loaded.

    The shell hand-implements the Bootstrap class names this page uses. A name
    it missed does not fall back to anything — `.card` with no rule is a plain
    div, `.btn` with no rule is unstyled text with no 44px target, on a
    clinical form a patient is signing. Nothing else in the stack would report
    it: the relay's tests fake the page, and CI renders HTML without applying
    CSS.

    MUTATION: add `class="table-responsive"` to action_review.html -> red.
    """
    shell = _read(RELAY_SHELL)
    style = "\n".join(re.findall(r"<style>(.*?)</style>", shell, re.S))
    assert style.strip(), "the relay shell has no inlined stylesheet"

    page = _read("action_review.html")
    used: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', page):
        for token in attr.split():
            if "{{" in token or "{%" in token or not token:
                continue
            used.add(token)

    undefined = sorted(
        tok for tok in used
        if tok not in BEHAVIOUR_ONLY
        and not re.search(r"\." + re.escape(tok) + r"(?![\w-])", style)
    )
    assert not undefined, (
        "action_review.html uses classes review_base.html does not define, and "
        "Bootstrap is not loaded, so they style nothing:\n  "
        + "\n  ".join(undefined)
        + "\nAdd a rule to the shell's inlined <style>, or stop using the class.")


def test_the_shell_keeps_the_blocks_the_page_fills():
    """Stripping chrome must not strip the seams the page renders into."""
    shell = _read(RELAY_SHELL)
    assert "{% block content %}" in shell and "{% block scripts %}" in shell, (
        "the shell must provide the blocks action_review.html fills")


def _rule_body(style: str, selector: str) -> str:
    """The declarations of the rule whose selector list starts with `selector`."""
    match = re.search(
        r"(?:^|\})\s*" + re.escape(selector) + r"(?![\w-])[^{}]*\{([^}]*)\}",
        style, re.M)
    return match.group(1) if match else ""


def test_the_buttons_clear_the_44px_tap_target():
    """design.md: 44px minimum, phone-first, one-handed.

    Bound to the `.btn` rule specifically. The first version of this asserted
    `"min-height: 44px" in style`, which passed even with `.btn` shrunk to
    30px — `.form-check` also declares 44px, so the substring was satisfied by
    a different rule. It checked that the number appears somewhere, not that
    the buttons have it.
    """
    style = _read(RELAY_SHELL)
    body = _rule_body(style, ".btn")
    assert body, "the relay shell defines no .btn rule"
    match = re.search(r"min-height:\s*(\d+)px", body)
    assert match and int(match.group(1)) >= 44, (
        f".btn min-height is {match.group(1) + 'px' if match else 'unset'}; "
        f"design.md sets a 44px floor, and on this page the small variant is "
        f"the still-taking / not-anymore / remove control")

    # .btn-sm must not claw it back.
    sm = _rule_body(style, ".btn-sm")
    shrunk = re.search(r"min-height:\s*(\d+)px", sm)
    assert not (shrunk and int(shrunk.group(1)) < 44), (
        ".btn-sm lowers the tap target below 44px")


def test_no_text_on_the_relayed_page_falls_under_16px():
    """iOS zooms the whole page when a control under 16px is focused, and this
    page carries medication and allergy provenance read under stress."""
    style = _read(RELAY_SHELL)
    assert not re.search(r"font-size:\s*(?:1[0-5]px|0\.[0-9]+rem)", style), (
        "the relay shell sets a control or body size under 16px")


# --- rendered output, not source -------------------------------------------

def _render_review_page() -> str:
    """The page as the patient receives it. Synthetic data only."""
    import main  # noqa: PLC0415 — importing at module scope boots the app

    with main.app.app_context():
        return main.app.jinja_env.get_template("action_review.html").render(
            action_id="act-test-0001",
            record_readable=True,
            record_reason=None,
            demographics=[("Name", "Grover Keeling"),
                          ("Date of birth", "1955-04-02")],
            meds=[{"name": "Lisinopril 10 mg tablet", "dose": "1 tablet daily"}],
            allergies=[{"allergen": "Penicillin", "reaction": "Hives"}],
            conditions=[{"name": "Type 2 diabetes mellitus"}],
        )


def test_no_template_internals_reach_the_page():
    """MUTATION: write a literal comment-close inside the header comment -> red.

    Every other check in this file reads SOURCE. This one existed nowhere, and
    the bug it catches shipped into a render: `review_base.html`'s header
    comment explained how it closes onto the doctype and, in doing so, wrote
    the closing delimiter as prose. Jinja closed the comment there, and the
    remaining two lines of explanation rendered as the first visible text on a
    clinical consent form.

    Reading the template could not see it — the source was exactly what was
    intended. Only the output was wrong. It is the same shape as the three
    source-scanning guards in this change that first failed against correct
    files because their own rationale named the thing they forbid: prose about
    a delimiter, sitting inside that delimiter.
    """
    html = _render_review_page()

    assert html.lstrip().startswith("<!DOCTYPE html>"), (
        f"the page does not begin with its doctype; something leaks ahead of "
        f"it: {html[:120]!r}")
    for delimiter in ("{#", "#}", "{%", "{{"):
        assert delimiter not in html, (
            f"{delimiter!r} survives into the rendered page — a template "
            f"delimiter written as prose closes the construct it describes")


def test_the_rendered_page_requests_nothing():
    """Source says it fetches nothing; confirm the render agrees.

    A `{% block head %}` filled by a child, or an inherited block, could add a
    request that no source scan of these two files would see.
    """
    html = _render_review_page()

    assert "<link" not in html.lower(), "the rendered page carries a <link>"
    assert not re.search(r"<script[^>]+\bsrc\s*=", html, re.I), (
        "the rendered page loads an external script")
    assert not re.search(r"url\(\s*['\"]?(?!data:)[^)]", html), (
        "the rendered page has a CSS url()")
    # Protocol-relative (//host/x.css) is a fetch the checks above can miss,
    # since neither "<link" nor "src=" need be present for @import-less CSS
    # or a rewritten attribute.
    assert not re.search(r"""['"(]//[a-z0-9-]+\.""", html, re.I), (
        "the rendered page has a protocol-relative URL")
