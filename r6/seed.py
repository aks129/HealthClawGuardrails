"""
r6/seed.py

Shared seed logic for the demo tenant. Used by:
- main.py auto-seed on first boot (SEED_DEMO_TENANT=1)
- POST /r6/fhir/internal/seed endpoint
- scripts/seed_demo_tenant.py CLI
"""

import json
import logging
from datetime import datetime, timezone

from models import db
from r6.models import R6Resource
from r6.audit import AuditWriteError, add_audit_event
from r6.sdc.intake import intake_questionnaire

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in demo resources: Patient + Condition (ICD-9) + 3 Obs + MedRequest
#
# EVERY resource here carries a fixed `id`, and that is load-bearing rather
# than tidy. railway.toml runs `seed-demo --tenant-id desktop-demo` before
# every deploy. A resource without an id takes a generated UUID, so it was
# inserted again on each of those deploys: production reached 19 Patients and
# 12 diabetes Conditions against a seed set of one and one. Adding a resource
# here without an id silently restores that, for that resource alone.
# tests/test_demo_tenant_stays_one_patient.py holds the line.
# ---------------------------------------------------------------------------

def _built_in_resources() -> list[dict]:
    """Return the default demo resource set."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return [
        {
            "resourceType": "Patient",
            "id": "demo-patient-rivera",
            "name": [{"use": "official", "family": "Rivera", "given": ["Maria", "Elena"]}],
            "birthDate": "1985-03-15",
            "gender": "female",
            "address": [{"line": ["123 Clinical Ave"], "city": "Boston", "state": "MA", "postalCode": "02101"}],
            "telecom": [{"system": "phone", "value": "617-555-0198"}],
            "identifier": [{"system": "http://example.org/mrn", "value": "MRN-2026-4471"}],
        },
        {
            "resourceType": "Condition",
            "id": "demo-condition-dm2",
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm", "code": "250.00", "display": "Diabetes mellitus without mention of complication"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
        },
        {
            "resourceType": "Observation",
            "id": "demo-obs-glucose",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "valueQuantity": {"value": 180, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
            "effectiveDateTime": now,
        },
        {
            "resourceType": "Observation",
            "id": "demo-obs-a1c",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "valueQuantity": {"value": 8.1, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"},
            "effectiveDateTime": now,
        },
        {
            "resourceType": "Observation",
            "id": "demo-obs-bp",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "55284-4", "display": "Blood pressure systolic and diastolic"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "component": [
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic BP"}]}, "valueQuantity": {"value": 138, "unit": "mmHg"}},
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic BP"}]}, "valueQuantity": {"value": 88, "unit": "mmHg"}},
            ],
            "effectiveDateTime": now,
        },
        {
            "resourceType": "MedicationRequest",
            "id": "demo-medreq-metformin",
            "status": "active",
            "intent": "order",
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975", "display": "Metformin 500 MG Oral Tablet"}]},
        },
        intake_questionnaire(),
    ]


# ---------------------------------------------------------------------------
# Core seed function (no Flask request context needed)
# ---------------------------------------------------------------------------

def seed_demo_data(tenant_id: str = 'desktop-demo', resources: list[dict] | None = None) -> int:
    """
    Seed a tenant with demo FHIR resources.

    Args:
        tenant_id: Target tenant (default: desktop-demo)
        resources: Custom resource list; if None, uses built-in demo data

    Returns:
        Number of resources created
    """
    if resources is None:
        resources = _built_in_resources()

    patient_id = None
    created = 0
    skipped = 0

    for resource in resources:
        rtype = resource.get('resourceType')
        if not rtype:
            continue

        resource_str = json.dumps(resource)
        if patient_id and rtype != 'Patient':
            resource_str = resource_str.replace('__PATIENT_ID__', patient_id)

        # Skip what is already seeded, rather than inserting and catching the
        # primary-key collision below. The collision path logged a warning and
        # carried on, which is indistinguishable in a deploy log from a seed
        # that had nothing to do — so the one resource that DID have a stable
        # id failed quietly on every deploy while the six without ids
        # duplicated loudly in the UI and nowhere else.
        rid = resource.get('id')
        # The lookup must SEE a tombstone, and must not count it as present.
        # A soft-deleted row still holds the composite primary key: filtering
        # it out (`is_deleted=False`, as this did until the revive below) sent
        # a deleted demo patient to the insert, which collided with the
        # tombstone, logged a warning and left the demo empty. Counting it as
        # present is #422's shape (soft-deleted rows taken for data). So: a
        # live row is skipped, a tombstone is revived, nothing else inserts.
        existing = (R6Resource.query
                    .filter_by(tenant_id=tenant_id, resource_type=rtype, id=rid)
                    .first()) if rid else None
        if existing is not None and not existing.is_deleted:
            # Resolve the placeholder against the patient already on file, or
            # every later resource in this pass points at nothing.
            if rtype == 'Patient':
                patient_id = str(existing.id)
            skipped += 1
            continue

        if existing is not None:
            # Revive through the ingest path's own upsert rather than a
            # second copy of it: it restores the seed content, lifts the
            # tombstone and writes the 'update' AuditEvent naming the revive
            # in this transaction (#558,
            # tests/test_revive_on_reingest_is_audited.py).
            from r6.fasten.ingester import _ingest_one
            try:
                result, _ = _ingest_one(
                    json.loads(resource_str), tenant_id, agent_id='seed',
                    detail='seeded via auto-seed on first boot')
            except Exception:
                # The revive and its audit row share the transaction, so
                # nothing landed; abort loudly as an audit failure does below.
                db.session.rollback()
                logger.error("Seed aborted: could not revive %s", rtype)
                raise
            if result != 'ok':
                logger.warning("Seed could not revive %s: %s", rtype, result)
                continue
            db.session.commit()
            if rtype == 'Patient':
                patient_id = str(existing.id)
            created += 1
            continue

        try:
            r = R6Resource(
                resource_type=rtype,
                resource_json=resource_str,
                # Preserve the FHIR logical id as the PK so consumers can resolve
                # the resource by it (e.g. GET /Questionnaire/healthclaw-intake).
                resource_id=rid,
                tenant_id=tenant_id,
            )
            db.session.add(r)
            db.session.flush()
        except Exception as e:
            # Already-seeded resources no longer reach this branch, so anything
            # landing here is unexpected. Roll back the failed insert so it
            # can't poison the final commit; prior resources are already
            # durable (each commits with its audit row below).
            db.session.rollback()
            logger.warning("Seed failed for %s: %s", rtype, e)
            continue

        if rtype == 'Patient':
            patient_id = str(r.id)

        try:
            add_audit_event(
                event_type='create',
                resource_type=rtype,
                resource_id=str(r.id),
                tenant_id=tenant_id,
                agent_id='seed',
                detail='seeded via auto-seed on first boot',
            )
            db.session.commit()
        except Exception as exc:
            # NOT a per-resource problem: the guardrail itself is broken, so
            # every subsequent resource would land unaudited too. Propagate
            # instead of logging 7 warnings and answering 201/created-0 (#182).
            db.session.rollback()
            logger.error("Seed aborted: audit trail unavailable for %s", rtype)
            raise AuditWriteError(
                f'audit write failed: {type(exc).__name__}') from exc
        created += 1

    db.session.commit()
    if skipped:
        logger.info("Seed complete for %s: %d created, %d already present",
                    tenant_id, created, skipped)
    return created
