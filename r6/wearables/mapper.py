"""
Map Open Wearables proprietary timeseries to FHIR R4 Observations.

Open Wearables emits normalized samples with fields like:
  {"kind": "heart_rate", "value": 72, "unit": "bpm",
   "recorded_at": "2025-11-02T14:03:00Z",
   "provider": "garmin", "user_id": "..."}

We translate those into FHIR R4 Observations with correct LOINC codes,
UCUM units, and vital-sign / activity / sleep / fitness / laboratory
categories. Unknown fields fall back to a code.text-only Observation so
no data is lost.

The mapper is pure — no I/O. Callers wrap outputs in a FHIR Bundle and
POST through HealthClaw's $ingest-context endpoint where redaction,
audit, and tenant isolation apply like any other read/write path.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# LOINC/UCUM lookup for the metrics we map with clinical codes. Order
# matters only for readability; dispatch is by key.
METRIC_MAP: dict[str, dict[str, str]] = {
    'heart_rate': {
        'loinc': '8867-4',
        'display': 'Heart rate',
        'unit_ucum': '/min',
        'unit_human': 'bpm',
        'category': 'vital-signs',
    },
    'resting_heart_rate': {
        'loinc': '40443-4',
        'display': 'Heart rate --resting',
        'unit_ucum': '/min',
        'unit_human': 'bpm',
        'category': 'vital-signs',
    },
    'heart_rate_variability': {  # SDNN (ms) — Open Wearables default
        'loinc': '80404-7',
        'display': 'R-R interval by Pulse oximetry',
        'unit_ucum': 'ms',
        'unit_human': 'ms',
        'category': 'vital-signs',
    },
    'spo2': {
        'loinc': '59408-5',
        'display': 'Oxygen saturation in Arterial blood by Pulse oximetry',
        'unit_ucum': '%',
        'unit_human': '%',
        'category': 'vital-signs',
    },
    'respiratory_rate': {
        'loinc': '9279-1',
        'display': 'Respiratory rate',
        'unit_ucum': '/min',
        'unit_human': 'breaths/min',
        'category': 'vital-signs',
    },
    'steps': {
        'loinc': '55423-8',
        'display': 'Number of steps in unspecified time Pedometer',
        'unit_ucum': '{count}',
        'unit_human': 'steps',
        'category': 'activity',
    },
    'sleep_duration': {
        'loinc': '93832-4',
        'display': 'Sleep duration',
        'unit_ucum': 'h',
        'unit_human': 'h',
        'category': 'sleep',
    },
    'vo2max': {
        'loinc': '65757-1',
        'display': 'VO2 maximum',
        'unit_ucum': 'mL/min/kg',
        'unit_human': 'mL/min/kg',
        'category': 'fitness',
    },
    'body_temperature': {
        'loinc': '8310-5',
        'display': 'Body temperature',
        'unit_ucum': 'Cel',
        'unit_human': '°C',
        'category': 'vital-signs',
    },
    'body_weight': {
        'loinc': '29463-7',
        'display': 'Body weight',
        'unit_ucum': 'kg',
        'unit_human': 'kg',
        'category': 'vital-signs',
    },
    'blood_pressure_systolic': {
        'loinc': '8480-6',
        'display': 'Systolic blood pressure',
        'unit_ucum': 'mm[Hg]',
        'unit_human': 'mmHg',
        'category': 'vital-signs',
    },
    'blood_pressure_diastolic': {
        'loinc': '8462-4',
        'display': 'Diastolic blood pressure',
        'unit_ucum': 'mm[Hg]',
        'unit_human': 'mmHg',
        'category': 'vital-signs',
    },
    'blood_glucose': {
        'loinc': '15074-8',
        'display': 'Glucose [Moles/volume] in Blood',
        'unit_ucum': 'mmol/L',
        'unit_human': 'mmol/L',
        'category': 'laboratory',
    },
}


_CATEGORY_CODING = {
    'vital-signs': {
        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
        'code': 'vital-signs',
        'display': 'Vital Signs',
    },
    'activity': {
        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
        'code': 'activity',
        'display': 'Activity',
    },
    'sleep': {
        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
        'code': 'social-history',
        'display': 'Social History',
    },
    'fitness': {
        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
        'code': 'exam',
        'display': 'Exam',
    },
    'laboratory': {
        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
        'code': 'laboratory',
        'display': 'Laboratory',
    },
}


def sample_to_observation(
    sample: dict[str, Any],
    *,
    patient_ref: str,
    provider: str,
    source_base_url: str = 'https://open-wearables.local',
) -> dict[str, Any] | None:
    """
    Map one Open Wearables sample to a FHIR R4 Observation dict.

    Required input keys:
      - kind: metric name (see METRIC_MAP)
      - value: numeric value
      - recorded_at: ISO 8601 timestamp

    Optional:
      - unit: human-readable unit (used only when kind is unmapped)
      - sample_id: idempotency identifier for deduping on re-poll

    Returns None when the sample is malformed (no value or timestamp).
    """
    kind = sample.get('kind')
    value = sample.get('value')
    recorded_at = sample.get('recorded_at') or sample.get('timestamp')

    if value is None or recorded_at is None:
        return None

    try:
        value_num = float(value)
    except (TypeError, ValueError):
        return None

    sample_id = sample.get('sample_id') or str(uuid.uuid4())
    spec = METRIC_MAP.get(kind)

    obs: dict[str, Any] = {
        'resourceType': 'Observation',
        'status': 'final',
        'effectiveDateTime': recorded_at,
        'subject': {'reference': patient_ref},
        'device': {'display': f'{provider} via Open Wearables'},
        'identifier': [{
            'system': f'{source_base_url}/sample',
            'value': sample_id,
        }],
        'meta': {
            'source': source_base_url,
            'tag': [{
                'system': 'https://healthclaw.io/tags',
                'code': 'wearable-sourced',
                'display': 'Wearable-sourced observation',
            }],
        },
    }

    if spec is None:
        # Fallback: no LOINC, preserve the raw kind in code.text. Loggers
        # can pick these up to nominate new metrics for the map.
        logger.warning(
            'Unmapped Open Wearables metric %r — emitting code.text fallback',
            kind,
        )
        obs['code'] = {'text': str(kind) if kind else 'unknown-metric'}
        unit = sample.get('unit')
        if unit:
            obs['valueQuantity'] = {'value': value_num, 'unit': unit}
        else:
            obs['valueQuantity'] = {'value': value_num}
        return obs

    obs['code'] = {
        'coding': [{
            'system': 'http://loinc.org',
            'code': spec['loinc'],
            'display': spec['display'],
        }],
        'text': spec['display'],
    }
    obs['category'] = [{
        'coding': [_CATEGORY_CODING[spec['category']]],
    }]
    obs['valueQuantity'] = {
        'value': value_num,
        'unit': spec['unit_human'],
        'system': 'http://unitsofmeasure.org',
        'code': spec['unit_ucum'],
    }
    return obs


def samples_to_bundle(
    samples: list[dict[str, Any]],
    *,
    patient_ref: str,
    provider: str,
    source_base_url: str = 'https://open-wearables.local',
) -> dict[str, Any]:
    """
    Convert a batch of Open Wearables samples into a FHIR collection Bundle
    ready for POST /Bundle/$ingest-context.
    """
    entries = []
    for s in samples:
        obs = sample_to_observation(
            s,
            patient_ref=patient_ref,
            provider=provider,
            source_base_url=source_base_url,
        )
        if obs is not None:
            entries.append({'resource': obs})
    return {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': entries,
    }
