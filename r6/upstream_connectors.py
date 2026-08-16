"""Which FHIR server we are in front of, and how we authenticate to it.

Before this, "supported upstreams" was two unrelated branches in `get_proxy`
under two naming conventions: `FHIR_UPSTREAM_*` with HTTP Basic, and
`MEDPLUM_*` with OAuth2. Aidbox — the one verified end to end, with a
published example — had no name of its own and travelled as "generic". An
operator had no way to ask what was supported, and adding a third server
meant a third branch.

This module answers three questions in one place: what servers we know how to
sit in front of, how each one expects to be authenticated, and where its token
endpoint lives.

WHAT IT DELIBERATELY DOES NOT TOUCH: the SHARP per-request path. That upstream
is chosen by the caller and authenticated with the CALLER's own token, which
is a different trust relationship from a proxy holding a credential of its
own. Folding the two together would put a caller-supplied server behind the
same resolution logic as an operator-configured one, and the difference
between those is the whole reason `caller_auth` exists.

BACK COMPATIBILITY IS A HARD REQUIREMENT here, not a courtesy: this is the one
path every upstream read and write goes through, and a deployment that stops
authenticating does not fail loudly — it fails as a 502 that looks like the
upstream's fault. Every environment that resolved to a working proxy before
resolves to the identical proxy now, which
tests/test_upstream_connector_registry.py asserts combination by combination.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

#: How the proxy proves who it is to the upstream.
AUTH_NONE = "none"
AUTH_BASIC = "basic"
AUTH_OAUTH2 = "oauth2_client_credentials"


@dataclass(frozen=True)
class Connector:
    """One FHIR server we know how to sit in front of."""

    name: str
    auth: str
    summary: str
    #: True when the server's token endpoint hangs off its origin rather than
    #: needing to be configured. Only meaningful for AUTH_OAUTH2.
    token_path: str | None = None

    def token_endpoint(self, base_url: str, explicit: str = "") -> str:
        """Where to ask THIS deployment for a token.

        Derived from the server we are actually talking to. The Medplum
        connector shipped with a constant pointing at Medplum's hosted
        service, so a self-hosted instance — the case self-hosting exists for
        — sent its credentials somewhere that had never heard of them. Any
        connector added here inherits the fix rather than repeating the bug.
        """
        if explicit:
            return explicit
        if not self.token_path:
            return ""
        parts = urlsplit(base_url or "")
        if parts.scheme and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, self.token_path, "", ""))
        return ""


#: Every upstream this build knows by name.
#:
#: `generic` is not a fallback for "we could not tell" — it is a real entry
#: for a server that takes HTTP Basic or nothing, which is most of them. What
#: naming the others buys is the auth style and the token rule, which is
#: exactly what an operator would otherwise have to work out from our source.
CONNECTORS: dict[str, Connector] = {
    "aidbox": Connector(
        name="aidbox",
        auth=AUTH_BASIC,
        summary="Aidbox Client credential over HTTP Basic, scoped by AccessPolicy.",
    ),
    "medplum": Connector(
        name="medplum",
        auth=AUTH_OAUTH2,
        summary="Medplum ClientApplication via OAuth2 client-credentials.",
        token_path="/oauth2/token",
    ),
    "hapi": Connector(
        name="hapi",
        auth=AUTH_NONE,
        summary="HAPI FHIR. Public sandboxes take no credential; add "
                "FHIR_UPSTREAM_CLIENT_ID/_SECRET for one behind HTTP Basic.",
    ),
    "generic": Connector(
        name="generic",
        auth=AUTH_BASIC,
        summary="Any FHIR server. HTTP Basic when credentials are set, "
                "anonymous when they are not.",
    ),
}


@dataclass(frozen=True)
class UpstreamConfig:
    """The resolved answer to "what are we in front of, and how do we sign in"."""

    kind: str
    base_url: str
    auth: str
    client_id: str = ""
    client_secret: str = ""
    token_endpoint: str = ""

    @property
    def connector(self) -> Connector:
        return CONNECTORS[self.kind]

    @property
    def basic_auth(self) -> tuple[str, str] | None:
        if self.auth == AUTH_BASIC and self.client_id and self.client_secret:
            return (self.client_id, self.client_secret)
        return None


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def resolve_upstream_config(environ=None) -> UpstreamConfig | None:
    """Read the environment and say what upstream is configured, or None.

    Precedence is unchanged from the code this replaces: an explicit
    FHIR_UPSTREAM_URL wins over MEDPLUM_BASE_URL. Deployments set one or the
    other, and reversing the order would silently move a live deployment onto
    a different server.
    """
    get = (lambda k: (environ or os.environ).get(k, "").strip()) if environ is not None else _env

    kind = get("FHIR_UPSTREAM_KIND").lower()
    if kind and kind not in CONNECTORS:
        raise ValueError(
            f"FHIR_UPSTREAM_KIND={kind!r} is not one of "
            f"{sorted(CONNECTORS)}. Refusing to guess: an unknown kind means "
            "an unknown auth style, and defaulting to anonymous would send "
            "unauthenticated requests at a record system."
        )

    url = get("FHIR_UPSTREAM_URL")
    client_id = get("FHIR_UPSTREAM_CLIENT_ID")
    client_secret = get("FHIR_UPSTREAM_CLIENT_SECRET")
    token_url = get("FHIR_UPSTREAM_TOKEN_URL")

    if not url:
        # The MEDPLUM_* names predate the unified ones and are still set on
        # real deployments. They imply the kind, which is why no existing
        # deployment has to learn FHIR_UPSTREAM_KIND to keep working.
        url = get("MEDPLUM_BASE_URL")
        if url:
            kind = kind or "medplum"
            client_id = client_id or get("MEDPLUM_CLIENT_ID")
            client_secret = client_secret or get("MEDPLUM_CLIENT_SECRET")
            token_url = token_url or get("MEDPLUM_TOKEN_URL")

    if not url:
        return None

    connector = CONNECTORS[kind or "generic"]
    return UpstreamConfig(
        kind=connector.name,
        base_url=url,
        auth=connector.auth,
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint=connector.token_endpoint(url, token_url),
    )


def supported_connectors() -> list[dict]:
    """The registry, for an operator asking what this build supports."""
    return [
        {"kind": c.name, "auth": c.auth, "summary": c.summary}
        for c in sorted(CONNECTORS.values(), key=lambda c: c.name)
    ]

