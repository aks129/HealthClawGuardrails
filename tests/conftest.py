"""
Test fixtures for the R6 FHIR Showcase.
"""

import os
import pytest

# Set test environment before importing app — prevents file-based DB creation
os.environ['TESTING'] = '1'
os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
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
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    from models import db
    with flask_app.app_context():
        db.create_all()
        # Reset rate limiter between tests to prevent 429 errors
        from r6.rate_limit import _rate_limits
        _rate_limits.clear()
        yield flask_app
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
