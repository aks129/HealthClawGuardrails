"""
Test fixtures for the R6 FHIR Showcase.
"""

import os
import pytest

# Set test environment before importing app — prevents file-based DB creation.
# setdefault (not assignment) so a pre-set SQLALCHEMY_DATABASE_URI wins: the
# postgres-tests CI lane points here, and the resource-identity migration
# rehearsal (docs/runbooks/resource-identity-migration.md) points the suite at
# a migrated Postgres copy. sqlite:///:memory: is the local/unconfigured default.
os.environ['TESTING'] = '1'
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ['STEP_UP_SECRET'] = 'test-secret-for-hmac-validation'
# Command-center tests assume desktop-demo is publicly readable (mirrors
# the healthclaw.io demo host). In production on Railway this env var is
# unset so PUBLIC_TENANTS is empty and everything requires a session.
os.environ.setdefault('PUBLIC_TENANTS', 'desktop-demo,test-tenant')

# Standard tenant ID for all tests
TEST_TENANT_ID = 'test-tenant'


@pytest.fixture
def app():
    """Create a test Flask application."""
    from main import app as flask_app
    flask_app.config['TESTING'] = True
    # Respect the module-level env var (sqlite by default, Postgres when a CI
    # lane or migration rehearsal pre-sets SQLALCHEMY_DATABASE_URI).
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['SQLALCHEMY_DATABASE_URI']

    from models import db
    with flask_app.app_context():
        db.create_all()
        # Reset rate limiter between tests to prevent 429 errors
        from r6.rate_limit import _rate_limits
        _rate_limits.clear()
        yield flask_app
        # Release the scoped session's connection/transaction before DDL.
        # On SQLite (StaticPool, single shared connection) drop_all() reuses
        # the same connection as db.session so this was never load-bearing —
        # but on Postgres, drop_all() opens a separate pooled connection,
        # and a still-open session transaction (e.g. from an uncommitted
        # SELECT during the test) blocks its DROP TABLE indefinitely.
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture
def tenant_id():
    """Test tenant identifier."""
    return TEST_TENANT_ID


@pytest.fixture
def step_up_token(tenant_id):
    """Generate a valid HMAC-signed step-up token for test tenant."""
    from r6.stepup import generate_step_up_token
    return generate_step_up_token(tenant_id)


@pytest.fixture
def auth_headers(tenant_id, step_up_token):
    """Headers for authenticated write operations."""
    return {
        'X-Tenant-Id': tenant_id,
        'X-Step-Up-Token': step_up_token,
    }


@pytest.fixture
def tenant_headers(tenant_id):
    """Headers for read operations (tenant only, no step-up)."""
    return {'X-Tenant-Id': tenant_id}


@pytest.fixture
def other_tenant_headers():
    """Authenticated read headers for a DIFFERENT, non-public tenant.

    Used by cross-tenant isolation tests: the read-auth gate now requires a
    tenant-bound token for any non-public tenant, so a bare
    X-Tenant-Id no longer reaches the handler. Carrying a *valid* token for
    'other-tenant' makes the isolation assertion stronger — it proves an
    authenticated foreign tenant still cannot see the test tenant's data.
    """
    from r6.stepup import generate_step_up_token
    other = 'other-tenant'
    return {'X-Tenant-Id': other,
            'X-Step-Up-Token': generate_step_up_token(other)}


@pytest.fixture
def sample_patient():
    """Sample FHIR R6 Patient resource."""
    return {
        'resourceType': 'Patient',
        'id': 'test-patient-1',
        'name': [{'family': 'Smith', 'given': ['John']}],
        'gender': 'male',
        'birthDate': '1990-01-15',
        'identifier': [
            {'system': 'http://example.org/mrn', 'value': 'MRN12345678'}
        ],
        'address': [
            {
                'line': ['123 Main St'],
                'city': 'Springfield',
                'state': 'IL',
                'postalCode': '62701',
                'country': 'US'
            }
        ]
    }


@pytest.fixture
def sample_observation():
    """Sample FHIR R6 Observation resource."""
    return {
        'resourceType': 'Observation',
        'id': 'test-obs-1',
        'status': 'final',
        'code': {
            'coding': [
                {
                    'system': 'http://loinc.org',
                    'code': '2339-0',
                    'display': 'Glucose [Mass/volume] in Blood'
                }
            ]
        },
        'subject': {'reference': 'Patient/test-patient-1'},
        'effectiveDateTime': '2024-01-15T10:30:00Z',
        'valueQuantity': {
            'value': 95,
            'unit': 'mg/dL',
            'system': 'http://unitsofmeasure.org',
            'code': 'mg/dL'
        }
    }


@pytest.fixture
def sample_bundle(sample_patient, sample_observation):
    """Sample FHIR R6 Bundle for context ingestion."""
    return {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [
            {'resource': sample_patient},
            {'resource': sample_observation}
        ]
    }


# --- Phase 2 Fixtures ---

@pytest.fixture
def sample_permission():
    """Sample FHIR R6 Permission resource."""
    return {
        'resourceType': 'Permission',
        'id': 'test-permission-1',
        'status': 'active',
        'combining': 'deny-overrides',
        'asserter': {'reference': 'Organization/hospital-1'},
        'rule': [
            {
                'type': 'permit',
                'activity': [{
                    'action': [{'coding': [{'code': 'read'}]}],
                }]
            },
            {
                'type': 'deny',
                'activity': [{
                    'action': [{'coding': [{'code': 'delete'}]}],
                }]
            }
        ]
    }


@pytest.fixture
def sample_subscription_topic():
    """Sample FHIR R6 SubscriptionTopic resource."""
    return {
        'resourceType': 'SubscriptionTopic',
        'id': 'test-topic-1',
        'url': 'http://example.org/fhir/SubscriptionTopic/encounter-admit',
        'status': 'active',
        'title': 'Encounter Admission Events',
        'resourceTrigger': [{
            'description': 'Encounter admission',
            'resource': 'Encounter',
            'supportedInteraction': ['create', 'update'],
        }]
    }


@pytest.fixture
def sample_subscription():
    """Sample FHIR R6 Subscription resource."""
    return {
        'resourceType': 'Subscription',
        'id': 'test-sub-1',
        'status': 'requested',
        'topic': 'http://example.org/fhir/SubscriptionTopic/encounter-admit',
        'reason': 'Monitor admissions',
        'channelType': {'code': 'rest-hook'},
        'endpoint': 'https://agent.example.org/webhooks/admission',
        'content': 'id-only',
    }


@pytest.fixture
def sample_nutrition_intake():
    """Sample FHIR R6 NutritionIntake resource."""
    return {
        'resourceType': 'NutritionIntake',
        'id': 'test-nutrition-1',
        'status': 'completed',
        'subject': {'reference': 'Patient/test-patient-1'},
        'consumedItem': [{
            'type': {'coding': [{'system': 'http://snomed.info/sct', 'code': '226059008', 'display': 'Breakfast cereal'}]},
            'nutritionProduct': {'concept': {'coding': [{'code': '226029003', 'display': 'Corn flakes'}]}},
            'amount': {'value': 1, 'unit': 'serving'}
        }],
    }


@pytest.fixture
def sample_device_alert():
    """Sample FHIR R6 DeviceAlert resource."""
    return {
        'resourceType': 'DeviceAlert',
        'id': 'test-alert-1',
        'status': 'active',
        'condition': {
            'coding': [{
                'system': 'urn:iso:std:iso:11073:10101',
                'code': 'MDC_EVT_HI_GT_LIM',
                'display': 'High limit alarm'
            }]
        },
        'device': {'reference': 'Device/pump-1'},
        'subject': {'reference': 'Patient/test-patient-1'},
    }


# --- Action rail fixtures (r6/actions/rails/*) ---

@pytest.fixture
def action_registry():
    """Fresh registry with the real rails registered (idempotent)."""
    from r6.actions.registry import _clear
    from r6.actions.rails import register_all
    _clear()
    register_all()
    yield
    _clear()
    register_all()


@pytest.fixture
def fake_providers(monkeypatch):
    """Intercept provider HTTP so contract tests never hit the network.
    Returns the recorded call list for assertions.

    r6/actions/rails/__init__.py's _safe_request() calls requests.request()
    (not requests.post/requests.get) so both GET and POST go through one
    code path — patch requests.request, dispatching on the method arg.
    """
    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {'call_id': 'fake-123', 'sid': 'fake-123', 'status': 'completed'}

    def fake_request(method, url, **kw):
        calls.append({'method': method, 'url': url, 'kw': kw})
        return _Resp()

    monkeypatch.setattr('requests.request', fake_request)
    return calls
