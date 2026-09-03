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

    def test_careagents_beta_tester_tenant_and_deletion_stated(self, client):
        # Council ruling 2026-09-02 (D3), derived from what the code does, not
        # from what we would like it to do. The synthetic tenant is
        # r6/seed.py's built-in set (Patient + 1 Condition + 3 Observations +
        # 1 MedicationRequest + intake_questionnaire()); deletion is
        # careagents' DELETE /api/connections/<id> -> /internal/purge-tenant,
        # which purges and commits in the same request (r6/routes.py) and
        # keeps the PHI-free audit rows (r6/purge.py).
        body = client.get('/privacy').get_data(as_text=True)
        assert 'CareAgents beta testers' in body
        assert 'one fictional sample patient' in body
        assert 'purges that tenant' in body
        assert 'audit trail' in body

    def test_careagents_retention_names_everything_that_survives(self, client):
        # A retention statement that lists only the audit trail reads as the
        # exhaustive list. It is not: the CareAgents account row (email,
        # passkey credential, which sources were connected) outlives a records
        # deletion and has no self-serve removal (#554). The first draft of
        # this paragraph also said the tenant held "nothing else about you",
        # which contradicts docs/beta-tester-guide.md's own data table. This
        # is a privacy-policy promise, so the page has to name both survivors.
        body = client.get('/privacy').get_data(as_text=True)
        assert 'nothing else about you' not in body
        assert 'no other health data about you' in body
        assert 'passkey credential' in body
        assert 'deleting your' in body and 'does not delete it' in body
        assert 'support@healthclaw.io' in body


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

    def test_start_promises_no_unimplemented_privacy_control(self):
        """The disclosure must not offer a control that does not exist.

        A summary-only mode was advertised in /start (and listed as a live
        mitigation in the privacy policy) before it was built. Offering a
        privacy control that does nothing is worse than silence: the user
        continues on an unencrypted consumer channel partly *because* they
        were told a mitigation was available. Both claims were removed; this
        test keeps them from returning ahead of an implementation.

        If summary-only ships, assert the real toggle here instead of
        deleting this test.
        """
        src = (
            Path(__file__).resolve().parent.parent / 'openclaw' / 'bot.py'
        ).read_text(encoding='utf-8')
        assert '/nophi' not in src, (
            'bot.py references /nophi — do not advertise summary-only mode '
            'until a toggle is implemented and gates the read formatters')

    def test_privacy_policy_lists_no_unimplemented_mitigation(self, client):
        """Same claim, second location — the policy is a legal document."""
        body = client.get('/privacy').get_data(as_text=True)
        assert 'summary-only mode' not in body.lower(), (
            'privacy policy lists summary-only mode as a mitigation, but it '
            'is not implemented')
