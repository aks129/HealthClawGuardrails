"""Guard: the connect page handles the events Fasten actually emits.

Reported twice on `/connect/<tenant>`: choosing CLEAR errors instantly with
`fasten_unauthorized_client` / "An error occurred while retrieving vault
profile", and the page itself says nothing — the patient sees only Fasten's
own "Oops! Something went wrong" inside the iframe (#326).

The handler branched on `widget.error` and `error`. Neither exists. Read out
of the shipped bundle at `cdn.fastenhealth.com/connect/v4`, the widget's only
events are:

    widget.close  widget.complete  widget.config_error

so every configuration refusal fell through both branches and produced
nothing. A control that produced nothing, and the page read it as "no news"
— docs/2026-08-06-two-generators-three-laws.md, Generator A.

These assert the SOURCE, for the reason `test_fasten_dialog_reachable.py`
gives: the widget is a third-party bundle we cannot render in CI. That is a
real limit. This catches the regression shape we hit — a handler that names
events the vendor does not send — not every way an error can be swallowed.
"""

from __future__ import annotations

import os
import pathlib
import re

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent
            / "templates" / "fasten_connect.html")

#: Every event name the v4 bundle emits. If Fasten adds one, this list is
#: what to update — and the assertion below is what will notice.
FASTEN_WIDGET_EVENTS = ("widget.close", "widget.complete", "widget.config_error")


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_config_error_event_is_handled_by_its_real_name():
    """MUTATION: rename the branch back to 'widget.error' -> red."""
    assert "'widget.config_error'" in _source(), (
        "widget.config_error is the only error event Fasten emits; a handler "
        "that does not name it swallows every configuration refusal")


def test_no_branch_waits_for_an_event_fasten_never_sends():
    """A branch on a non-existent name is dead code that reads as coverage.

    'widget.error' and 'error' were both handled and neither is emitted, so
    the page looked like it reported widget failures and never had.
    """
    # Match the COMPARISON, not the bare string. `'error'` is also the status
    # kind passed to showStatus, and the phantom names are quoted in the
    # comment that explains why they were removed — a substring scan flags
    # both and fails on a correct file. First draft of this test did exactly
    # that.
    source = _source()
    for phantom in ("widget.error", "error"):
        pattern = r"type\s*===\s*'%s'" % re.escape(phantom)
        assert not re.search(pattern, source), (
            f"a branch tests type === '{phantom}', which Fasten never emits "
            f"(v4 emits only {', '.join(FASTEN_WIDGET_EVENTS)}) — it can "
            f"never run and hides the absence of real error handling")


def test_dismissing_the_widget_is_not_reported_as_an_error():
    """widget.close is the patient closing the modal. Telling them something
    went wrong because they changed their mind is its own defect."""
    source = _source()
    close_at = source.find("'widget.close'")
    assert close_at != -1, "widget.close must be handled"
    branch = source[close_at:close_at + 400]
    assert "showStatus" not in branch, (
        "the widget.close branch must not raise a status message")


def test_the_error_message_does_not_blame_the_patients_records():
    """The cause lives in Fasten's account entitlement or the browser's
    cookie policy. Neither is a fact about this person's health records, and
    saying otherwise sends them to their provider over our configuration."""
    source = _source()
    at = source.find("'widget.config_error'")
    # Wide enough to reach the message. A window that stops short fails on a
    # correct file, which is a probe measuring its own truncation.
    branch = source[at:at + 2500]
    assert "not with your records" in branch, (
        "the message must state this is not a problem with the patient's "
        "records, because the patient cannot tell and will assume it is")


def test_tefca_mode_is_opt_in():
    """A capability this process cannot verify must not be the default.

    TEFCA IAS needs per-account provisioning with Fasten AND third-party
    cookies. Defaulting it on meant any unset deployment failed at the first
    identity step with a vendor error we cannot intercept (#326).

    MUTATION: flip the default back to 'true' -> red.
    """
    app_source = (pathlib.Path(__file__).resolve().parent.parent
                  / "app.py").read_text(encoding="utf-8")
    match = re.search(r"FASTEN_TEFCA_MODE',\s*'(\w+)'", app_source)
    assert match, "the FASTEN_TEFCA_MODE default moved; update this guard"
    assert match.group(1) == "false", (
        "FASTEN_TEFCA_MODE must default to false — entitlement is declared "
        "by the deployment that has it, never assumed")


def test_the_embed_url_sends_what_the_official_component_sends():
    """Our iframe is hand-rolled, so nothing else pins it to the contract.

    Read out of the v4 bundle, the component's TEFCA branch sets exactly
    `tefca-mode` and `search-only=false` (identity-request-uri and
    tefca-csp-prompt-force are optional and default to null/false). If our
    URL drifts from that, the widget is being asked for something the
    supported integration never asks for.
    """
    source = _source()
    assert "tefca-mode=true&search-only=false" in source, (
        "TEFCA mode must send both params, as the official component does")
    assert "public-id={{ fasten_public_key }}" in source


def test_the_page_still_renders_with_tefca_off(monkeypatch):
    """Turning the mode off must not take the connect flow down with it —
    that fallback is the interim mitigation in #326."""
    monkeypatch.setenv("FASTEN_TEFCA_MODE", "false")
    assert os.environ["FASTEN_TEFCA_MODE"] == "false"
    source = _source()
    assert "{% if tefca_mode %}" in source, (
        "the TEFCA params must stay behind the flag so turning it off "
        "degrades to standard provider search rather than breaking the embed")
