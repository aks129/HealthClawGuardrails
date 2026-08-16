"""The connector registry, and the promise that nothing moved underneath it.

This is the one path every upstream read and write goes through, and its
failure mode is quiet: a deployment that stops authenticating does not crash,
it returns 502s that look like the upstream's fault. So the first class here
is not about the new feature at all. It is a table of every environment
combination that resolved to a working proxy before this change, asserting it
resolves to the identical proxy now.

The rest covers what the registry adds: a named kind per server, an auth style
that comes from the registry rather than from a branch, and a token endpoint
derived from the server being addressed.
"""

import pytest

from r6.fhir_proxy import FHIRUpstreamProxy, MedplumProxy, OAuth2UpstreamProxy
from r6.upstream_connectors import (
    AUTH_BASIC,
    AUTH_NONE,
    AUTH_OAUTH2,
    CONNECTORS,
    resolve_upstream_config,
    supported_connectors,
)

AIDBOX = "https://aidbox.example/fhir"
MEDPLUM = "https://medplum.example/fhir/R4"


def _resolve(**env):
    return resolve_upstream_config(environ=env)


class TestNothingMovedForDeploymentsThatAlreadyWork:
    """MUTATION: swap the precedence so MEDPLUM_BASE_URL wins -> red.

    Every row is a live-shaped environment from before the registry existed.
    A row that changes meaning is a deployment that silently stops working.
    """

    def test_generic_upstream_with_basic_credentials(self):
        """The Aidbox example's exact configuration, under its old name."""
        c = _resolve(FHIR_UPSTREAM_URL=AIDBOX,
                     FHIR_UPSTREAM_CLIENT_ID="healthclaw",
                     FHIR_UPSTREAM_CLIENT_SECRET="s3cret")
        assert c.base_url == AIDBOX
        assert c.auth == AUTH_BASIC
        assert c.basic_auth == ("healthclaw", "s3cret")

    def test_generic_upstream_with_no_credentials_stays_anonymous(self):
        """Public sandboxes were reachable without credentials and must stay so."""
        c = _resolve(FHIR_UPSTREAM_URL="https://hapi.example/fhir")
        assert c.auth == AUTH_BASIC and c.basic_auth is None

    def test_medplum_env_still_resolves_medplum(self):
        """No deployment should have to learn FHIR_UPSTREAM_KIND to keep working."""
        c = _resolve(MEDPLUM_BASE_URL=MEDPLUM,
                     MEDPLUM_CLIENT_ID="cid", MEDPLUM_CLIENT_SECRET="csec")
        assert c.kind == "medplum"
        assert c.auth == AUTH_OAUTH2
        assert (c.client_id, c.client_secret) == ("cid", "csec")
        assert c.token_endpoint == "https://medplum.example/oauth2/token"

    def test_fhir_upstream_url_still_wins_over_medplum(self):
        """Precedence, pinned. Reversing it moves a live deployment to a
        different server without changing a single variable."""
        c = _resolve(FHIR_UPSTREAM_URL=AIDBOX, MEDPLUM_BASE_URL=MEDPLUM,
                     MEDPLUM_CLIENT_ID="cid", MEDPLUM_CLIENT_SECRET="csec")
        assert c.base_url == AIDBOX
        assert c.auth == AUTH_BASIC

    def test_no_upstream_is_still_local_mode(self):
        assert _resolve() is None
        assert _resolve(FHIR_UPSTREAM_URL="   ") is None


class TestTheRegistryNamesWhatWeAreInFrontOf:

    @pytest.mark.parametrize("kind,auth", [
        ("aidbox", AUTH_BASIC),
        ("medplum", AUTH_OAUTH2),
        # hapi was AUTH_NONE, and this row PINNED the defect. Its summary
        # tells an operator to set FHIR_UPSTREAM_CLIENT_ID/_SECRET for a
        # secured server, and AUTH_NONE made `basic_auth` drop them. Changing
        # this row is changing a specification deliberately, with the reason
        # recorded — not raising a ratchet to go green.
        ("hapi", AUTH_BASIC),
        ("generic", AUTH_BASIC),
    ])
    def test_each_kind_declares_its_auth_style(self, kind, auth):
        c = _resolve(FHIR_UPSTREAM_KIND=kind, FHIR_UPSTREAM_URL=AIDBOX)
        assert c.kind == kind and c.auth == auth

    def test_a_connector_that_offers_credentials_actually_sends_them(self):
        """THE RULE, and the general form of the hapi defect: a summary that
        names a credential must belong to an auth style that carries one.

        The registry summary is not decoration — `supported_connectors()`
        publishes it as the answer to "what does this build support and how do
        I authenticate to it". A connector whose prose and whose behaviour
        disagree is the retro's shape with an operator manual attached, and
        the failure is silent: credentials accepted, dropped, 401 upstream,
        502 that reads as the upstream's fault.

        MUTATION: set hapi back to AUTH_NONE -> red.
        """
        offenders = []
        for connector in CONNECTORS.values():
            mentions_credential = (
                "CLIENT_ID" in connector.summary
                or "Basic" in connector.summary
                or "credential" in connector.summary.lower())
            if mentions_credential and connector.auth == AUTH_NONE:
                offenders.append(f"{connector.name}: {connector.summary}")
        assert not offenders, (
            "a connector offers a credential its auth style cannot send:\n  "
            + "\n  ".join(offenders))

    def test_hapi_sends_a_credential_when_one_is_configured(self):
        """The behaviour, measured rather than inferred from the enum.

        MUTATION: set hapi back to AUTH_NONE -> red.
        """
        c = _resolve(FHIR_UPSTREAM_KIND="hapi", FHIR_UPSTREAM_URL=AIDBOX,
                     FHIR_UPSTREAM_CLIENT_ID="hapi-user",
                     FHIR_UPSTREAM_CLIENT_SECRET="hapi-pass")
        assert c.basic_auth == ("hapi-user", "hapi-pass")

    def test_hapi_stays_anonymous_when_none_is(self):
        """The other side, and why this is not a breaking change: a public
        sandbox takes no credential and must keep taking none.

        MUTATION: make basic_auth return a pair when only one half is set,
        or make AUTH_BASIC demand credentials -> red.
        """
        c = _resolve(FHIR_UPSTREAM_KIND="hapi", FHIR_UPSTREAM_URL=AIDBOX)
        assert c.basic_auth is None
        half = _resolve(FHIR_UPSTREAM_KIND="hapi", FHIR_UPSTREAM_URL=AIDBOX,
                        FHIR_UPSTREAM_CLIENT_ID="hapi-user")
        assert half.basic_auth is None

    def test_aidbox_is_first_class_now(self):
        """It was the better-verified connector and the one with no name."""
        c = _resolve(FHIR_UPSTREAM_KIND="aidbox", FHIR_UPSTREAM_URL=AIDBOX,
                     FHIR_UPSTREAM_CLIENT_ID="healthclaw",
                     FHIR_UPSTREAM_CLIENT_SECRET="s3cret")
        assert c.basic_auth == ("healthclaw", "s3cret")

    def test_medplum_under_the_unified_names(self):
        c = _resolve(FHIR_UPSTREAM_KIND="medplum", FHIR_UPSTREAM_URL=MEDPLUM,
                     FHIR_UPSTREAM_CLIENT_ID="cid",
                     FHIR_UPSTREAM_CLIENT_SECRET="csec")
        assert c.auth == AUTH_OAUTH2
        assert c.token_endpoint == "https://medplum.example/oauth2/token"

    def test_an_unknown_kind_refuses_rather_than_guessing(self):
        """MUTATION: fall back to 'generic' on an unknown kind -> red.

        An unknown kind means an unknown auth style. Defaulting to generic
        would send anonymous requests at whatever that server is, and a typo
        in a variable name is not a reason to do that quietly.
        """
        with pytest.raises(ValueError, match="not one of"):
            _resolve(FHIR_UPSTREAM_KIND="epic", FHIR_UPSTREAM_URL=AIDBOX)

    def test_the_supported_list_is_answerable(self):
        listed = {c["kind"] for c in supported_connectors()}
        assert listed == set(CONNECTORS)
        assert all(c["summary"] for c in supported_connectors())


class TestTheTokenEndpointFollowsTheServer:
    """The defect this registry inherits the fix for: a token endpoint that
    was a constant pointing at one vendor's hosted service."""

    @pytest.mark.parametrize("base,expected", [
        ("https://api.medplum.com/fhir/R4", "https://api.medplum.com/oauth2/token"),
        ("https://self.hosted.example/fhir/R4", "https://self.hosted.example/oauth2/token"),
        ("http://localhost:8103/fhir/R4", "http://localhost:8103/oauth2/token"),
    ])
    def test_derived_from_the_configured_base(self, base, expected):
        c = _resolve(FHIR_UPSTREAM_KIND="medplum", FHIR_UPSTREAM_URL=base,
                     FHIR_UPSTREAM_CLIENT_ID="i", FHIR_UPSTREAM_CLIENT_SECRET="s")
        assert c.token_endpoint == expected

    def test_an_explicit_override_wins(self):
        c = _resolve(FHIR_UPSTREAM_KIND="medplum", FHIR_UPSTREAM_URL=MEDPLUM,
                     FHIR_UPSTREAM_TOKEN_URL="https://auth.example/token",
                     FHIR_UPSTREAM_CLIENT_ID="i", FHIR_UPSTREAM_CLIENT_SECRET="s")
        assert c.token_endpoint == "https://auth.example/token"

    def test_a_basic_connector_has_no_token_endpoint(self):
        c = _resolve(FHIR_UPSTREAM_KIND="aidbox", FHIR_UPSTREAM_URL=AIDBOX)
        assert c.token_endpoint == ""


class TestGetProxyBuildsWhatTheConfigSays:

    @pytest.fixture(autouse=True)
    def _reset(self):
        from r6.fhir_proxy import reset_proxy
        reset_proxy()
        yield
        reset_proxy()

    def test_basic_upstream_builds_a_plain_proxy(self, monkeypatch):
        from r6.fhir_proxy import get_proxy
        monkeypatch.setenv("FHIR_UPSTREAM_URL", AIDBOX)
        monkeypatch.setenv("FHIR_UPSTREAM_CLIENT_ID", "healthclaw")
        monkeypatch.setenv("FHIR_UPSTREAM_CLIENT_SECRET", "s3cret")
        p = get_proxy()
        assert isinstance(p, FHIRUpstreamProxy)
        assert not isinstance(p, OAuth2UpstreamProxy)

    def test_oauth2_upstream_builds_a_token_injecting_proxy(self, monkeypatch):
        from r6.fhir_proxy import get_proxy
        monkeypatch.setenv("FHIR_UPSTREAM_KIND", "medplum")
        monkeypatch.setenv("FHIR_UPSTREAM_URL", MEDPLUM)
        monkeypatch.setenv("FHIR_UPSTREAM_CLIENT_ID", "cid")
        monkeypatch.setenv("FHIR_UPSTREAM_CLIENT_SECRET", "csec")
        p = get_proxy()
        assert isinstance(p, OAuth2UpstreamProxy)
        assert p._token_endpoint == "https://medplum.example/oauth2/token"

    def test_oauth2_without_credentials_refuses_rather_than_going_anonymous(
            self, monkeypatch):
        """MUTATION: build the proxy anyway when credentials are missing -> red.

        A client-credentials upstream with no credentials can only make
        anonymous requests. That is not a degraded mode, it is a different
        request than the one intended.
        """
        from r6.fhir_proxy import get_proxy
        monkeypatch.setenv("FHIR_UPSTREAM_KIND", "medplum")
        monkeypatch.setenv("FHIR_UPSTREAM_URL", MEDPLUM)
        assert get_proxy() is None

    def test_the_old_class_name_still_imports(self):
        """Scripts and tests import MedplumProxy by name."""
        assert MedplumProxy is OAuth2UpstreamProxy


class TestHealthNamesTheConnectorBesideTheServer:
    """The kind WE resolved, printed next to the software the server claims.

    When those disagree — kind `medplum` against software `aidbox` — the
    deployment is pointed somewhere nobody meant, and that is otherwise
    invisible until a request fails for a reason naming neither.

    Carried on the proxy rather than added by the health handler, because
    r6/routes.py is under a shrink-only ratchet and because the kind is a
    property of the connection, not of the endpoint that reports it.
    """

    def _proxy_reporting(self, kind, software="aidbox"):
        import httpx
        p = FHIRUpstreamProxy("https://up.example/fhir", kind=kind)
        p._client = httpx.Client(
            base_url="https://up.example/fhir",
            transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={"resourceType": "CapabilityStatement",
                           "fhirVersion": "4.0.1",
                           "software": {"name": software}})))
        return p

    def test_the_kind_is_reported(self):
        p = self._proxy_reporting("aidbox")
        try:
            assert p.healthy()["kind"] == "aidbox"
        finally:
            p.close()

    def test_a_mismatch_is_visible_rather_than_hidden(self):
        """MUTATION: drop 'kind' from healthy() -> red.

        Both facts have to be on the same line for anyone to notice they
        disagree.
        """
        p = self._proxy_reporting("medplum", software="aidbox")
        try:
            h = p.healthy()
            assert h["kind"] == "medplum" and h["software"] == "aidbox"
        finally:
            p.close()

    def test_an_unkinded_proxy_omits_it_entirely(self):
        """SHARP builds proxies with no configured kind. An empty string in
        the payload would read as a connector named ''."""
        p = self._proxy_reporting("")
        try:
            assert "kind" not in p.healthy()
        finally:
            p.close()
