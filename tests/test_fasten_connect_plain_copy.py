"""The connect page says what the patient will actually see, in plain words (#326).

FASTEN_TEFCA_MODE defaults off (#450), so on the default path the widget
opens Fasten's provider search and the patient signs in to their health
system's portal. The page still promised the opposite, unconditionally:
"Identity verification through CLEAR or ID.me satisfies every QHIN on the
nationwide TEFCA network", "A modal will open with the CLEAR / ID.me flow",
and "Identity verified" on completion. With TEFCA on, the eyebrow read
"TEFCA mode" — network jargon a patient cannot act on.

These read the VISIBLE text of the rendered page: script and style blocks
are stripped, because the handler's comments and the iframe's
`tefca-mode=true` query parameter are not copy.
"""

from __future__ import annotations

import re

_JARGON = ("TEFCA", "QHIN")


def _visible(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def _render(client, monkeypatch, tefca: str | None) -> str:
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_XYZ")
    if tefca is None:
        monkeypatch.delenv("FASTEN_TEFCA_MODE", raising=False)
    else:
        monkeypatch.setenv("FASTEN_TEFCA_MODE", tefca)
    r = client.get("/connect/test-tenant")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_default_page_does_not_promise_identity_verification(client, monkeypatch):
    """Default mode is provider search. Promising CLEAR or ID.me sets up an
    expectation the widget will not meet.

    MUTATION: drop the `{% if tefca_mode %}` around the identity copy -> red.
    """
    text = _visible(_render(client, monkeypatch, None))
    for word in _JARGON + ("CLEAR", "ID.me"):
        assert word not in text, f"default connect page shows {word!r}"
    assert "health system" in text, "the default copy must say what happens"


def test_tefca_page_describes_identity_step_without_network_jargon(
        client, monkeypatch):
    """With the flag on, the patient does verify with CLEAR or ID.me, so the
    page says so — but not in QHIN/TEFCA terms they cannot act on."""
    html = _render(client, monkeypatch, "true")
    assert "tefca-mode=true" in html, "the TEFCA path must stay intact"
    text = _visible(html)
    for word in _JARGON:
        assert word not in text, f"TEFCA-mode connect page shows {word!r}"
    assert "CLEAR" in text and "ID.me" in text


def test_completion_status_does_not_claim_identity_was_verified(
        client, monkeypatch):
    """The completion handler runs in both modes. On the default path no
    identity verification happened, so the status must not say it did."""
    for mode in (None, "true"):
        assert "Identity verified" not in _render(client, monkeypatch, mode)
