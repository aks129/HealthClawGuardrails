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


class _FakeSyntheticHospitalTokenEndpoint:
    """Stands in for `httpx.post` against the simulator's POST /auth/token.

    Behaves like the real one: a JSON {"username","password"} body gets a
    bearer token, and a form-encoded body gets 422.
    """

    def __init__(self, token='sh-token-1', status=200):
        self.token = token
        self.status = status
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        request = httpx.Request('POST', url)
        if 'data' in kw or not isinstance(kw.get('json'), dict):
            return httpx.Response(422, json={'detail': 'expected JSON'},
                                  request=request)
        if self.status != 200:
            return httpx.Response(self.status, request=request)
        return httpx.Response(200, request=request, json={
            'access_token': self.token, 'token_type': 'bearer',
            'expires_in': 43200})


class _DictRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value.encode()


SH_BASE = 'http://synthetic-hospital.example:58000/fhir'
SH_TOKEN_URL = 'http://synthetic-hospital.example:58000/auth/token'


def _synthetic_hospital_env(monkeypatch):
    monkeypatch.setenv('FHIR_UPSTREAM_KIND', 'synthetic_hospital')
    monkeypatch.setenv('FHIR_UPSTREAM_URL', SH_BASE)
    monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_ID', 'sh-user')
    monkeypatch.setenv('FHIR_UPSTREAM_CLIENT_SECRET', 'sh-pass-not-real')


def _wire(proxy, handler):
    proxy._client = httpx.Client(
        base_url=proxy.upstream_url,
        transport=httpx.MockTransport(handler),
        event_hooks={'request': [proxy._inject_bearer]},
    )


class TestTheJsonPasswordGrant:
    """The Synthetic Hospital simulator's token endpoint takes a JSON body."""

    def test_the_token_request_is_a_json_body(self, monkeypatch):
        """MUTATION: send the password grant form-encoded -> red (422)."""
        fake = _FakeSyntheticHospitalTokenEndpoint()
        monkeypatch.setattr(fhir_proxy, '_get_redis', lambda: None)
        monkeypatch.setattr(fhir_proxy.httpx, 'post', fake)

        token = fhir_proxy._fetch_medplum_token(
            'sh-user', 'sh-pass-not-real', SH_TOKEN_URL,
            grant=fhir_proxy.AUTH_PASSWORD_JSON)

        assert token == 'sh-token-1'
        url, kw = fake.calls[0]
        assert url == SH_TOKEN_URL
        assert kw['json'] == {'username': 'sh-user',
                              'password': 'sh-pass-not-real'}
        assert 'data' not in kw

    def test_the_minted_token_reaches_the_upstream(self, monkeypatch):
        """End to end through get_proxy: env -> token endpoint -> header."""
        _synthetic_hospital_env(monkeypatch)
        fake = _FakeSyntheticHospitalTokenEndpoint(token='sh-token-live')
        monkeypatch.setattr(fhir_proxy, '_get_redis', lambda: None)
        monkeypatch.setattr(fhir_proxy.httpx, 'post', fake)

        seen = {}

        def upstream(request):
            seen['authorization'] = request.headers.get('authorization')
            return httpx.Response(200, json={'resourceType': 'Patient',
                                             'id': '1'})

        proxy = fhir_proxy.get_proxy()
        _wire(proxy, upstream)
        body, status = proxy.read('Patient', '1')

        assert status == 200 and body['id'] == '1'
        assert fake.calls[0][0] == SH_TOKEN_URL
        assert seen['authorization'] == 'Bearer sh-token-live'

    def test_a_refused_token_is_not_an_anonymous_request(self, monkeypatch):
        """Fail closed, as the OAuth2 path does: no token, no request."""
        _synthetic_hospital_env(monkeypatch)
        monkeypatch.setattr(fhir_proxy, '_get_redis', lambda: None)
        monkeypatch.setattr(fhir_proxy.httpx, 'post',
                            _FakeSyntheticHospitalTokenEndpoint(status=401))
        sent = []

        proxy = fhir_proxy.get_proxy()
        _wire(proxy, lambda r: sent.append(r) or httpx.Response(200, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            proxy._client.get('/Patient/1')
        assert sent == []

    def test_the_password_is_never_logged(self, monkeypatch, caplog):
        _synthetic_hospital_env(monkeypatch)
        monkeypatch.setattr(fhir_proxy, '_get_redis', lambda: None)
        monkeypatch.setattr(fhir_proxy.httpx, 'post',
                            _FakeSyntheticHospitalTokenEndpoint())
        with caplog.at_level('DEBUG'):
            proxy = fhir_proxy.get_proxy()
            _wire(proxy, lambda r: httpx.Response(200, json={
                'resourceType': 'Patient', 'id': '1'}))
            proxy.read('Patient', '1')
        assert 'sh-pass-not-real' not in caplog.text
        assert 'sh-token-1' not in caplog.text

    def test_it_needs_a_token_endpoint_of_its_own(self):
        """The Medplum fallback endpoint is wrong for any other grant."""
        with pytest.raises(ValueError):
            fhir_proxy.OAuth2UpstreamProxy(
                SH_BASE, 'u', 'p', grant=fhir_proxy.AUTH_PASSWORD_JSON)


class TestTokensDoNotCrossUpstreams:
    """A token minted by one server is never presented to another.

    MUTATION: give the password grant the Medplum cache key -> red.
    """

    MEDPLUM_TOKEN_URL = 'https://medplum.example/oauth2/token'

    def _post(self, tokens):
        def post(url, **kw):
            request = httpx.Request('POST', url)
            return httpx.Response(200, request=request, json={
                'access_token': tokens[url], 'expires_in': 3600})
        return post

    @pytest.mark.parametrize('redis', [False, True],
                             ids=['in-process', 'redis'])
    def test_each_upstream_gets_its_own_token_both_ways(self, monkeypatch,
                                                        redis):
        store = _DictRedis() if redis else None
        monkeypatch.setattr(fhir_proxy, '_get_redis', lambda: store)
        monkeypatch.setattr(fhir_proxy.httpx, 'post', self._post({
            self.MEDPLUM_TOKEN_URL: 'medplum-token',
            SH_TOKEN_URL: 'synthetic-token'}))

        def medplum():
            return fhir_proxy._fetch_medplum_token(
                'cid', 'csec', self.MEDPLUM_TOKEN_URL)

        def synthetic():
            return fhir_proxy._fetch_medplum_token(
                'sh-user', 'sh-pass', SH_TOKEN_URL,
                grant=fhir_proxy.AUTH_PASSWORD_JSON)

        assert medplum() == 'medplum-token'
        assert synthetic() == 'synthetic-token'
        assert medplum() == 'medplum-token'
        assert synthetic() == 'synthetic-token'

    def test_the_medplum_redis_key_is_unchanged(self):
        """A running deployment's cached token stays valid across upgrade."""
        assert fhir_proxy._token_cache_key(
            fhir_proxy.AUTH_OAUTH2, 'x', 'y') == 'medplum:access_token'
        assert fhir_proxy._token_cache_key(
            fhir_proxy.AUTH_PASSWORD_JSON, SH_TOKEN_URL,
            'sh-user') != 'medplum:access_token'


class TestBrokenPagingLinksAreDropped:
    """The simulator sends self url "" and next url "&_offset=10".

    MUTATION: return the upstream Bundle.link unfiltered -> red.
    """

    def test_only_absolute_links_survive(self):
        proxy = fhir_proxy.FHIRUpstreamProxy(SH_BASE)
        good = {'relation': 'next',
                'url': 'https://fhir.example/Condition?_offset=10'}
        proxy._client = httpx.Client(
            base_url=SH_BASE,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                'resourceType': 'Bundle', 'type': 'searchset', 'total': 42,
                'link': [{'relation': 'self', 'url': ''},
                         {'relation': 'next', 'url': '&_offset=10'},
                         {'relation': 'previous'}, 'junk', good],
                'entry': []})))
        try:
            bundle, status = proxy.search('Condition', {'patient': '1'})
        finally:
            proxy.close()
        assert status == 200
        assert bundle['link'] == [good]
        assert bundle['total'] == 42
