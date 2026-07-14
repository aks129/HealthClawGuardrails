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


_AUDIT_OUTCOME_DETAIL_TEXT = {
    'ignored-date': 'ignored unsupported parameter date',
    'ignored-datetime': 'ignored unsupported parameter datetime',
    'ignored-date-datetime': (
        'ignored unsupported parameters date and datetime'),
    'ignored-unnamed': 'ignored unsupported parameters',
    'ignored-date-unnamed': (
        'ignored unsupported parameter date; additional unsupported '
        'parameters ignored'),
    'ignored-datetime-unnamed': (
        'ignored unsupported parameter datetime; additional unsupported '
        'parameters ignored'),
    'ignored-date-datetime-unnamed': (
        'ignored unsupported parameters date and datetime; additional '
        'unsupported parameters ignored'),
}


class R6Resource(db.Model):
    """
    Minimal resource store for FHIR R6 resources.
    Resources stored as canonical JSON + envelope fields.
    """
    __tablename__ = 'r6_resources'

    # Resource identity is (tenant_id, resource_type, id) — COMPOSITE.
    # FHIR ids are only unique per resource type per source server, and this
    # store holds many tenants: with the old global single-column PK, tenant
    # B importing an id tenant A already held (Synthea 'example', Epic
    # numeric ids) hit a PK collision and the resource was silently dropped
    # from the import. Patient/X and Observation/X collided within one
    # tenant, too. Column order here fixes the PK order in the DDL —
    # (tenant_id, resource_type, id) — matching
    # scripts/migrate_resource_identity.py for live Postgres databases.
    #
    # W2 (planned, NOT this migration): a `source` column + ingest
    # Provenance will record which upstream server each row came from, so
    # the same logical resource pulled via two connections can be told apart.
    tenant_id = db.Column(db.String(64), primary_key=True, index=True)
    resource_type = db.Column(db.String(64), primary_key=True, index=True)
    # Real EHR resource ids (Epic in particular) routinely exceed the FHIR
    # 64-char id limit — 65/250 in a live Epic export, max 109. 255 gives
    # headroom while preserving the full id so intra-bundle references
    # (subject.reference "Patient/<id>") still resolve.
    id = db.Column(db.String(255), primary_key=True)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    last_updated = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    resource_json = db.Column(db.Text, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
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
        # Phase 5 — SDC Structured Data Capture
        'Questionnaire', 'QuestionnaireResponse',
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
    # Holds the same FHIR resource ids as R6Resource.id — Epic ids exceed 64,
    # and an audit-insert truncation here rolls back the whole transaction,
    # discarding the resource write too (found live 2026-07-08).
    resource_id = db.Column(db.String(255), nullable=True)
    context_id = db.Column(db.String(64), nullable=True, index=True)
    tenant_id = db.Column(db.String(64), nullable=True, index=True)
    agent_id = db.Column(db.String(128), nullable=True)
    outcome = db.Column(db.String(32), default='success')  # success, failure
    # Only allowlisted codes cross the public FHIR boundary. Generic detail is
    # an internal operational note and is never projected into the response.
    outcome_detail_code = db.Column(db.String(64), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    recorded = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_fhir_json(self):
        """Convert to a FHIR R6 AuditEvent-like JSON."""
        outcome_detail_text = _AUDIT_OUTCOME_DETAIL_TEXT.get(
            self.outcome_detail_code)
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
                },
                **({'detail': [{'text': outcome_detail_text}]}
                   if outcome_detail_text else {}),
            },
            'agent': [
                {
                    'who': {'display': self.agent_id or 'system'},
                    'requestor': True
                }
            ],
            'entity': entity
        }

    @staticmethod
    def ignored_parameters_outcome_code(safe_keys, has_unnamed):
        """Return an allowlisted code for ignored-parameter audit evidence."""
        parts = ['ignored', *safe_keys]
        if has_unnamed:
            parts.append('unnamed')
        code = '-'.join(parts)
        return code if code in _AUDIT_OUTCOME_DETAIL_TEXT else None

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


class TelegramBinding(db.Model):
    """
    Maps a HealthClaw tenant_id to a Telegram chat_id so the Fasten ingest
    webhook can push a "your records are ready" notification back through
    OpenClaw without polling. Created on /start; deleted on /unbind.

    A single tenant may have multiple bindings (shared family tenant where
    several people want notifications); a single chat may bind to multiple
    tenants (e.g. a clinician chat covering several patients) — the (tenant_id,
    chat_id) pair is unique, but neither side alone is.
    """
    __tablename__ = 'telegram_bindings'

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    chat_id = db.Column(db.BigInteger, nullable=False, index=True)
    username = db.Column(db.String(64), nullable=True)
    bound_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'chat_id', name='uq_tenant_chat'),
    )

    @classmethod
    def bind(cls, tenant_id: str, chat_id: int, username: str | None = None) -> 'TelegramBinding':
        """Idempotent bind. Returns the existing row if (tenant, chat) is already mapped."""
        existing = cls.query.filter_by(tenant_id=tenant_id, chat_id=chat_id).first()
        if existing:
            existing.last_seen = datetime.now(timezone.utc)
            if username and existing.username != username:
                existing.username = username
            return existing
        row = cls(tenant_id=tenant_id, chat_id=chat_id, username=username)
        db.session.add(row)
        return row

    @classmethod
    def chat_ids_for_tenant(cls, tenant_id: str) -> list[int]:
        """Return all chat_ids currently bound to a tenant."""
        return [
            r.chat_id for r in cls.query.filter_by(tenant_id=tenant_id).all()
        ]
