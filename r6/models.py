"""
R6 FHIR Resource Store Models.

Stores FHIR R6 resources as canonical JSON with minimal envelope fields.
Resources are validated via $validate before writes are committed.
"""

import uuid
import hashlib
import json
from datetime import datetime, timezone
from models import db


class R6Resource(db.Model):
    """
    Minimal resource store for FHIR R6 resources.
    Resources stored as canonical JSON + envelope fields.
    """
    __tablename__ = 'r6_resources'

    id = db.Column(db.String(64), primary_key=True)
    resource_type = db.Column(db.String(64), nullable=False, index=True)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    last_updated = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    resource_json = db.Column(db.Text, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    tenant_id = db.Column(db.String(64), nullable=True, index=True)
    is_deleted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Curator promotion pipeline (action_policy.yaml: curation_policy)
    # States: raw | in_review | curated | rejected
    curation_state = db.Column(db.String(32), nullable=True, default='raw')
    quality_score = db.Column(db.Float, nullable=True)   # 0.0–1.0
    review_needed = db.Column(db.Boolean, default=False)  # issues found

    # Supported resource types for the showcase
    # Phase 1: Core FHIR resources (R4+)
    # Phase 2: R6-specific ballot resources
    # Phase 3: Curatr data quality resources
    # Phase 4: US Core v9 R4 clinical resources (stable, widely deployed)
    SUPPORTED_TYPES = [
        # Phase 1 — Core (R4+)
        'Patient', 'Encounter', 'Observation', 'Bundle',
        'AuditEvent', 'Consent', 'OperationOutcome',
        # Phase 2 — R6-specific (experimental ballot3)
        'Permission', 'SubscriptionTopic', 'Subscription',
        'NutritionIntake', 'NutritionProduct',
        'DeviceAlert', 'DeviceAssociation',
        'Requirements', 'ActorDefinition',
        # Phase 3 — Curatr (data quality)
        'Condition', 'Provenance',
        # Phase 4 — US Core v9 R4 clinical resources
        'AllergyIntolerance', 'Immunization', 'MedicationRequest',
        'Medication', 'MedicationDispense',
        'Procedure', 'DiagnosticReport',
        'CarePlan', 'CareTeam', 'Goal',
        'DocumentReference',
        'Location', 'Organization',
        'Practitioner', 'PractitionerRole', 'RelatedPerson',
        'Coverage', 'ServiceRequest', 'Specimen',
        'FamilyMemberHistory',
    ]

    def __init__(self, resource_type, resource_json, resource_id=None, tenant_id=None):
        self.id = resource_id or str(uuid.uuid4())
        self.resource_type = resource_type
        self.resource_json = resource_json
        self.sha256 = hashlib.sha256(resource_json.encode('utf-8')).hexdigest()
        self.tenant_id = tenant_id
        self.version_id = 1
        self.last_updated = datetime.now(timezone.utc)

    def to_fhir_json(self):
        """Return the stored resource with meta envelope."""
        resource = json.loads(self.resource_json)
        resource['id'] = self.id
        resource['meta'] = {
            'versionId': str(self.version_id),
            'lastUpdated': self.last_updated.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        }
        return resource

    def update_resource(self, new_json):
        """Update resource content, incrementing version."""
        self.resource_json = new_json
        self.sha256 = hashlib.sha256(new_json.encode('utf-8')).hexdigest()
        self.version_id += 1
        self.last_updated = datetime.now(timezone.utc)

    @classmethod
    def is_supported_type(cls, resource_type):
        return resource_type in cls.SUPPORTED_TYPES


class ContextEnvelope(db.Model):
    """
    Context envelope for agent interactions.
    A bounded, policy-stamped package of FHIR resources.
    """
    __tablename__ = 'context_envelopes'

    context_id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=True, index=True)
    patient_ref = db.Column(db.String(128), nullable=False)
    encounter_ref = db.Column(db.String(128), nullable=True)
    window_start = db.Column(db.DateTime, nullable=True)
    window_end = db.Column(db.DateTime, nullable=True)
    redaction_profile = db.Column(db.String(64), default='standard')
    consent_decision = db.Column(db.String(32), default='permit')
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    items = db.relationship('ContextItem', backref='envelope', lazy=True,
                           cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'context_id': self.context_id,
            'tenant_id': self.tenant_id,
            'patient_ref': self.patient_ref,
            'encounter_ref': self.encounter_ref,
            'window_start': self.window_start.isoformat() if self.window_start else None,
            'window_end': self.window_end.isoformat() if self.window_end else None,
            'redaction_profile': self.redaction_profile,
            'consent_decision': self.consent_decision,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'items': [item.to_dict() for item in self.items],
            'item_count': len(self.items)
        }


class ContextItem(db.Model):
    """Individual resource reference within a context envelope."""
    __tablename__ = 'context_items'

    id = db.Column(db.Integer, primary_key=True)
    context_id = db.Column(db.String(64), db.ForeignKey('context_envelopes.context_id'),
                          nullable=False, index=True)
    resource_ref = db.Column(db.String(128), nullable=False)
    resource_version = db.Column(db.String(16), nullable=True)
    slice_name = db.Column(db.String(64), nullable=True)
    sha256 = db.Column(db.String(64), nullable=True)

    def to_dict(self):
        return {
            'resource_ref': self.resource_ref,
            'resource_version': self.resource_version,
            'slice_name': self.slice_name,
            'sha256': self.sha256
        }


class AuditEventRecord(db.Model):
    """
    AuditEvent records for FHIR resource access.
    R6 defines AuditEvent as a record of events relevant for
    operations, privacy, security, maintenance, and performance.

    APPEND-ONLY: AuditEvents are immutable legal records.
    Updates and deletes are blocked at the model level.
    """
    __tablename__ = 'audit_events'

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = db.Column(db.String(32), nullable=False)  # read, create, update, delete, validate
    resource_type = db.Column(db.String(64), nullable=True)
    resource_id = db.Column(db.String(64), nullable=True)
    context_id = db.Column(db.String(64), nullable=True, index=True)
    tenant_id = db.Column(db.String(64), nullable=True, index=True)
    agent_id = db.Column(db.String(128), nullable=True)
    outcome = db.Column(db.String(32), default='success')  # success, failure
    detail = db.Column(db.Text, nullable=True)
    recorded = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_fhir_json(self):
        """Convert to a FHIR R6 AuditEvent-like JSON."""
        # Build entity list carefully to avoid null references
        entity = []
        if self.resource_type and self.resource_id:
            entity.append({
                'what': {
                    'reference': f'{self.resource_type}/{self.resource_id}'
                },
                'role': {
                    'system': 'http://terminology.hl7.org/CodeSystem/object-role',
                    'code': '4',
                    'display': 'Domain Resource'
                }
            })

        return {
            'resourceType': 'AuditEvent',
            'id': self.id,
            'meta': {
                'lastUpdated': self.recorded.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            },
            'type': {
                'system': 'http://dicom.nema.org/resources/ontology/DCM',
                'code': self._map_event_code(),
                'display': self.event_type
            },
            'action': self._map_action_code(),
            'recorded': self.recorded.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            'outcome': {
                'code': {
                    'system': 'http://hl7.org/fhir/audit-event-outcome',
                    'code': '0' if self.outcome == 'success' else '8',
                    'display': 'Success' if self.outcome == 'success' else 'Serious failure'
                }
            },
            'agent': [
                {
                    'who': {'display': self.agent_id or 'system'},
                    'requestor': True
                }
            ],
            'entity': entity
        }

    def _map_event_code(self):
        mapping = {
            'read': '110106', 'create': '110153', 'update': '110153',
            'delete': '110105', 'validate': '110100'
        }
        return mapping.get(self.event_type, '110100')

    def _map_action_code(self):
        mapping = {
            'read': 'R', 'create': 'C', 'update': 'U',
            'delete': 'D', 'validate': 'E'
        }
        return mapping.get(self.event_type, 'E')


# --- Append-only enforcement for AuditEvent ---
# These listeners fire on Session.delete() and dirty flush, preventing
# programmatic mutation of audit records. DROP TABLE (test teardown) is unaffected.

@db.event.listens_for(AuditEventRecord, 'before_update')
def _prevent_audit_update(mapper, connection, target):
    raise RuntimeError('AuditEvent records are immutable and cannot be updated')


@db.event.listens_for(AuditEventRecord, 'before_delete')
def _prevent_audit_delete(mapper, connection, target):
    raise RuntimeError('AuditEvent records are immutable and cannot be deleted')
