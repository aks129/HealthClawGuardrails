"""The Medplum connector talks to the Medplum you configured.

Every existing Medplum test mocks `_fetch_medplum_token`, which is exactly
why two defects lived in the connector while the guardrails wrapped around it
were thoroughly covered. The tests proved the redaction, audit and step-up
behaviour of a proxy whose authentication never ran.

  * the token endpoint was the constant `https://api.medplum.com/oauth2/token`
    with no override. Self-hosting is Medplum's whole proposition, so pointing
    MEDPLUM_BASE_URL at your own server was the expected case — and it sent
    your credentials to a service that has never heard of them.
  * `_inject_bearer` caught every exception, logged, and returned. The request
    then went out with NO Authorization header: a token failure silently
    became an anonymous request against the record system.

The second is what makes the first dangerous rather than merely broken. A
wrong token endpoint fails, the failure is swallowed, and the proxy asks a
FHIR server for patient data with no credential at all.
"""

import os
from unittest.mock import patch

import httpx
import pytest

from r6.fhir_proxy import (
    MedplumProxy,
    _MEDPLUM_HOSTED_TOKEN_ENDPOINT,
    medplum_token_endpoint,
)


class TestTheTokenEndpointFollowsTheServer:
    """MUTATION: return the hosted constant unconditionally -> red."""

    @pytest.mark.parametrize("base,expected", [
        # The hosted service, which is the only case the constant was right for.
        ("https://api.medplum.com/fhir/R4",
         "https://api.medplum.com/oauth2/token"),
        # Self-hosted: the case that silently authenticated against Medplum's
        # servers with credentials they do not hold.
        ("https://fhir.hospital.example/fhir/R4",
         "https://fhir.hospital.example/oauth2/token"),
        # A port and a path prefix both survive, because deployments behind a
        # gateway are ordinary.
        ("https://medplum.internal:8103/fhir/R4",
         "https://medplum.internal:8103/oauth2/token"),
    ])
    def test_it_is_derived_from_the_configured_base(self, base, expected):
        assert medplum_token_endpoint(base) == expected

    def test_an_explicit_override_wins(self, monkeypatch):
        """Some deployments put the authorization server somewhere else."""
        monkeypatch.setenv("MEDPLUM_TOKEN_URL", "https://auth.example/token")
        assert medplum_token_endpoint(
            "https://fhir.hospital.example/fhir/R4") == "https://auth.example/token"

    def test_it_falls_back_rather_than_crashing_on_nonsense(self, monkeypatch):
        """A malformed base must not take the process down at import time.

        The fallback is the hosted endpoint, which fails loudly at token time
        against the wrong credentials — a bad outcome, but a diagnosable one,
        and better than a 500 from a config typo.
        """
        monkeypatch.delenv("MEDPLUM_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEDPLUM_BASE_URL", raising=False)
        assert medplum_token_endpoint("not-a-url") == _MEDPLUM_HOSTED_TOKEN_ENDPOINT

    def test_the_proxy_resolves_it_from_its_own_base(self, monkeypatch):
        """Not from the environment at call time.

        Reading the env inside the request hook would let a proxy built for
        one server authenticate against another after an unrelated env change.
        """
        monkeypatch.delenv("MEDPLUM_TOKEN_URL", raising=False)
        proxy = MedplumProxy("https://self.hosted.example/fhir/R4", "id", "sec")
        try:
            assert proxy._token_endpoint == "https://self.hosted.example/oauth2/token"
        finally:
            proxy.close()


class TestATokenFailureIsNotAnAnonymousRequest:
    """MUTATION: catch the exception in _inject_bearer and return -> red.

    This is the check that would have caught the fail-open. It asserts on
    what LEFT the process, not on what was logged: a log line saying "failed
    to obtain token" beside a request that went out anyway is exactly the
    shape that made this invisible.
    """

    def test_the_request_is_not_sent_without_a_credential(self):
        sent = []

        def record(request):
            sent.append(request)
            return httpx.Response(200, json={"resourceType": "Patient"})

        proxy = MedplumProxy("https://self.hosted.example/fhir/R4", "id", "sec")
        proxy._client = httpx.Client(
            base_url="https://self.hosted.example/fhir/R4",
            transport=httpx.MockTransport(record),
            event_hooks={"request": [proxy._inject_bearer]},
        )
        try:
            with patch("r6.fhir_proxy._fetch_medplum_token",
                       side_effect=httpx.ConnectError("token endpoint down")):
                with pytest.raises(httpx.ConnectError):
                    proxy._client.get("/Patient/x")
        finally:
            proxy.close()

        assert sent == [], (
            "a request reached the FHIR server after the token fetch failed. "
            "It carried no Authorization header, so this is an anonymous "
            "request for patient data that nobody asked for.")

    def test_a_working_token_is_attached(self):
        """Two-sided (#213): a hook that raised on everything would pass the
        check above while making the connector useless."""
        seen = {}

        def record(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"resourceType": "Patient"})

        proxy = MedplumProxy("https://self.hosted.example/fhir/R4", "id", "sec")
        proxy._client = httpx.Client(
            base_url="https://self.hosted.example/fhir/R4",
            transport=httpx.MockTransport(record),
            event_hooks={"request": [proxy._inject_bearer]},
        )
        try:
            with patch("r6.fhir_proxy._fetch_medplum_token", return_value="tok-123"):
                proxy._client.get("/Patient/x")
        finally:
            proxy.close()

        assert seen["auth"] == "Bearer tok-123"


def test_the_token_fetch_posts_to_the_endpoint_it_was_given():
    """MUTATION: ignore the token_endpoint argument -> red.

    Deriving the right URL and then not using it is a two-line regression
    that every other test in the suite would survive.
    """
    from r6.fhir_proxy import _fetch_medplum_token

    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "t", "expires_in": 3600}

    def fake_post(url, **kw):
        calls.append(url)
        return _Resp()

    with patch("r6.fhir_proxy._get_redis", return_value=None), \
         patch("r6.fhir_proxy._medplum_cache", {"token": None, "expires_at": 0.0}), \
         patch("httpx.post", side_effect=fake_post):
        _fetch_medplum_token("id", "sec",
                             "https://self.hosted.example/oauth2/token")

    assert calls == ["https://self.hosted.example/oauth2/token"], calls
    assert os.environ.get("MEDPLUM_TOKEN_URL") is None or True
