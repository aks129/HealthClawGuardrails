"""
Phase 3 — chat-platform consent.

Covers the consent notice/module, the TelegramBinding consent helpers, and the
step-up-gated /internal/ endpoints the OpenClaw bot calls to record, check, and
adjust a chat's consent + PHI mode.
"""
import pytest

from r6.consent import CONSENT_VERSION, consent_notice, CONSENT_REQUIRED_HINT
from r6.models import TelegramBinding
from r6.stepup import generate_step_up_token

TENANT = 'consent-tenant'
CHAT = 4242


def _hdr_token():
    return generate_step_up_token(TENANT)


# --- Notice / module ---

def test_consent_notice_states_patient_directed_posture():
    notice = consent_notice("Telegram")
    assert "right of access" in notice
    assert "Telegram" in notice            # platform named for the 3rd-party warning
    assert "not medical advice" in notice.lower()
    assert "/privacy" in notice and "/consent" in notice


def test_consent_version_is_set():
    assert isinstance(CONSENT_VERSION, str) and CONSENT_VERSION


# --- Model helpers ---

def test_record_consent_binds_and_marks(app):
    with app.app_context():
        TelegramBinding.record_consent(TENANT, CHAT, CONSENT_VERSION, username='eugene')
        from models import db
        db.session.commit()
        assert TelegramBinding.has_consented(TENANT, CHAT)
        assert TelegramBinding.has_consented(TENANT, CHAT, required_version=CONSENT_VERSION)


def test_has_consented_false_for_unknown_chat(app):
    with app.app_context():
        assert TelegramBinding.has_consented(TENANT, 9999) is False


def test_notice_version_bump_reprompts(app):
    with app.app_context():
        from models import db
        TelegramBinding.record_consent(TENANT, CHAT, 'old-version')
        db.session.commit()
        # consented in general, but a newer required version re-prompts
        assert TelegramBinding.has_consented(TENANT, CHAT) is True
        assert TelegramBinding.has_consented(TENANT, CHAT, required_version=CONSENT_VERSION) is False


def test_set_phi_mode(app):
    with app.app_context():
        from models import db
        TelegramBinding.bind(TENANT, CHAT)
        db.session.commit()
        assert TelegramBinding.set_phi_mode(TENANT, CHAT, 'summary') is True
        db.session.commit()
        assert TelegramBinding.consent_status(TENANT, CHAT)['phi_mode'] == 'summary'
        # invalid mode rejected, unbound chat rejected
        assert TelegramBinding.set_phi_mode(TENANT, CHAT, 'bogus') is False
        assert TelegramBinding.set_phi_mode(TENANT, 1234, 'summary') is False


# --- Endpoints ---

def test_post_consent_records_and_get_reflects(client):
    body = {'tenant_id': TENANT, 'chat_id': CHAT, 'step_up_token': _hdr_token()}
    r = client.post('/r6/fhir/internal/telegram-consent', json=body)
    assert r.status_code == 200
    assert r.get_json()['consented'] is True

    r = client.get('/r6/fhir/internal/telegram-consent',
                   query_string={'tenant_id': TENANT, 'chat_id': CHAT,
                                 'step_up_token': _hdr_token()})
    data = r.get_json()
    assert r.status_code == 200
    assert data['consented'] is True
    assert data['needs_consent'] is False
    assert data['required_version'] == CONSENT_VERSION


def test_consent_requires_step_up(client):
    r = client.post('/r6/fhir/internal/telegram-consent',
                    json={'tenant_id': TENANT, 'chat_id': CHAT})
    assert r.status_code == 401


def test_get_status_unknown_chat_needs_consent(client):
    r = client.get('/r6/fhir/internal/telegram-consent',
                   query_string={'tenant_id': TENANT, 'chat_id': 7777,
                                 'step_up_token': _hdr_token()})
    data = r.get_json()
    assert data['bound'] is False
    assert data['needs_consent'] is True
    assert data['phi_mode'] == 'full'
    # notice is server-rendered so chat clients render a single source of truth
    assert 'right of access' in data['notice']


def test_phi_mode_endpoint_sets_summary(client):
    # bind via consent first
    client.post('/r6/fhir/internal/telegram-consent',
                json={'tenant_id': TENANT, 'chat_id': CHAT, 'step_up_token': _hdr_token()})
    r = client.post('/r6/fhir/internal/telegram-phi-mode',
                    json={'tenant_id': TENANT, 'chat_id': CHAT, 'mode': 'summary',
                          'step_up_token': _hdr_token()})
    assert r.status_code == 200
    assert r.get_json()['phi_mode'] == 'summary'

    r = client.get('/r6/fhir/internal/telegram-consent',
                   query_string={'tenant_id': TENANT, 'chat_id': CHAT,
                                 'step_up_token': _hdr_token()})
    assert r.get_json()['phi_mode'] == 'summary'


def test_phi_mode_invalid_value_rejected(client):
    client.post('/r6/fhir/internal/telegram-consent',
                json={'tenant_id': TENANT, 'chat_id': CHAT, 'step_up_token': _hdr_token()})
    r = client.post('/r6/fhir/internal/telegram-phi-mode',
                    json={'tenant_id': TENANT, 'chat_id': CHAT, 'mode': 'nope',
                          'step_up_token': _hdr_token()})
    assert r.status_code == 400


def test_phi_mode_unbound_chat_rejected(client):
    r = client.post('/r6/fhir/internal/telegram-phi-mode',
                    json={'tenant_id': TENANT, 'chat_id': 55555, 'mode': 'summary',
                          'step_up_token': _hdr_token()})
    assert r.status_code == 400
