"""Guard: the site does not hyperlink a host nobody has vouched for.

Its sibling `test_site_links_resolve.py` checks that internal links land
somewhere. This checks the other direction: an external link hands the reader
to a party we do not control, so the set of parties has to be a decision
somebody made rather than an accumulation.

The failure this was written for: `templates/faq.html` and `templates/wiki.html`
linked `sharponmcp.com` as the SHARP-on-MCP specification. That domain is not
merely unreachable, it is *unregistered* (`whois`: no match; NXDOMAIN from both
Google and Cloudflare with the `.com` SOA in authority, 2026-09-04). Anyone
could register it and inherit the credibility of a link from our own FAQ
describing it as the spec our servers implement.

A resolution check would need the network, which the suite does not get, and
would also pass the day a squatter puts up a page. An allowlist catches the
case a liveness probe cannot: a host that answers and should still not be
linked. It also fails closed, at the moment a new link is added, which is the
only moment anyone is in a position to judge it.

Scope is `templates/` — the Flask site. Adding a host here is the review step,
so give each one a reason.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

_HREF_HOST = re.compile(r'href="https?://([^"/?#]+)')

#: External hosts the site may link to, and why each is here.
VOUCHED = {
    "healthclaw.io": "our site",
    "app.healthclaw.io": "our app",
    "careagents.cloud": "our consumer app",
    "github.com": "our source, and third-party repositories we cite",
    "evestel.substack.com": "the author's newsletter",
    "app.promptopinion.ai": "sister product, ours",
    "docs.promptopinion.ai": "sister product, ours",
    "www.fastenhealth.com": "the connector vendor we integrate",
    "docs.connect.fastenhealth.com": "that vendor's documentation",
    "app.connect.fastenhealth.com": "that vendor's console",
    "stitch.fastenhealth.com": "that vendor's service",
    "resources.anthropic.com": "vendor documentation",
    "resend.com": "the email provider we run on",
    "ngrok.com": "named in developer setup instructions",
    "agents-assemble.devpost.com": "a hackathon listing we entered",
}


def _templates() -> list[pathlib.Path]:
    return sorted(TEMPLATES.glob("*.html"))


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_external_links_go_only_to_vouched_hosts(template: pathlib.Path) -> None:
    text = template.read_text(encoding="utf-8")
    unvouched = sorted(
        {h for h in _HREF_HOST.findall(text) if h not in VOUCHED}
    )
    assert not unvouched, (
        f"{template.name} links to {unvouched}, which is not in VOUCHED. "
        "Confirm the host is registered to someone we mean to send readers "
        "to, then add it with a reason — or drop the hyperlink and keep the "
        "prose."
    )
