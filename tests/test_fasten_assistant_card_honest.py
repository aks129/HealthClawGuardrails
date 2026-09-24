"""Guard: the "Connect your AI assistant" card says who it will refuse (#600).

A tester who connects records through CareAgents is sent to HealthClaw's
`/connect/<tenant>` page. Once Fasten's signed webhook verifies the
connection, that page shows a one-time card with a read token and told them
to copy it into "Claude, Perplexity, or any MCP agent".

Measured 2026-09-23 against the production MCP endpoint: `POST /mcp` without
the operator token answers `401` with a bare `WWW-Authenticate: Bearer`, and
both `/.well-known/oauth-protected-resource` paths are `404` — the #290 dead
end, unchanged, because phase 1 (#556) ships behind a flag that is off. A
hosted assistant cannot attach the operator token, so every one the card
named refuses the tester, and nothing on the page said so.

The card stays: the token is mint-once (`agent_token_issued_at`, 410 on a
second request), so withholding it would lose it for good. What changes is
that the card says, before the values, which assistants will refuse them.

This renders the page rather than reading the file, but the card itself is
built by the page's script after a live webhook, so the assertion is on the
script text the browser receives.
"""

from __future__ import annotations


def _connect_page(client, monkeypatch) -> str:
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_600")
    resp = client.get("/connect/tester-600")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_assistant_card_says_up_front_that_hosted_assistants_refuse(
        client, monkeypatch):
    """MUTATION: restore "(Claude, Perplexity, or any MCP agent) so it can
    read" in templates/fasten_connect.html and drop the beta warning -> red.
    """
    page = _connect_page(client, monkeypatch)

    assert "Perplexity, or any MCP agent" not in page, (
        "the card promises hosted assistants that the production MCP "
        "endpoint refuses (#290, #600)")

    warning = "cannot connect to HealthClaw during the beta"
    assert warning in page, (
        "the card must say which assistants will refuse the token")
    assert page.index(warning) < page.index("Read token:"), (
        "the warning must come before the values, not after the tester has "
        "already copied them")


def test_faq_does_not_send_desktop_clients_to_the_hosted_endpoint(client):
    """The FAQ made the same claim in a second place (#600 gate review).

    It told Claude Desktop / Claude Code users to use "the hosted Railway
    URL" — the endpoint measured refusing them above. It must say what the
    card says: hosted assistants cannot connect during the beta, and the
    token works with a HealthClaw MCP server run on your own computer.

    MUTATION: restore "or the hosted Railway URL" in templates/faq.html and
    drop the beta sentence -> red.
    """
    resp = client.get("/faq")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)

    assert "hosted Railway URL" not in page, (
        "the FAQ points desktop clients at a hosted endpoint that refuses "
        "them (#290, #600)")
    assert "cannot connect to HealthClaw during the beta" in page, (
        "the FAQ must say the same thing the connect card says")
