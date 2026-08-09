"""Guard: the clinical review page must not ship a write credential.

`templates/action_review.html` rendered a tenant-bound step-up token as a
JavaScript literal, and `careagents/app.py` relays that HTML verbatim from
careagents.cloud. Anything that could read the page body held a live WRITE
credential — the gate on clinical writes — until it expired, across an origin
boundary, at the approval moment (#395).

It contradicted `careagents/healthclaw.py`'s own stated invariant: that the
client carries credentials the browser never sees.

Deleting it costs nothing, and the scoping matters more than the deletion:

  - the GET at `r6/actions/review.py:130` REQUIRES the step-up header and 401s
    without it. A browser navigation cannot set a custom header, so no browser
    ever loaded this page directly, and the literal rendered as "" on any
    direct request.
  - the only caller supplying that header is CareAgents' server-side
    `fetch_review_page` — the path where the token got baked into HTML and
    shipped onward.
  - on submit, `careagents/app.py` rewrites the form action to
    `/review/<agent>/<action>/submit`, resolves the tenant from ownership, and
    re-injects credentials itself, reading neither browser-supplied header.

So the credential was populated only on the path that did not need it.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import r6.actions.review as review_module

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "action_review.html")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_page_renders_no_step_up_token():
    """MUTATION: restore `var TOKEN = {{ step_up_token | tojson }}` -> red."""
    assert not re.search(r"\{\{\s*step_up_token", _template()), (
        "action_review.html interpolates step_up_token; a tenant-bound write "
        "credential must never reach the page body")


def test_the_renderer_does_not_hand_the_token_to_the_template():
    """The template is one half; the render call is the other.

    Checking only the template would leave a page that stopped USING the token
    while the server still handed it over — one edit from regressing.
    """
    assert "step_up_token=" not in inspect.getsource(review_module), (
        "review.py passes step_up_token into render_template; the credential "
        "must not leave the server at all")


def test_no_request_header_is_built_from_a_page_credential():
    """MUTATION: re-add 'X-Step-Up-Token': TOKEN to the fetch headers -> red.

    Match the header being SET as an object key, not merely named. The comment
    above the fetch explains why these were removed and names them, so a bare
    substring scan flags that comment and fails on a correct file — the check
    has to look at the code shape, not the vocabulary.
    """
    source = _template()
    for header in ("X-Step-Up-Token", "X-Tenant-Id"):
        assert not re.search(r"['\"]%s['\"]\s*:" % re.escape(header), source), (
            f"the page sets {header} from a literal; the relay resolves the "
            f"tenant from ownership and injects credentials server-side, so "
            f"this header is both unread and an exposure")


def test_the_submit_still_authenticates_by_session():
    """Deleting credentials must not leave the POST unauthenticated.

    The relay is `@login_required` and the form posts same-origin to it, so
    the browser's session cookie is the credential. If that ever stops being
    true the fix is a server-side one — never putting the token back.
    """
    assert "credentials: 'same-origin'" in _template(), (
        "the submit must send the session cookie, which is what authorises "
        "the relay now that no token travels in the page")


def test_the_form_still_targets_the_path_the_relay_rewrites():
    """`careagents/app.py` rewrites this exact action string. If the template
    changes it the rewrite silently stops matching, the browser posts straight
    at HealthClaw with no credentials, and the patient gets a 401 at the
    approval moment — from a template edit."""
    assert '/r6/actions/{{ action_id }}/review' in _template(), (
        "the form action must stay the string careagents/app.py rewrites")
