"""
R6 FHIR Validator.

Validates resources against R6 core definitions.
Can proxy to the HL7 validator-wrapper when available,
falls back to structural validation for the showcase.
"""

import logging
import os
import time
import requests

logger = logging.getLogger(__name__)

# Validator service URL (HL7 validator-wrapper)
VALIDATOR_URL = os.environ.get('FHIR_VALIDATOR_URL', 'http://localhost:8080')

# Supported FHIR resource types (R4 stable + R6 experimental ballot3)
R6_RESOURCE_TYPES = [
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
    # Phase 4 — US Core v9 R4 clinical resources (stable)
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

# TTL for validator availability cache (seconds)
_AVAILABILITY_TTL = 60


class R6Validator:
    """Validates FHIR R6 resources."""

    def __init__(self, validator_url=None):
        self.validator_url = validator_url or VALIDATOR_URL
        self._validator_available = None
        self._last_availability_check = 0.0

    def validate_resource(self, resource, mode='no-action', profile=None):
        """
        Validate a FHIR R6 resource.

        First tries the external HL7 validator-wrapper.
        Falls back to structural validation if unavailable.

        Args:
            resource: FHIR resource dict
            mode: Validation mode (no-action, create, update, delete)
            profile: Optional profile URL to validate against

        Returns:
            dict with 'valid' (bool) and 'operation_outcome' (FHIR OperationOutcome)
        """
        # Try external validator first
        if self._is_validator_available():
            try:
                return self._validate_external(resource, profile)
            except Exception as e:
                logger.warning(f'External validator failed, falling back to structural: {e}')
                # Invalidate cache on failure so we recheck next time
                self._validator_available = None

        # Structural validation fallback
        return self._validate_structural(resource)

    def _is_validator_available(self):
        """Check if the external validator service is reachable (with TTL cache)."""
        now = time.monotonic()
        if (self._validator_available is not None
                and (now - self._last_availability_check) < _AVAILABILITY_TTL):
            return self._validator_available

        try:
            resp = requests.get(f'{self.validator_url}/health', timeout=2)
            self._validator_available = resp.status_code < 400
        except Exception:
            self._validator_available = False

        self._last_availability_check = now
        return self._validator_available

    def _validate_external(self, resource, profile=None):
        """Validate using the HL7 validator-wrapper service."""
        url = f'{self.validator_url}/validate'
        params = {}
        if profile:
            params['profile'] = profile

        headers = {'Content-Type': 'application/fhir+json'}
        resp = requests.post(
            url, json=resource, params=params, headers=headers, timeout=30
        )

        if resp.status_code == 200:
            outcome = resp.json()
            issues = outcome.get('issue', [])
            has_errors = any(
                i.get('severity') in ('error', 'fatal') for i in issues
            )
            return {
                'valid': not has_errors,
                'operation_outcome': outcome
            }

        # Non-200 response from validator
        return {
            'valid': False,
            'operation_outcome': {
                'resourceType': 'OperationOutcome',
                'issue': [{
                    'severity': 'error',
                    'code': 'exception',
                    'diagnostics': f'Validator returned HTTP {resp.status_code}'
                }]
            }
        }

    def _validate_structural(self, resource):
        """
        Perform basic structural validation on a FHIR resource.
        This is a fallback when the external validator is unavailable.
        """
        issues = []

        # Check resourceType
        resource_type = resource.get('resourceType')
        if not resource_type:
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'resourceType is required',
                'expression': ['resourceType']
            })
            # Can't do type-specific checks without resourceType
            return {
                'valid': False,
                'operation_outcome': {
                    'resourceType': 'OperationOutcome',
                    'issue': issues
                }
            }

        if resource_type not in R6_RESOURCE_TYPES:
            issues.append({
                'severity': 'error',
                'code': 'value',
                'diagnostics': f'Unsupported resource type: {resource_type}',
                'expression': ['resourceType']
            })

        # Resource-specific structural checks
        if resource_type == 'Patient':
            issues.extend(self._validate_patient(resource))
        elif resource_type == 'Observation':
            issues.extend(self._validate_observation(resource))
        elif resource_type == 'Encounter':
            issues.extend(self._validate_encounter(resource))
        elif resource_type == 'Permission':
            issues.extend(self._validate_permission(resource))
        elif resource_type == 'SubscriptionTopic':
            issues.extend(self._validate_subscription_topic(resource))
        elif resource_type == 'Subscription':
            issues.extend(self._validate_subscription(resource))
        elif resource_type == 'NutritionIntake':
            issues.extend(self._validate_nutrition_intake(resource))
        elif resource_type == 'DeviceAlert':
            issues.extend(self._validate_device_alert(resource))
        elif resource_type == 'Condition':
            issues.extend(self._validate_condition(resource))
        elif resource_type == 'Provenance':
            issues.extend(self._validate_provenance(resource))
        # Phase 4 — US Core v9 R4 structural checks
        elif resource_type == 'AllergyIntolerance':
            issues.extend(self._validate_allergy_intolerance(resource))
        elif resource_type == 'Immunization':
            issues.extend(self._validate_immunization(resource))
        elif resource_type == 'MedicationRequest':
            issues.extend(self._validate_medication_request(resource))
        elif resource_type == 'Procedure':
            issues.extend(self._validate_procedure(resource))
        elif resource_type == 'DiagnosticReport':
            issues.extend(self._validate_diagnostic_report(resource))
        elif resource_type == 'DocumentReference':
            issues.extend(self._validate_document_reference(resource))
        elif resource_type == 'Coverage':
            issues.extend(self._validate_coverage(resource))
        elif resource_type == 'ServiceRequest':
            issues.extend(self._validate_service_request(resource))
        elif resource_type == 'Goal':
            issues.extend(self._validate_goal(resource))
        elif resource_type == 'CarePlan':
            issues.extend(self._validate_care_plan(resource))

        has_errors = any(i['severity'] in ('error', 'fatal') for i in issues)

        if not issues:
            issues.append({
                'severity': 'information',
                'code': 'informational',
                'diagnostics': 'Structural validation passed (R4/R6, external validator unavailable)'
            })

        return {
            'valid': not has_errors,
            'operation_outcome': {
                'resourceType': 'OperationOutcome',
                'issue': issues
            }
        }

    def _validate_patient(self, resource):
        """Validate Patient-specific structure."""
        issues = []
        if not resource.get('name') and not resource.get('identifier'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': 'Patient should have at least a name or identifier',
                'expression': ['Patient']
            })
        return issues

    def _validate_observation(self, resource):
        """Validate Observation-specific structure."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Observation.status is required',
                'expression': ['Observation.status']
            })
        if not resource.get('code'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Observation.code is required',
                'expression': ['Observation.code']
            })
        return issues

    def _validate_encounter(self, resource):
        """Validate Encounter-specific structure."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Encounter.status is required',
                'expression': ['Encounter.status']
            })
        return issues

    # --- Phase 2: R6-specific resource validation ---

    def _validate_permission(self, resource):
        """Validate Permission resource (R6 access control)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Permission.status is required (active | entered-in-error | draft | rejected)',
                'expression': ['Permission.status']
            })
        valid_statuses = {'active', 'entered-in-error', 'draft', 'rejected'}
        if resource.get('status') and resource['status'] not in valid_statuses:
            issues.append({
                'severity': 'error',
                'code': 'value',
                'diagnostics': f'Permission.status must be one of: {", ".join(sorted(valid_statuses))}',
                'expression': ['Permission.status']
            })
        if not resource.get('combining'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Permission.combining is required (deny-overrides | permit-overrides | ordered-deny-overrides | ordered-permit-overrides | deny-unless-permit | permit-unless-deny)',
                'expression': ['Permission.combining']
            })
        return issues

    def _validate_subscription_topic(self, resource):
        """Validate SubscriptionTopic resource (R6 event triggers)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'SubscriptionTopic.status is required',
                'expression': ['SubscriptionTopic.status']
            })
        if not resource.get('url'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': 'SubscriptionTopic.url is recommended for discoverability',
                'expression': ['SubscriptionTopic.url']
            })
        return issues

    def _validate_subscription(self, resource):
        """Validate Subscription resource (R6 topic-based)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Subscription.status is required',
                'expression': ['Subscription.status']
            })
        if not resource.get('topic'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Subscription.topic is required (reference to SubscriptionTopic)',
                'expression': ['Subscription.topic']
            })
        return issues

    def _validate_nutrition_intake(self, resource):
        """Validate NutritionIntake resource (R6 nutrition tracking)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'NutritionIntake.status is required',
                'expression': ['NutritionIntake.status']
            })
        if not resource.get('consumedItem'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': 'NutritionIntake.consumedItem is recommended',
                'expression': ['NutritionIntake.consumedItem']
            })
        return issues

    def _validate_device_alert(self, resource):
        """Validate DeviceAlert resource (R6 medical device alerts)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DeviceAlert.status is required',
                'expression': ['DeviceAlert.status']
            })
        if not resource.get('condition'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': (
                    'DeviceAlert.condition is recommended'
                    ' for alert categorization'
                ),
                'expression': ['DeviceAlert.condition']
            })
        return issues

    def _validate_condition(self, resource):
        """Validate Condition resource (clinical problem/diagnosis)."""
        issues = []
        if not resource.get('code'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Condition.code is required',
                'expression': ['Condition.code']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Condition.subject is required',
                'expression': ['Condition.subject']
            })
        valid_clinical = {
            'active', 'recurrence', 'relapse',
            'inactive', 'remission', 'resolved',
        }
        cs = resource.get('clinicalStatus', {})
        for coding in cs.get('coding', []):
            if coding.get('code') and coding['code'] not in valid_clinical:
                issues.append({
                    'severity': 'warning',
                    'code': 'value',
                    'diagnostics': (
                        f'Condition.clinicalStatus code '
                        f'"{coding["code"]}" is not a valid'
                        f' FHIR value'
                    ),
                    'expression': ['Condition.clinicalStatus'],
                })
        valid_ver = {
            'unconfirmed', 'provisional', 'differential',
            'confirmed', 'refuted', 'entered-in-error',
        }
        vs = resource.get('verificationStatus', {})
        for coding in vs.get('coding', []):
            if coding.get('code') and coding['code'] not in valid_ver:
                issues.append({
                    'severity': 'warning',
                    'code': 'value',
                    'diagnostics': (
                        f'Condition.verificationStatus code '
                        f'"{coding["code"]}" is not a valid'
                        f' FHIR value'
                    ),
                    'expression': ['Condition.verificationStatus'],
                })
        return issues

    def _validate_provenance(self, resource):
        """Validate Provenance resource (data change tracking)."""
        issues = []
        if not resource.get('target'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Provenance.target is required',
                'expression': ['Provenance.target']
            })
        if not resource.get('recorded'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Provenance.recorded is required',
                'expression': ['Provenance.recorded']
            })
        if not resource.get('agent'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Provenance.agent is required',
                'expression': ['Provenance.agent']
            })
        return issues

    # --- Phase 4: US Core v9 R4 resource validation ---

    def _validate_allergy_intolerance(self, resource):
        """Validate AllergyIntolerance (US Core v9)."""
        issues = []
        if not resource.get('clinicalStatus'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'AllergyIntolerance.clinicalStatus is required (US Core)',
                'expression': ['AllergyIntolerance.clinicalStatus']
            })
        if not resource.get('verificationStatus'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'AllergyIntolerance.verificationStatus is required (US Core)',
                'expression': ['AllergyIntolerance.verificationStatus']
            })
        if not resource.get('patient'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'AllergyIntolerance.patient is required',
                'expression': ['AllergyIntolerance.patient']
            })
        return issues

    def _validate_immunization(self, resource):
        """Validate Immunization (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Immunization.status is required',
                'expression': ['Immunization.status']
            })
        if not resource.get('vaccineCode'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Immunization.vaccineCode is required (US Core)',
                'expression': ['Immunization.vaccineCode']
            })
        if not resource.get('patient'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Immunization.patient is required',
                'expression': ['Immunization.patient']
            })
        if not resource.get('occurrenceDateTime') and not resource.get('occurrenceString'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Immunization.occurrence[x] is required',
                'expression': ['Immunization.occurrence[x]']
            })
        return issues

    def _validate_medication_request(self, resource):
        """Validate MedicationRequest (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'MedicationRequest.status is required',
                'expression': ['MedicationRequest.status']
            })
        if not resource.get('intent'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'MedicationRequest.intent is required',
                'expression': ['MedicationRequest.intent']
            })
        has_medication = (
            resource.get('medicationCodeableConcept') or
            resource.get('medicationReference') or
            resource.get('medication')
        )
        if not has_medication:
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'MedicationRequest.medication[x] is required (US Core)',
                'expression': ['MedicationRequest.medication[x]']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'MedicationRequest.subject is required',
                'expression': ['MedicationRequest.subject']
            })
        return issues

    def _validate_procedure(self, resource):
        """Validate Procedure (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Procedure.status is required',
                'expression': ['Procedure.status']
            })
        if not resource.get('code'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Procedure.code is required (US Core)',
                'expression': ['Procedure.code']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Procedure.subject is required',
                'expression': ['Procedure.subject']
            })
        return issues

    def _validate_diagnostic_report(self, resource):
        """Validate DiagnosticReport (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DiagnosticReport.status is required',
                'expression': ['DiagnosticReport.status']
            })
        if not resource.get('code'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DiagnosticReport.code is required (US Core)',
                'expression': ['DiagnosticReport.code']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DiagnosticReport.subject is required',
                'expression': ['DiagnosticReport.subject']
            })
        return issues

    def _validate_document_reference(self, resource):
        """Validate DocumentReference (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DocumentReference.status is required',
                'expression': ['DocumentReference.status']
            })
        if not resource.get('type'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': 'DocumentReference.type is recommended (US Core)',
                'expression': ['DocumentReference.type']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DocumentReference.subject is required (US Core)',
                'expression': ['DocumentReference.subject']
            })
        if not resource.get('content'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'DocumentReference.content is required',
                'expression': ['DocumentReference.content']
            })
        return issues

    def _validate_coverage(self, resource):
        """Validate Coverage (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Coverage.status is required',
                'expression': ['Coverage.status']
            })
        if not resource.get('beneficiary'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Coverage.beneficiary is required (US Core)',
                'expression': ['Coverage.beneficiary']
            })
        if not resource.get('payor'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Coverage.payor is required',
                'expression': ['Coverage.payor']
            })
        return issues

    def _validate_service_request(self, resource):
        """Validate ServiceRequest (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'ServiceRequest.status is required',
                'expression': ['ServiceRequest.status']
            })
        if not resource.get('intent'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'ServiceRequest.intent is required',
                'expression': ['ServiceRequest.intent']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'ServiceRequest.subject is required',
                'expression': ['ServiceRequest.subject']
            })
        if not resource.get('code'):
            issues.append({
                'severity': 'warning',
                'code': 'business-rule',
                'diagnostics': 'ServiceRequest.code is recommended (US Core)',
                'expression': ['ServiceRequest.code']
            })
        return issues

    def _validate_goal(self, resource):
        """Validate Goal (US Core v9)."""
        issues = []
        if not resource.get('lifecycleStatus'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Goal.lifecycleStatus is required',
                'expression': ['Goal.lifecycleStatus']
            })
        if not resource.get('description'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Goal.description is required (US Core)',
                'expression': ['Goal.description']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'Goal.subject is required',
                'expression': ['Goal.subject']
            })
        return issues

    def _validate_care_plan(self, resource):
        """Validate CarePlan (US Core v9)."""
        issues = []
        if not resource.get('status'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'CarePlan.status is required',
                'expression': ['CarePlan.status']
            })
        if not resource.get('intent'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'CarePlan.intent is required',
                'expression': ['CarePlan.intent']
            })
        if not resource.get('subject'):
            issues.append({
                'severity': 'error',
                'code': 'required',
                'diagnostics': 'CarePlan.subject is required',
                'expression': ['CarePlan.subject']
            })
        return issues
