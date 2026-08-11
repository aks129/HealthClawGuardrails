"""The upstream proxy can present a credential to the server it proxies.

Written for the Aidbox integration example (examples/aidbox-healthclaw-
guardrails), and it exists because building that example found the claim
"a proxy in front of any FHIR server" to be narrower than it sounded.

`FHIR_UPSTREAM_URL` mode sent no credential. That is fine against the four
upstreams it had been tested against — HAPI's public server, SMART Health
IT, a local HAPI, and a Medplum instance that gets its token by a different
path entirely. All of them serve an anonymous read. Aidbox does not, and
neither does any other FHIR server configured the way you would configure
one that holds real records. Against those the proxy sent no Authorization
header, took a 401, and reported a 502.

So the guardrail layer could not sit in front of a secured FHIR server,
which is the only kind worth guarding.
"""

import httpx
import pytest

from r6 import fhir_proxy


@pytest.fixture(autouse=True)
def _reset():
    fhir_proxy.reset_proxy()
    yield
    fhir_proxy.reset_proxy()


class TestTheCredentialReachesTheUpstream:

    def test_basic_auth_is_sent_when_both_halves_are_configured(
            self, monkeypatch):
        """MUTATION: drop `auth=basic_auth` from the httpx client -> red.

        Asserted on the wire, not on the constructor argument: the point is
        that the upstream receives an Authorization header, and only a
        request can show that.
        """
        monkeypatch.setenv('FHIR_UPSTREAM_URL', 'http://aidbox:8080/fhir')
        monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_ID', 'healthclaw')
        monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_SECRET', 'not-a-real-secret')

        proxy = fhir_proxy.get_proxy()
        assert proxy is not None

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['authorization'] = request.headers.get('authorization')
            return httpx.Response(200, json={'resourceType': 'CapabilityStatement',
                                             'fhirVersion': '4.0.1'})

        proxy._client = httpx.Client(
            base_url=proxy.upstream_url,
            auth=proxy.basic_auth,
            transport=httpx.MockTransport(handler),
        )
        proxy.healthy()

        assert seen['authorization'] is not None, (
            'the upstream request carried no Authorization header')
        assert seen['authorization'].startswith('Basic ')

    def test_no_credential_is_sent_when_none_is_configured(self, monkeypatch):
        """The default has to stay anonymous. A public sandbox upstream must
        keep working for everyone who already points at one."""
        monkeypatch.setenv('FHIR_UPSTREAM_URL', 'https://hapi.fhir.org/baseR4')
        monkeypatch.delenv('FHIR_UPSTREAM_CLIENT_ID', raising=False)
        monkeypatch.delenv('FHIR_UPSTREAM_CLIENT_SECRET', raising=False)

        proxy = fhir_proxy.get_proxy()
        assert proxy is not None
        assert proxy.basic_auth is None


class TestHalfConfiguredIsLoud:
    """A typo in one variable name must not degrade to anonymous in silence.

    This is the defect shape the retro names: a control that looks like one
    thing and quietly does another. Anonymous-against-a-secured-upstream
    fails, but it fails as a wall of 502s whose cause appears nowhere.
    """

    @pytest.mark.parametrize('present,missing', [
        ('FHIR_UPSTREAM_CLIENT_ID', 'FHIR_UPSTREAM_CLIENT_SECRET'),
        ('FHIR_UPSTREAM_CLIENT_SECRET', 'FHIR_UPSTREAM_CLIENT_ID'),
    ])
    def test_it_names_the_variable_you_forgot(self, monkeypatch, caplog,
                                              present, missing):
        """MUTATION: return None without logging -> red."""
        monkeypatch.setenv('FHIR_UPSTREAM_URL', 'http://aidbox:8080/fhir')
        monkeypatch.delenv('FHIR_UPSTREAM_CLIENT_ID', raising=False)
        monkeypatch.delenv('FHIR_UPSTREAM_CLIENT_SECRET', raising=False)
        monkeypatch.setenv(present, 'something')

        with caplog.at_level('ERROR'):
            auth = fhir_proxy._upstream_basic_auth()

        assert auth is None, 'half a credential must not be sent as a whole one'
        assert missing in caplog.text, (
            f'the log does not name {missing}, so the operator has to guess')

    def test_the_secret_is_never_logged(self, monkeypatch, caplog):
        """THE ONE PROPERTY of this module's logging.

        MUTATION: log the credential tuple instead of the client id -> red.
        """
        secret = 'sup3rs3cret-value-that-must-not-appear'
        monkeypatch.setenv('FHIR_UPSTREAM_URL', 'http://aidbox:8080/fhir')
        monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_ID', 'healthclaw')
        monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_SECRET', secret)

        with caplog.at_level('DEBUG'):
            auth = fhir_proxy._upstream_basic_auth()

        assert auth == ('healthclaw', secret)
        assert secret not in caplog.text


class TestAnUpstreamRefusalIsNotTheCallersProblem:

    def test_a_401_from_upstream_is_not_handed_back_as_re_authenticate(self):
        """The proxy's credential is not the caller's.

        With `caller_auth=False` an upstream 401 maps to 502: our credential
        is wrong, and telling the caller to re-authenticate would send them
        round a loop they have no way to exit. This pins that adding Basic
        auth did not quietly flip that mapping.

        MUTATION: construct the proxy with caller_auth=True when basic_auth
        is set -> red.
        """
        proxy = fhir_proxy.FHIRUpstreamProxy(
            'http://aidbox:8080/fhir', basic_auth=('healthclaw', 'secret'))
        assert proxy.caller_auth is False

        body, status = fhir_proxy.sanitize_upstream_error(
            httpx.Response(401, json={'resourceType': 'OperationOutcome'}),
            caller_auth=proxy.caller_auth)
        assert status == 502
        assert body['resourceType'] == 'OperationOutcome'
