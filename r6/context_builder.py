"""
Context Builder Service.

Ingests patient-centric Bundles and constructs bounded "context envelopes"
with retention, redaction, and caching support.
"""

import collections
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from models import db
from r6.models import R6Resource, ContextEnvelope, ContextItem

logger = logging.getLogger(__name__)

# Default context TTL in minutes
DEFAULT_CONTEXT_TTL_MINUTES = 30

# Default temporal window in days
DEFAULT_WINDOW_DAYS = 90


class ContextBuilder:
    """Builds bounded context envelopes from FHIR Bundles."""

    def __init__(self, default_ttl_minutes=DEFAULT_CONTEXT_TTL_MINUTES,
                 default_window_days=DEFAULT_WINDOW_DAYS):
        self.default_ttl_minutes = default_ttl_minutes
        self.default_window_days = default_window_days

    def ingest_bundle(self, bundle, tenant_id=None):
        """
        Ingest a FHIR Bundle, store resources, and build a context envelope.

        Args:
            bundle: A FHIR Bundle resource (dict)
            tenant_id: Optional tenant identifier

        Returns:
            dict with context_id, resource_count, and envelope summary

        Raises:
            ValueError: If bundle is empty
            Exception: On database errors (caller should rollback)
        """
        entries = bundle.get('entry', [])
        if not entries:
            raise ValueError('Bundle contains no entries')

        # Find the patient anchor
        patient_ref = self._find_patient_ref(entries)
        encounter_ref = self._find_encounter_ref(entries)

        # Store resources and collect items
        stored_resources = []
        context_items = []
        # WHICH types we dropped, not just how many we kept. This is the one
        # synchronous ingest path — the caller is holding the response — and
        # it reported `resource_count` alone, so a bundle whose medications
        # arrived as MedicationStatement looked identical to one that had no
        # medications in it (#377). Imported here rather than at module level
        # because `r6.fasten` pulls in its blueprint, which imports this
        # module back.
        from r6.fasten.ingester import _RESOURCE_ID_PATTERN, safe_skipped_type
        skipped_types: collections.Counter = collections.Counter()

        try:
            for index, entry in enumerate(entries):
                resource = entry.get('resource')
                if not resource:
                    continue

                resource_type = resource.get('resourceType')
                if not resource_type or not R6Resource.is_supported_type(resource_type):
                    skipped_types[safe_skipped_type(resource)] += 1
                    logger.debug(f'Skipping unsupported resource type: {resource_type}')
                    continue

                # Store canonical JSON (redaction applied at read-time, not write-time)
                resource_json = json.dumps(resource, separators=(',', ':'), sort_keys=True)

                # A caller-supplied id is validated before use, the way the
                # Fasten ingester validates it (#548, the #286 shape on this
                # second path): `resource.get('id', uuid)` kept "" as an id,
                # stored the row under (tenant, type, ""), and let the next
                # blank-id resource of the same type upsert over it. Blank,
                # bool, float, list or object refuses the whole bundle, the
                # message names the entry and never echoes the value; an int
                # is stored as its string; an absent id gets a UUID.
                if 'id' in resource:
                    raw_id = resource['id']
                    if (isinstance(raw_id, bool)
                            or not isinstance(raw_id, (str, int))
                            or not _RESOURCE_ID_PATTERN.fullmatch(str(raw_id))):
                        db.session.rollback()
                        raise ValueError(
                            f'entry {index}: {resource_type}.id is not a '
                            f'valid FHIR id and was refused')
                    resource_id = str(raw_id)
                else:
                    resource_id = str(uuid.uuid4())
                # Create or update the resource (tenant-scoped)
                existing = R6Resource.query.filter_by(
                    id=resource_id, resource_type=resource_type,
                    tenant_id=tenant_id
                ).first()

                if existing:
                    existing.update_resource(resource_json)
                    # A re-ingest of a tombstoned (tenant, type, id) revives
                    # it, as r6/fasten/ingester.py does (#509 defect 2).
                    existing.is_deleted = False
                    r6_resource = existing
                else:
                    r6_resource = R6Resource(
                        resource_type=resource_type,
                        resource_json=resource_json,
                        resource_id=resource_id,
                        tenant_id=tenant_id
                    )
                    db.session.add(r6_resource)

                stored_resources.append(r6_resource)

                # Build context item
                slice_name = self._determine_slice(resource_type)
                context_items.append({
                    'resource_ref': f'{resource_type}/{r6_resource.id}',
                    'resource_version': str(r6_resource.version_id),
                    'slice_name': slice_name,
                    'sha256': r6_resource.sha256
                })

            # Create context envelope
            now = datetime.now(timezone.utc)
            envelope = ContextEnvelope(
                tenant_id=tenant_id,
                patient_ref=patient_ref or 'unknown',
                encounter_ref=encounter_ref,
                window_start=now - timedelta(days=self.default_window_days),
                window_end=now,
                redaction_profile='standard',
                consent_decision='permit',
                expires_at=now + timedelta(minutes=self.default_ttl_minutes)
            )
            db.session.add(envelope)
            db.session.flush()  # Get the context_id

            # Add context items
            for item_data in context_items:
                item = ContextItem(
                    context_id=envelope.context_id,
                    resource_ref=item_data['resource_ref'],
                    resource_version=item_data['resource_version'],
                    slice_name=item_data['slice_name'],
                    sha256=item_data['sha256']
                )
                db.session.add(item)

            db.session.commit()

            return {
                'context_id': envelope.context_id,
                'patient_ref': patient_ref,
                'encounter_ref': encounter_ref,
                'resource_count': len(stored_resources),
                'skipped_count': sum(skipped_types.values()),
                'skipped_types': dict(skipped_types),
                'expires_at': envelope.expires_at.isoformat(),
                'items': context_items
            }

        except ValueError:
            raise  # Re-raise validation errors
        except Exception:
            db.session.rollback()
            raise  # Re-raise after cleanup so caller can handle

    def _find_patient_ref(self, entries):
        """Find the patient reference from Bundle entries."""
        for entry in entries:
            resource = entry.get('resource', {})
            if resource.get('resourceType') == 'Patient':
                rid = resource.get('id', '')
                return f'Patient/{rid}' if rid else None
            # Check subject references in other resources
            subject = resource.get('subject', {})
            if isinstance(subject, dict) and 'reference' in subject:
                ref = subject['reference']
                if ref.startswith('Patient/'):
                    return ref
        return None

    def _find_encounter_ref(self, entries):
        """Find the encounter reference from Bundle entries."""
        for entry in entries:
            resource = entry.get('resource', {})
            if resource.get('resourceType') == 'Encounter':
                rid = resource.get('id', '')
                return f'Encounter/{rid}' if rid else None
        return None

    def _determine_slice(self, resource_type):
        """Determine the context slice name for a resource type."""
        slices = {
            'Patient': 'demographics',
            'Encounter': 'encounters',
            'Observation': 'observations',
            'Consent': 'consent',
            'AuditEvent': 'audit',
            # Phase 2 — R6-specific slices
            'Permission': 'access-control',
            'SubscriptionTopic': 'subscriptions',
            'Subscription': 'subscriptions',
            'NutritionIntake': 'nutrition',
            'NutritionProduct': 'nutrition',
            'DeviceAlert': 'devices',
            'DeviceAssociation': 'devices',
            'Requirements': 'conformance',
            'ActorDefinition': 'conformance',
        }
        return slices.get(resource_type, 'other')

    # Redaction logic is in r6.redaction module (shared with routes)
