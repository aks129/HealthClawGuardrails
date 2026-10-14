"""
R6 FHIR Resource Store Models.

Stores FHIR R6 resources as canonical JSON with minimal envelope fields.
Resources are validated via $validate before writes are committed.
"""

import uuid
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy.orm import validates

from models import db
from r6.audit_ddl import install_statements
# How a record arrived (FHI-42); the code set and the strip/stamp rules live
# in r6/record_source.py. Re-exported here for callers that import the model.
from r6.record_source import (  # noqa: F401
    RECORD_SOURCE_SYSTEM, RECORD_SOURCES, StoredResource, stamp, strip_all)
from r6.us_core_profiles import filter_all as filter_profiles


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

    # Curator promotion pipeline — transitions live in r6/curatr.py
    # persist_curation_state.
    # States: raw | in_review | curated | rejected
    curation_state = db.Column(db.String(32), nullable=True, default='raw')
    quality_score = db.Column(db.Float, nullable=True)   # 0.0–1.0
    review_needed = db.Column(db.Boolean, default=False)  # issues found

    # The channel this row last arrived through, one of RECORD_SOURCES, set
    # only by the ingest path (`_ingest_one(source=...)`) and the built-in
    # sample seed. NULL for rows written before the column existed (no
    # backfill: that would be inference), for engine-authored writes, and
    # after any rewrite that is not an ingest (PUT, demo loop, curatr fix).
    # Never read from the request or the resource's own `meta`.
    source = db.Column(db.String(16), nullable=True)

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
        'MedicationStatement',  # #377 — stored apart, never merged
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

    def __init__(self, resource_type, resource_json, resource_id=None, tenant_id=None,
                 source=None):
        self.id = resource_id or str(uuid.uuid4())
        self.resource_type = resource_type
        self.resource_json = resource_json
        self.sha256 = hashlib.sha256(resource_json.encode('utf-8')).hexdigest()
        self.tenant_id = tenant_id
        self.version_id = 1
        self.last_updated = datetime.now(timezone.utc)
        self.source = source

    @validates('source')
    def _validate_source(self, _key, value):
        # Refused at write time on every ORM path, not only in `_ingest_one`:
        # an off-list value is how a clinic name would get in.
        if value is not None and (not isinstance(value, str)
                                  or value not in RECORD_SOURCES):
            raise ValueError('source must be one of RECORD_SOURCES or None')
        return value

    def to_fhir_json(self):
        """Return the stored resource with meta envelope.

        A `StoredResource`: a dict carrying the channel as `record_source`,
        so redaction can stamp it again after its own strip (see
        r6/record_source.py)."""
        resource = StoredResource(json.loads(self.resource_json))
        # Nested resources (contained[], a contained Bundle's entries) keep
        # their own meta, so strip our system and meta.source at every depth.
        strip_all(resource)
        # Profile claims likewise, at every depth: only allow-listed canonicals
        # (r6/us_core_profiles.py, FHI-52). The top-level list is read before
        # the replace below and is the only thing carried across it.
        filter_profiles(resource)
        meta = resource.get('meta')
        profiles = meta.get('profile') if isinstance(meta, dict) else None
        resource['id'] = self.id
        # Replaced wholesale, so nothing an upstream stamped into meta
        # (meta.source, its tags and their displays, meta.security) surfaces.
        resource['meta'] = {
            'versionId': str(self.version_id),
            'lastUpdated': self.last_updated.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        }
        if profiles:
            resource['meta']['profile'] = profiles
        # Code only, no display, and only for a code in the set: the column
        # is not trusted on read, whatever wrote it.
        if self.source in RECORD_SOURCES:
            resource.record_source = self.source
            stamp(resource, self.source)
        return resource

    def update_resource(self, new_json):
        """Update resource content, incrementing version.

        Clears `source`: the tag never claims a channel we cannot vouch for,
        and rewritten content is no longer what that channel delivered.
        `_ingest_one` sets it again afterwards when the rewrite IS an ingest.
        """
        self.resource_json = new_json
        self.sha256 = hashlib.sha256(new_json.encode('utf-8')).hexdigest()
        self.version_id += 1
        self.last_updated = datetime.now(timezone.utc)
        self.source = None

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
    Updates and deletes are blocked at the model level and, for bulk and
    raw-SQL statements the ORM never sees, by database triggers.
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


# The listeners above never see a bulk Query.update/delete or raw SQL. The
# database triggers do: installed here wherever create_all builds the table,
# and by migration 0009 everywhere else. See r6/audit_ddl.py.
for _dialect in ('sqlite', 'postgresql'):
    for _statement in install_statements(_dialect):
        db.event.listen(
            AuditEventRecord.__table__, 'after_create',
            db.DDL(_statement).execute_if(dialect=_dialect))


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
