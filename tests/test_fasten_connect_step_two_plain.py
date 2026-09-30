"""Step 2 of the connect page names no internals (calm hub spec section 7)."""

from __future__ import annotations

import re

_INTERNALS = ("org_connection_id", "/fasten/webhook", "Curatr")


def _visible(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", html,
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def test_step_two_names_no_internals(client, monkeypatch):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_step2")
    r = client.get("/connect/test-tenant")
    assert r.status_code == 200
    text = " ".join(_visible(r.get_data(as_text=True)).split())
    for word in _INTERNALS:
        assert word not in text, word
    assert "private space that only you control" in text
