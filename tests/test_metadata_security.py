"""
tests/test_metadata_security.py

Reconciliation tests for the security disclosure work:
  1. CapabilityStatement advertises real SMART-on-FHIR security (no more
     "security: none") with an oauth-uris extension carrying live authorize
     and token endpoints.
  2. The privacy policy renders the reconciled (honest) security claims:
     the reference-implementation note and the messaging-platforms posture.
  3. The OpenClaw bot's /start welcome carries the one-time chat-channel
     risk acknowledgment. openclaw/bot.py imports the telegram SDK, which is
     not a test dependency, so this is a source-content assertion rather than
     a live handler invocation.
"""

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. CapabilityStatement security block
# ---------------------------------------------------------------------------

class TestMetadataSecurity:
    def _rest_security(self, client):
        resp = client.get('/r6/fhir/metadata')
        assert resp.status_code == 200
        cs = resp.get_json()
        rest = cs['rest'][0]
        assert 'security' in rest, "rest[0].security must be present (no 'security: none')"
        return rest['security']

    def test_security_service_is_smart_on_fhir(self, client):
        security = self._rest_security(client)
        code = security['service'][0]['coding'][0]['code']
        assert code == 'SMART-on-FHIR'

    def test_security_service_coding_system(self, client):
        security = self._rest_security(client)
        coding = security['service'][0]['coding'][0]
        assert coding['system'] == (
            'http://terminology.hl7.org/CodeSystem/restful-security-service'
        )

    def test_oauth_uris_extension_present(self, client):
        security = self._rest_security(client)
        ext = security['extension'][0]
        assert ext['url'] == (
            'http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris'
        )

    def test_authorize_and_token_uris_nonempty_http(self, client):
        security = self._rest_security(client)
        uris = {
            e['url']: e['valueUri']
            for e in security['extension'][0]['extension']
        }
        for key in ('authorize', 'token'):
            assert key in uris, f'{key} URI must be advertised'
            assert uris[key], f'{key} URI must be non-empty'
            assert uris[key].startswith('http'), f'{key} URI must be an http(s) URL'

    def test_register_uri_present(self, client):
        security = self._rest_security(client)
        uris = {
            e['url']: e['valueUri']
            for e in security['extension'][0]['extension']
        }
        assert uris.get('register', '').startswith('http')

    def test_oauth_uris_match_smart_configuration(self, client):
        """The CapabilityStatement endpoints must match the discovery doc."""
        security = self._rest_security(client)
        cs_uris = {
            e['url']: e['valueUri']
            for e in security['extension'][0]['extension']
        }
        smart = client.get('/r6/fhir/.well-known/smart-configuration').get_json()
        assert cs_uris['authorize'] == smart['authorization_endpoint']
        assert cs_uris['token'] == smart['token_endpoint']
        assert cs_uris['register'] == smart['registration_endpoint']


# ---------------------------------------------------------------------------
# 2. Privacy policy reconciliation
# ---------------------------------------------------------------------------

class TestPrivacyReconciliation:
    def test_privacy_renders(self, client):
        resp = client.get('/privacy')
        assert resp.status_code == 200

    def test_no_universal_authenticated_tenant_claim(self, client):
        body = client.get('/privacy').get_data(as_text=True)
        # The old overclaiming phrasing must be gone.
        assert 'scoped to the authenticated tenant' not in body

    def test_reference_implementation_note_present(self, client):
        body = client.get('/privacy').get_data(as_text=True)
        assert 'Reference Implementation vs. Production' in body
        assert 'tenant-authenticated reads' in body

    def test_messaging_platforms_section_present(self, client):
        body = client.get('/privacy').get_data(as_text=True)
        assert 'Messaging Platforms' in body
        assert 'patient-directed access' in body
        assert 'individual right of access' in body
        # Honest comparative point.
        assert 'exceeds the security posture of typical consumer health' in body


# ---------------------------------------------------------------------------
# 3. Bot /start risk disclosure (source-content check)
# ---------------------------------------------------------------------------

class TestBotStartDisclosure:
    def test_start_has_risk_acknowledgment(self):
        src = (
            Path(__file__).resolve().parent.parent / 'openclaw' / 'bot.py'
        ).read_text(encoding='utf-8')
        assert 'cmd_start' in src
        assert 'risk_line' in src
        assert 'chat apps aren' in src  # apostrophe variant tolerant
        assert '/nophi' in src
