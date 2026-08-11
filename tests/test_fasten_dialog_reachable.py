"""Guard: the Fasten consent dialog must be reachable on a short viewport.

Reported from an iPad: the Fasten dialog rendered with its policy-agreement
checkbox and Continue button below the fold, and nothing would scroll to them.
A patient could start the connect flow and simply not be able to finish it.

Two independent causes, one per surface, and both are a container clipping a
child that is taller than it:

- ``templates/fasten_connect.html`` sized the modal ``height: min(640px, 90vh)``
  with ``overflow: hidden``. A fixed height cannot shrink, ``overflow: hidden``
  removes the scrollbar that would have rescued it, and iOS measures ``vh``
  against the viewport with browser chrome EXPANDED, so 90vh regularly exceeds
  what is actually visible. On top of that, iOS and iPadOS expand an iframe to
  its content height and will not scroll it internally whatever the iframe's
  own overflow says — so the scroll container has to be a wrapper element.
- ``templates/r6_dashboard.html`` gave ``#stitch-container`` ``overflow: hidden``
  for a rounded corner. ``<fasten-stitch-element>`` renders its consent dialog
  inside that element, so the corner radius clipped the dialog.

These assert the source, not a rendered layout, because the widget itself is a
third-party bundle we do not control and cannot render in CI. That is a real
limit and it is stated in each test: this catches the regression shape we hit,
not every possible way a dialog can be cut off.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONNECT = REPO_ROOT / 'templates' / 'fasten_connect.html'
DASHBOARD_CSS = REPO_ROOT / 'static' / 'css' / 'r6-dashboard.css'
DASHBOARD_HTML = REPO_ROOT / 'templates' / 'r6_dashboard.html'


_CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)


def _connect_modal_css() -> str:
    """The .fasten-modal rule body, comments stripped.

    Stripping matters: these rules carry comments that quote the OLD broken
    declarations to explain the fix, and a naive search finds the quote and
    reports the bug as still present. A guard that reads its own documentation
    as evidence is not a guard.
    """
    src = CONNECT.read_text(encoding='utf-8')
    match = re.search(r'\.fasten-modal\s*\{(.*?)\}', src, re.S)
    assert match, '.fasten-modal rule not found — did the modal move?'
    return _CSS_COMMENT.sub('', match.group(1))


def test_the_connect_modal_is_not_a_fixed_height_that_cannot_shrink():
    """MUTATION: restore `height: min(640px, 90vh)` and drop max-height -> red.

    A dialog that cannot shrink below its content on a short viewport puts its
    own submit control off-screen.
    """
    body = _connect_modal_css()
    assert 'max-height' in body, (
        '.fasten-modal has no max-height, so it cannot shrink to fit a short '
        'viewport — the iPad clip')
    assert not re.search(r'height:\s*min\(\s*640px\s*,\s*90vh\s*\)', body), (
        'the fixed 90vh height is back; iOS measures vh against the '
        'chrome-expanded viewport, so it overflows what is visible')


def test_the_connect_modal_scrolls_in_a_wrapper_not_the_iframe():
    """iOS/iPadOS will not scroll an iframe internally.

    MUTATION: delete .fasten-modal-body (or its overflow-y) -> red.
    """
    src = CONNECT.read_text(encoding='utf-8')
    assert 'fasten-modal-body' in src, (
        'the iframe scroll wrapper is gone; an iframe alone does not scroll on '
        'iOS, so the bottom of the widget becomes unreachable')
    match = re.search(r'\.fasten-modal-body\s*\{(.*?)\}', src, re.S)
    assert match, '.fasten-modal-body rule not found'
    rule = match.group(1)
    assert 'overflow-y: auto' in rule, (
        '.fasten-modal-body must scroll; without it the wrapper clips exactly '
        'like the modal used to')
    assert 'min-height: 0' in rule, (
        'a flex child defaults to min-height:auto and refuses to shrink, which '
        're-creates the clip even with overflow-y set')


def test_the_iframe_is_taller_than_its_scroll_container():
    """The declaration that decides whether the scroll container can scroll.

    The first fix for this bug set `height: 100%` on the iframe. That makes it
    exactly fill .fasten-modal-body, so the wrapper never overflows, its
    `overflow-y: auto` never engages, and the widget stays clipped — iOS will
    not scroll an iframe's internals, which is the only reason the wrapper is
    there. The dialog shipped still broken and the earlier version of this file
    passed, because it asserted that a scroll container EXISTED rather than
    that scrolling could ever HAPPEN.

    A scroll container whose only child always fits is not a scroll container.

    MUTATION: set the iframe back to `height: 100%` (or any percentage) -> red.
    """
    src = CONNECT.read_text(encoding='utf-8')
    match = re.search(r'\.fasten-modal-body iframe\s*\{(.*?)\}', src, re.S)
    assert match, '.fasten-modal-body iframe rule not found'
    rule = _CSS_COMMENT.sub('', match.group(1))

    height = re.search(r'(?<!min-)(?<!max-)height:\s*([^;]+);', rule)
    assert height, 'the iframe declares no height, so its size is content-'\
                   'driven and iOS will clip it'
    value = height.group(1).strip()
    assert '%' not in value, (
        f'the iframe height is {value!r}: a percentage resolves against '
        '.fasten-modal-body, so the iframe can never exceed it and the wrapper '
        'can never scroll. Use an absolute height taller than the tallest '
        'phone/tablet viewport.')
    px = re.match(r'(\d+)px$', value)
    assert px and int(px.group(1)) >= 900, (
        f'the iframe height is {value!r}; it must be an absolute height of at '
        'least 900px so the wrapper overflows and scrolls on a phone-sized '
        'viewport, which is where this was reported')


def test_the_iframe_is_inside_the_scroll_wrapper():
    """The wrapper only helps if the iframe is actually in it."""
    src = CONNECT.read_text(encoding='utf-8')
    match = re.search(
        r'<div class="fasten-modal-body">(.*?)</div>', src, re.S)
    assert match, 'fasten-modal-body element not found in the markup'
    assert 'stitch-iframe' in match.group(1), (
        'the Fasten iframe is not inside .fasten-modal-body, so nothing '
        'scrolls it')


def test_the_close_control_meets_the_tap_target_minimum():
    """design.md: minimum tap target 44px. A 22px glyph is not a 44px target,
    and this dialog is the one a stuck patient reaches for.

    MUTATION: drop width/height from .fasten-modal-close -> red.
    """
    src = CONNECT.read_text(encoding='utf-8')
    match = re.search(r'\.fasten-modal-close\s*\{(.*?)\}', src, re.S)
    assert match, '.fasten-modal-close rule not found'
    rule = match.group(1)
    assert re.search(r'width:\s*44px', rule), (
        'the close control is narrower than the 44px minimum in design.md')
    assert re.search(r'height:\s*44px', rule), (
        'the close control is shorter than the 44px minimum in design.md')


def test_no_template_embeds_the_raw_stitch_web_component():
    """MUTATION: add a live <fasten-stitch-element> to any template -> red.

    This pin MOVED rather than being deleted, and what it guards changed with
    it. It used to check that /r6-dashboard's #stitch-container did not clip
    its own consent dialog. That container is gone — /r6-dashboard is a
    conformance report now — and the panel it lived in was the weaker of the
    two Fasten embeds anyway: it was not bound to a tenant, while
    /connect/<tenant_id> is.

    The surviving embed does NOT use the web component. It renders the widget
    in an iframe inside a scrolling wrapper, because iOS and iPadOS expand an
    iframe to its content height and refuse to scroll it internally — the
    reasoning is written up beside .fasten-modal-body, and the five tests
    above pin it. The raw <fasten-stitch-element> is the shape that had the
    clipping bug and has none of that handling, so bringing one back is a
    regression even on a page nobody has looked at yet.

    Escaped occurrences in documentation (wiki.html shows the tag as sample
    markup with &lt;) are not embeds and do not match.
    """
    offenders = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / 'templates').rglob('*.html')
        if '<fasten-stitch-element' in p.read_text(encoding='utf-8')
    )
    assert offenders == [], (
        'the Stitch web component is embedded live in '
        f'{offenders}; the supported embed is the iframe on '
        'templates/fasten_connect.html, which handles the iOS scroll case')
