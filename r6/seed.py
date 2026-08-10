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
from r6.audit import AuditWriteError, record_audit_event
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
        # `is_deleted=False` is load-bearing, not defensive. A soft-deleted
        # row is a tombstone: if the demo patient has been purged, this must
        # seed a live one rather than see the tombstone, decide the tenant is
        # already seeded, and leave the demo empty. That is #422's shape
        # (soft-deleted rows counted as present) in a second place, and
        # tests/test_ratchets.py caught it here before it shipped.
        existing = (R6Resource.query
                    .filter_by(tenant_id=tenant_id, resource_type=rtype,
                               id=rid, is_deleted=False)
                    .first()) if rid else None
        if existing is not None:
            # Resolve the placeholder against the patient already on file, or
            # every later resource in this pass points at nothing.
            if rtype == 'Patient':
                patient_id = str(existing.id)
            skipped += 1
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

            if rtype == 'Patient':
                patient_id = str(r.id)

            record_audit_event(
                event_type='create',
                resource_type=rtype,
                resource_id=str(r.id),
                tenant_id=tenant_id,
                agent_id='seed',
                detail='seeded via auto-seed on first boot',
            )
            created += 1
        except AuditWriteError:
            # NOT a per-resource problem: the guardrail itself is broken, so
            # every subsequent resource would land unaudited too. Propagate
            # instead of logging 7 warnings and answering 201/created-0 (#182).
            db.session.rollback()
            logger.error("Seed aborted: audit trail unavailable for %s", rtype)
            raise
        except Exception as e:
            # Already-seeded resources no longer reach this branch, so anything
            # landing here is unexpected. Roll back the failed insert so it
            # can't poison the final commit; prior resources are already
            # durable (record_audit_event commits).
            db.session.rollback()
            logger.warning("Seed failed for %s: %s", rtype, e)

    db.session.commit()
    if skipped:
        logger.info("Seed complete for %s: %d created, %d already present",
                    tenant_id, created, skipped)
    return created
