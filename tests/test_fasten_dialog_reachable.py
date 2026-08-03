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


def test_the_dashboard_stitch_container_does_not_clip_its_dialog():
    """MUTATION: put `overflow: hidden` back on .stitch-container -> red.

    <fasten-stitch-element> renders its consent dialog inside this element, so
    clipping it for a rounded corner hides the checkbox and Continue button.
    """
    css = DASHBOARD_CSS.read_text(encoding='utf-8')
    match = re.search(r'\.stitch-container\s*\{(.*?)\}', css, re.S)
    assert match, '.stitch-container rule not found in r6-dashboard.css'
    assert 'overflow: hidden' not in match.group(1), (
        '.stitch-container clips its own child dialog again')

    html = DASHBOARD_HTML.read_text(encoding='utf-8')
    container = re.search(r'<div id="stitch-container"[^>]*>', html)
    assert container, 'stitch-container element not found'
    assert 'overflow:hidden' not in container.group(0).replace(' ', ''), (
        'the clip came back as an inline style, which beats the stylesheet')
