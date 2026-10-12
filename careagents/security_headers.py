"""Browser security headers on every CareAgents response.

careagents.cloud sent none of the headers app.healthclaw.io sends (app.py
`_security_headers`). This is the same set, with a policy written for what
CareAgents' own pages load:

- Fonts and stylesheets are self-hosted under static/; the favicon is a
  data: URI (and CSS icons may be data: SVGs).
- chat.html and landing.html carry inline <script> blocks, several templates
  use style="" attributes, and the relayed engine review page
  (/review/<agent>/<action>, templates/review_base.html) is self-contained:
  inline <style> and <script>, nothing fetched. So 'unsafe-inline' for both,
  as on the engine. Tighten to nonces when the inline blocks move out.
- Every fetch and the chat SSE stream are same-origin.
- Nothing is framed: Fasten Connect and re-auth open in a new tab
  (home.js window.open), not an iframe, so there is no frame-src.
- WebAuthn is not governed by CSP.

setdefault throughout, so a handler's own header wins; the texted-link
pages keep their `Referrer-Policy: no-referrer` (app.py).

HSTS only when the request arrived over HTTPS (Railway terminates TLS and
says so in X-Forwarded-Proto) or in production, where the canonical host is
HTTPS-only. No includeSubDomains: other careagents.cloud subdomains are not
this app's to commit to HTTPS.
"""

from __future__ import annotations

from flask import Flask, request

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)
HSTS = "max-age=31536000"


def install(app: Flask, production: bool) -> None:
    @app.after_request
    def _security_headers(response):
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        proto = request.headers.get("X-Forwarded-Proto", "")
        if (production or request.is_secure
                or proto.split(",")[0].strip().lower() == "https"):
            h.setdefault("Strict-Transport-Security", HSTS)
        return response
