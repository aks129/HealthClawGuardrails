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
from r6.models import R6Resource, AuditEventRecord
from r6.audit import record_audit_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in demo resources: Patient + Condition (ICD-9) + 3 Obs + MedRequest
# ---------------------------------------------------------------------------

def _built_in_resources() -> list[dict]:
    """Return the default demo resource set."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return [
        {
            "resourceType": "Patient",
            "name": [{"use": "official", "family": "Rivera", "given": ["Maria", "Elena"]}],
            "birthDate": "1985-03-15",
            "gender": "female",
            "address": [{"line": ["123 Clinical Ave"], "city": "Boston", "state": "MA", "postalCode": "02101"}],
            "telecom": [{"system": "phone", "value": "617-555-0198"}],
            "identifier": [{"system": "http://example.org/mrn", "value": "MRN-2026-4471"}],
        },
        {
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm", "code": "250.00", "display": "Diabetes mellitus without mention of complication"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
        },
        {
            "resourceType": "Observation",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "valueQuantity": {"value": 180, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
            "effectiveDateTime": now,
        },
        {
            "resourceType": "Observation",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}]},
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "valueQuantity": {"value": 8.1, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"},
            "effectiveDateTime": now,
        },
        {
            "resourceType": "Observation",
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
            "status": "active",
            "intent": "order",
            "subject": {"reference": "Patient/__PATIENT_ID__"},
            "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975", "display": "Metformin 500 MG Oral Tablet"}]},
        },
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

    for resource in resources:
        rtype = resource.get('resourceType')
        if not rtype:
            continue

        resource_str = json.dumps(resource)
        if patient_id and rtype != 'Patient':
            resource_str = resource_str.replace('__PATIENT_ID__', patient_id)

        try:
            r = R6Resource(
                resource_type=rtype,
                resource_json=resource_str,
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
        except Exception as e:
            logger.warning("Seed failed for %s: %s", rtype, e)

    db.session.commit()
    return created
