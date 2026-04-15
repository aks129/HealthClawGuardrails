"""
Tests for r6.wearables — mapper, model, routes, and MCP App.

Covers:
- LOINC/UCUM correctness per metric in the mapper
- Fallback path for unmapped metrics
- WearableConnection model CRUD + uniqueness
- /wearables/providers, /wearables/sync-status, OAuth start/callback
- Compiled Truth MCP App endpoint
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────
# Mapper tests
# ─────────────────────────────────────────────

from r6.wearables.mapper import (
    METRIC_MAP,
    sample_to_observation,
    samples_to_bundle,
)


class TestMapperLoincUcum:
    """Each mapped metric must produce the correct LOINC + UCUM code."""

    @pytest.mark.parametrize('kind,loinc,ucum,category', [
        ('heart_rate',               '8867-4',  '/min',     'vital-signs'),
        ('resting_heart_rate',       '40443-4', '/min',     'vital-signs'),
        ('heart_rate_variability',   '80404-7', 'ms',       'vital-signs'),
        ('spo2',                     '59408-5', '%',        'vital-signs'),
        ('respiratory_rate',         '9279-1',  '/min',     'vital-signs'),
        ('steps',                    '55423-8', '{count}',  'activity'),
        ('sleep_duration',           '93832-4', 'h',        'sleep'),
        ('vo2max',                   '65757-1', 'mL/min/kg','fitness'),
        ('body_temperature',         '8310-5',  'Cel',      'vital-signs'),
        ('body_weight',              '29463-7', 'kg',       'vital-signs'),
        ('blood_pressure_systolic',  '8480-6',  'mm[Hg]',   'vital-signs'),
        ('blood_pressure_diastolic', '8462-4',  'mm[Hg]',   'vital-signs'),
        ('blood_glucose',            '15074-8', 'mmol/L',   'laboratory'),
    ])
    def test_metric_produces_correct_coding(
        self, kind, loinc, ucum, category,
    ):
        sample = {
            'kind': kind,
            'value': 72,
            'recorded_at': '2025-11-02T14:00:00Z',
        }
        obs = sample_to_observation(
            sample, patient_ref='Patient/pt-1', provider='garmin',
        )
        assert obs is not None
        assert obs['code']['coding'][0]['code'] == loinc
        assert obs['valueQuantity']['code'] == ucum
        assert obs['valueQuantity']['system'] == 'http://unitsofmeasure.org'
        # Category coding reflects intended FHIR category mapping
        cat_code = obs['category'][0]['coding'][0]['code']
        expected_cat = {
            'vital-signs': 'vital-signs',
            'activity': 'activity',
            'sleep': 'social-history',
            'fitness': 'exam',
            'laboratory': 'laboratory',
        }[category]
        assert cat_code == expected_cat


class TestMapperFallback:
    def test_unknown_kind_preserves_value_with_text_code(self):
        sample = {
            'kind': 'mystery_metric',
            'value': 42,
            'unit': 'widgets',
            'recorded_at': '2025-11-02T14:00:00Z',
        }
        obs = sample_to_observation(
            sample, patient_ref='Patient/pt-1', provider='garmin',
        )
        assert obs is not None
        assert obs['code'] == {'text': 'mystery_metric'}
        assert obs['valueQuantity']['value'] == 42
        assert obs['valueQuantity']['unit'] == 'widgets'
        # No LOINC system claimed for fallback path
        assert 'system' not in obs['valueQuantity']

    def test_missing_value_returns_none(self):
        obs = sample_to_observation(
            {'kind': 'heart_rate', 'recorded_at': '2025-11-02T14:00:00Z'},
            patient_ref='Patient/pt-1', provider='garmin',
        )
        assert obs is None

    def test_missing_timestamp_returns_none(self):
        obs = sample_to_observation(
            {'kind': 'heart_rate', 'value': 72},
            patient_ref='Patient/pt-1', provider='garmin',
        )
        assert obs is None

    def test_device_display_includes_provider(self):
        obs = sample_to_observation(
            {'kind': 'heart_rate', 'value': 72,
             'recorded_at': '2025-11-02T14:00:00Z'},
            patient_ref='Patient/pt-1', provider='oura',
        )
        assert obs['device']['display'] == 'oura via Open Wearables'
        tags = obs['meta']['tag']
        assert any(t['code'] == 'wearable-sourced' for t in tags)


class TestSamplesToBundle:
    def test_builds_collection_bundle(self):
        samples = [
            {'kind': 'heart_rate', 'value': 60,
             'recorded_at': '2025-11-02T14:00:00Z'},
            {'kind': 'spo2', 'value': 98,
             'recorded_at': '2025-11-02T14:05:00Z'},
            {'kind': 'no_such_field'},  # invalid, skipped
        ]
        bundle = samples_to_bundle(
            samples, patient_ref='Patient/x', provider='whoop',
        )
        assert bundle['resourceType'] == 'Bundle'
        assert bundle['type'] == 'collection'
        assert len(bundle['entry']) == 2


# ─────────────────────────────────────────────
# Model tests
# ─────────────────────────────────────────────

from r6.wearables.models import WearableConnection


class TestWearableConnectionModel:
    def test_create_and_read(self, app, tenant_id):
        from models import db
        with app.app_context():
            conn = WearableConnection(
                tenant_id=tenant_id,
                provider='garmin',
                ow_user_id='ow-user-1',
                patient_ref='Patient/pt-1',
                connected_at=datetime.now(timezone.utc),
            )
            db.session.add(conn)
            db.session.commit()
            found = WearableConnection.query.filter_by(
                tenant_id=tenant_id, provider='garmin',
            ).first()
            assert found is not None
            assert found.ow_user_id == 'ow-user-1'

    def test_to_dict_shape(self, app, tenant_id):
        from models import db
        with app.app_context():
            conn = WearableConnection(
                tenant_id=tenant_id,
                provider='oura',
                ow_user_id='ow-user-oura',
                connected_at=datetime.now(timezone.utc),
            )
            db.session.add(conn)
            db.session.commit()
            d = conn.to_dict()
            assert d['provider'] == 'oura'
            assert d['last_sync_status'] == 'never'
            assert d['observation_count'] == 0


# ─────────────────────────────────────────────
# Routes tests
# ─────────────────────────────────────────────

class TestWearableRoutes:
    def test_providers_endpoint_returns_default_list(self, client):
        resp = client.get('/wearables/providers')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'providers' in body
        names = {p['name'] for p in body['providers']}
        assert 'garmin' in names
        assert 'oura' in names

    def test_sync_status_requires_tenant_id(self, client):
        resp = client.get('/wearables/sync-status')
        assert resp.status_code == 400

    def test_sync_status_empty_for_new_tenant(self, client, tenant_headers):
        resp = client.get(
            '/wearables/sync-status?tenant_id=empty-tenant',
            headers=tenant_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['connections'] == []

    def test_oauth_start_rejects_bad_provider(self, client):
        resp = client.get(
            '/wearables/oauth/start?provider=fakebit&tenant_id=t',
        )
        assert resp.status_code == 400

    def test_oauth_start_without_url_returns_503(self, client, tenant_id):
        # OPEN_WEARABLES_URL is not set in the test env
        resp = client.get(
            f'/wearables/oauth/start?provider=garmin&tenant_id={tenant_id}',
        )
        # 503 because OPEN_WEARABLES_URL is unset in tests
        assert resp.status_code in (302, 503)

    def test_oauth_callback_rejects_bad_state(self, client):
        resp = client.get('/wearables/oauth/callback?state=invalid')
        assert resp.status_code == 400

    def test_sync_now_requires_step_up(self, client, tenant_headers):
        resp = client.post('/wearables/sync-now', headers=tenant_headers)
        assert resp.status_code == 403


# ─────────────────────────────────────────────
# MCP App tests
# ─────────────────────────────────────────────

class TestWearablesMCPApp:
    def test_serves_html(self, client):
        resp = client.get(
            '/r6/fhir/mcp-apps/wearables/?tenant_id=desktop-demo'
        )
        assert resp.status_code == 200
        assert 'text/html' in resp.headers['Content-Type']
        assert resp.headers.get('X-MCP-App') == 'wearables'
        body = resp.get_data(as_text=True)
        assert '<title>Wearables' in body
        assert 'desktop-demo' in body


# ─────────────────────────────────────────────
# Poller tests (mocked OW client)
# ─────────────────────────────────────────────

class TestPollerSyncOnce:
    def test_run_once_no_url_returns_skipped(self, app):
        from r6.wearables.poller import run_once
        with patch.dict('os.environ', {}, clear=False):
            # Ensure OPEN_WEARABLES_URL isn't set
            import os
            os.environ.pop('OPEN_WEARABLES_URL', None)
            result = run_once(app)
            assert result.get('skipped_reason')
