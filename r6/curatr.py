"""
Curatr — FHIR Data Quality Evaluation Engine.

Validates coding elements in FHIR resources against public terminology
services and structural rules, then presents issues in plain language
with patient-facing impact descriptions and resolution suggestions.

Public terminology services used (no auth required):
- tx.fhir.org  — HL7 public FHIR terminology server (SNOMED, LOINC, ICD-10)
- NLM Clinical Tables API — ICD-10-CM code lookup and description
- RXNAV API    — RxNorm drug code validation

Fixes are applied with a linked FHIR Provenance resource so the original
source is always traceable.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Public terminology service endpoints (no auth required)
TX_FHIR_ORG = "https://tx.fhir.org/r4"
NLM_ICD10_API = (
    "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"
)
RXNAV_API = "https://rxnav.nlm.nih.gov/REST/rxcui.json"
TERMINOLOGY_TIMEOUT = 5  # seconds

# Code systems that are deprecated / retired
DEPRECATED_SYSTEMS = {
    "http://hl7.org/fhir/sid/icd-9-cm": {
        "message": (
            "ICD-9-CM was retired in October 2015 and is no longer "
            "accepted by most US health systems and insurers."
        ),
        "replacement": "http://hl7.org/fhir/sid/icd-10-cm",
        "replacement_name": "ICD-10-CM",
        "severity": "critical",
    },
    "http://hl7.org/fhir/sid/icd-9": {
        "message": "ICD-9 was retired in October 2015.",
        "replacement": "http://hl7.org/fhir/sid/icd-10-cm",
        "replacement_name": "ICD-10-CM",
        "severity": "critical",
    },
    "2.16.840.1.113883.6.103": {
        "message": (
            "ICD-9-CM (OID 2.16.840.1.113883.6.103) was retired "
            "October 2015."
        ),
        "replacement": "http://hl7.org/fhir/sid/icd-10-cm",
        "replacement_name": "ICD-10-CM",
        "severity": "critical",
    },
}

# Systems that Curatr can actively validate
SUPPORTED_VALIDATION_SYSTEMS = {
    "http://snomed.info/sct": "SNOMED CT",
    "http://loinc.org": "LOINC",
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://hl7.org/fhir/sid/icd-10": "ICD-10",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
}

# Missing-field definitions for Condition
_CONDITION_FIELDS = {
    "code": {
        "message": (
            "Your condition record has no standard medical code attached."
        ),
        "impact": (
            "Without a standard code, this condition cannot be matched "
            "to reference data, insurance records, or clinical guidelines."
        ),
        "severity": "critical",
    },
    "subject": {
        "message": "The condition is not linked to a patient record.",
        "impact": "Unlinked conditions may be ignored by health systems.",
        "severity": "critical",
    },
    "clinicalStatus": {
        "message": (
            "Clinical status is missing — it is not clear whether this "
            "condition is currently active, resolved, or in remission."
        ),
        "impact": (
            "Without clinical status, AI tools and providers may not "
            "know whether this condition needs attention today."
        ),
        "severity": "warning",
    },
    "verificationStatus": {
        "message": (
            "Verification status is missing — it is unclear whether "
            "this diagnosis has been confirmed."
        ),
        "impact": (
            "A missing verification status means there is no indication "
            "of whether this is a confirmed diagnosis, a suspicion, or "
            "an error."
        ),
        "severity": "info",
    },
}

_VALID_CLINICAL_STATUS = {
    "active", "recurrence", "relapse",
    "inactive", "remission", "resolved",
}

_VALID_VERIFICATION_STATUS = {
    "unconfirmed", "provisional", "differential",
    "confirmed", "refuted", "entered-in-error",
}


@dataclass
class CuratrIssue:
    """A single data quality issue found in a FHIR resource."""
    field_path: str
    severity: str          # critical | warning | info | suggestion
    title: str
    plain_language: str
    impact: str
    suggestion: str
    code_system: Optional[str] = None
    code_value: Optional[str] = None
    suggested_value: Optional[dict] = None


@dataclass
class CuratrResult:
    """Result of a Curatr evaluation pass."""
    resource_type: str
    resource_id: str
    issues: list = field(default_factory=list)
    overall_quality: str = "good"
    summary: str = ""
    checked_at: str = ""

    def to_dict(self):
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "overall_quality": self.overall_quality,
            "summary": self.summary,
            "checked_at": self.checked_at,
            "issue_count": len(self.issues),
            "issues": [
                {
                    "field_path": i.field_path,
                    "severity": i.severity,
                    "title": i.title,
                    "plain_language": i.plain_language,
                    "impact": i.impact,
                    "suggestion": i.suggestion,
                    "code_system": i.code_system,
                    "code_value": i.code_value,
                    "suggested_value": i.suggested_value,
                }
                for i in self.issues
            ],
        }


class CuratrEngine:
    """
    Evaluates FHIR resources for data quality issues.

    Checks coding elements against public terminology services and
    structural rules. Designed to be called from a Flask route or
    MCP tool — does not write to the database.
    """

    def __init__(self, timeout=TERMINOLOGY_TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/fhir+json"})

    # ------------------------------------------------------------------ #
    # Public entry points                                                  #
    # ------------------------------------------------------------------ #

    def evaluate(self, resource: dict) -> CuratrResult:
        """Dispatch evaluation to the correct resource handler."""
        resource_type = resource.get("resourceType", "Unknown")
        resource_id = resource.get("id", "unknown")

        if resource_type == "Condition":
            return self._evaluate_condition(resource)

        # Generic fallback: just check any coding elements present
        issues = self._scan_codings(resource, resource_type)
        return self._build_result(resource_type, resource_id, issues)

    def _evaluate_condition(self, condition: dict) -> CuratrResult:
        resource_id = condition.get("id", "unknown")
        issues: list[CuratrIssue] = []

        # 1. Required field checks
        for fname, finfo in _CONDITION_FIELDS.items():
            if not condition.get(fname):
                issues.append(CuratrIssue(
                    field_path=f"Condition.{fname}",
                    severity=finfo["severity"],
                    title=f"Missing {fname}",
                    plain_language=finfo["message"],
                    impact=finfo["impact"],
                    suggestion=(
                        f"Add a {fname} element to your Condition record."
                    ),
                ))

        # 2. clinicalStatus value check
        cs = condition.get("clinicalStatus", {})
        for idx, coding in enumerate(cs.get("coding", [])):
            code = coding.get("code", "")
            if code and code not in _VALID_CLINICAL_STATUS:
                issues.append(CuratrIssue(
                    field_path=f"Condition.clinicalStatus.coding[{idx}]",
                    severity="warning",
                    title="Invalid clinical status code",
                    plain_language=(
                        f"The clinical status code '{code}' is not "
                        "a recognized FHIR value."
                    ),
                    impact=(
                        "Invalid status codes may cause this condition "
                        "to be filtered out or misclassified in reports."
                    ),
                    suggestion=(
                        "Use one of: "
                        + ", ".join(sorted(_VALID_CLINICAL_STATUS))
                        + "."
                    ),
                    code_system=coding.get("system"),
                    code_value=code,
                    suggested_value={"code": "active"},
                ))

        # 3. verificationStatus value check
        vs = condition.get("verificationStatus", {})
        for idx, coding in enumerate(vs.get("coding", [])):
            code = coding.get("code", "")
            if code and code not in _VALID_VERIFICATION_STATUS:
                issues.append(CuratrIssue(
                    field_path=(
                        f"Condition.verificationStatus.coding[{idx}]"
                    ),
                    severity="warning",
                    title="Invalid verification status code",
                    plain_language=(
                        f"The verification status code '{code}' is not "
                        "a recognized FHIR value."
                    ),
                    impact=(
                        "This affects whether the condition is treated "
                        "as confirmed or still under evaluation."
                    ),
                    suggestion=(
                        "Use one of: "
                        + ", ".join(sorted(_VALID_VERIFICATION_STATUS))
                        + "."
                    ),
                    code_system=coding.get("system"),
                    code_value=code,
                    suggested_value={"code": "confirmed"},
                ))

        # 4. Main condition code checks (deprecated system + live lookup)
        code_elem = condition.get("code", {})
        if code_elem:
            issues.extend(self._check_codeable_concept(
                code_elem, "Condition.code"
            ))

        return self._build_result("Condition", resource_id, issues)

    # ------------------------------------------------------------------ #
    # CodeableConcept and Coding checks                                   #
    # ------------------------------------------------------------------ #

    def _check_codeable_concept(
        self, element: dict, path: str
    ) -> list:
        issues = []
        codings = element.get("coding", [])

        if not codings:
            issues.append(CuratrIssue(
                field_path=f"{path}.coding",
                severity="warning",
                title="No structured coding",
                plain_language=(
                    "Your condition has a text description but no "
                    "standard medical code attached."
                ),
                impact=(
                    "Without a standard code, this condition cannot be "
                    "matched to reference data or clinical guidelines."
                ),
                suggestion=(
                    "Ask your provider to add an ICD-10-CM or SNOMED CT "
                    "code for this condition."
                ),
            ))
            return issues

        for idx, coding in enumerate(codings):
            system = coding.get("system", "")
            code = coding.get("code", "")
            display = coding.get("display", "")
            cpath = f"{path}.coding[{idx}]"

            # Deprecated system check (fast, no network)
            if system in DEPRECATED_SYSTEMS:
                dep = DEPRECATED_SYSTEMS[system]
                sys_label = system.split("/")[-1]
                issues.append(CuratrIssue(
                    field_path=cpath,
                    severity=dep["severity"],
                    title="Outdated code system",
                    plain_language=dep["message"],
                    impact=(
                        f"Records using {sys_label} may not be "
                        "recognized by current health IT systems, "
                        "causing matching failures with providers "
                        "and insurers."
                    ),
                    suggestion=(
                        f"Update to {dep['replacement_name']} "
                        f"({dep['replacement']}). Your provider can "
                        "look up the equivalent code."
                    ),
                    code_system=system,
                    code_value=code,
                ))
                continue

            # Unrecognized system — info only, can't validate
            if system and system not in SUPPORTED_VALIDATION_SYSTEMS:
                issues.append(CuratrIssue(
                    field_path=cpath,
                    severity="info",
                    title="Unrecognized code system",
                    plain_language=(
                        f"This record uses a code system ({system}) "
                        "that Curatr cannot validate automatically."
                    ),
                    impact=(
                        "The code may be valid in its source system but "
                        "cannot be verified against a public terminology "
                        "service."
                    ),
                    suggestion=(
                        "Verify this code with your healthcare provider, "
                        "or request it be mapped to ICD-10-CM or "
                        "SNOMED CT."
                    ),
                    code_system=system,
                    code_value=code,
                ))
                continue

            if not system or not code:
                continue

            # Live terminology lookup
            check = self._lookup_code(system, code)
            if check:
                issues.extend(
                    self._issues_from_lookup(
                        cpath, system, code, display, check
                    )
                )

        return issues

    def _scan_codings(self, resource: dict, resource_type: str) -> list:
        """Generic scan: find all coding[] arrays anywhere in a resource."""
        issues = []

        def _recurse(obj, path):
            if isinstance(obj, dict):
                if "system" in obj and "code" in obj:
                    system = obj.get("system", "")
                    if system in DEPRECATED_SYSTEMS:
                        dep = DEPRECATED_SYSTEMS[system]
                        issues.append(CuratrIssue(
                            field_path=path,
                            severity=dep["severity"],
                            title="Outdated code system",
                            plain_language=dep["message"],
                            impact=(
                                "Outdated code system may not be "
                                "recognized by current health IT systems."
                            ),
                            suggestion=(
                                f"Update to {dep['replacement_name']}."
                            ),
                            code_system=system,
                            code_value=obj.get("code"),
                        ))
                for k, v in obj.items():
                    _recurse(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _recurse(item, f"{path}[{i}]")

        _recurse(resource, resource_type)
        return issues

    # ------------------------------------------------------------------ #
    # Terminology service calls                                            #
    # ------------------------------------------------------------------ #

    def _lookup_code(self, system: str, code: str) -> Optional[dict]:
        """Route to the right terminology API for the given system."""
        if system in (
            "http://hl7.org/fhir/sid/icd-10-cm",
            "http://hl7.org/fhir/sid/icd-10",
        ):
            return self._validate_icd10_nlm(code)
        if system == "http://www.nlm.nih.gov/research/umls/rxnorm":
            return self._validate_rxnorm(code)
        if system in ("http://snomed.info/sct", "http://loinc.org"):
            return self._validate_tx_fhir(system, code)
        return None

    def _validate_icd10_nlm(self, code: str) -> Optional[dict]:
        """
        Validate an ICD-10-CM code via NLM Clinical Tables API.
        Returns {'valid': bool, 'display': str|None, 'message': str|None}.
        """
        try:
            resp = self._session.get(
                NLM_ICD10_API,
                params={"sf": "code", "terms": code,
                        "maxList": 1, "df": "code,name"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            # Response: [total, [codes], null, [[code, name], ...]]
            if not data or len(data) < 4:
                return None
            total = data[0]
            results = data[3] or []
            if total == 0 or not results:
                return {
                    "valid": False,
                    "display": None,
                    "message": (
                        f"Code '{code}' was not found in ICD-10-CM."
                    ),
                }
            first = results[0]
            if first[0].upper() == code.upper():
                return {"valid": True, "display": first[1], "message": None}
            return {
                "valid": False,
                "display": None,
                "message": (
                    f"Code '{code}' not found in ICD-10-CM. "
                    f"Closest match: '{first[0]}'."
                ),
            }
        except Exception as exc:
            logger.debug("NLM ICD-10 lookup failed for %s: %s", code, exc)
            return None

    def _validate_rxnorm(self, code: str) -> Optional[dict]:
        """
        Validate an RxNorm RXCUI via RXNAV REST API.
        Returns {'valid': bool, 'display': None, 'message': str|None}.
        """
        try:
            resp = self._session.get(
                RXNAV_API,
                params={"rxcui": code},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            rxcuis = data.get("idGroup", {}).get("rxnormId", [])
            if rxcuis:
                return {"valid": True, "display": None, "message": None}
            return {
                "valid": False,
                "display": None,
                "message": f"RxNorm concept '{code}' not found.",
            }
        except Exception as exc:
            logger.debug("RXNAV lookup failed for %s: %s", code, exc)
            return None

    def _validate_tx_fhir(
        self, system: str, code: str
    ) -> Optional[dict]:
        """
        Validate a code via HL7 public FHIR terminology server (tx.fhir.org).
        Uses CodeSystem/$validate-code; returns FHIR Parameters.
        """
        try:
            resp = self._session.get(
                f"{TX_FHIR_ORG}/CodeSystem/$validate-code",
                params={"system": system, "code": code},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            params = resp.json().get("parameter", [])
            result_p = next(
                (p for p in params if p.get("name") == "result"), None
            )
            display_p = next(
                (p for p in params if p.get("name") == "display"), None
            )
            message_p = next(
                (p for p in params if p.get("name") == "message"), None
            )
            if result_p is None:
                return None
            return {
                "valid": result_p.get("valueBoolean", False),
                "display": (
                    display_p.get("valueString") if display_p else None
                ),
                "message": (
                    message_p.get("valueString") if message_p else None
                ),
            }
        except Exception as exc:
            logger.debug(
                "tx.fhir.org validation failed for %s|%s: %s",
                system, code, exc,
            )
            return None

    # ------------------------------------------------------------------ #
    # Issue builders                                                       #
    # ------------------------------------------------------------------ #

    def _issues_from_lookup(
        self,
        path: str,
        system: str,
        code: str,
        display: str,
        result: dict,
    ) -> list:
        issues = []
        sys_name = SUPPORTED_VALIDATION_SYSTEMS.get(system, system)

        if not result.get("valid"):
            msg = result.get("message") or (
                f"Code '{code}' could not be validated in {sys_name}."
            )
            issues.append(CuratrIssue(
                field_path=path,
                severity="warning",
                title=f"Code not found in {sys_name}",
                plain_language=msg,
                impact=(
                    f"If '{code}' is not a valid {sys_name} code, this "
                    "condition may not match correctly when shared with "
                    "other health systems."
                ),
                suggestion=(
                    f"Check with your provider that '{code}' is the "
                    f"correct {sys_name} code for your condition."
                ),
                code_system=system,
                code_value=code,
            ))
        elif (
            display
            and result.get("display")
            and display.strip().lower() != result["display"].strip().lower()
        ):
            canonical = result["display"]
            issues.append(CuratrIssue(
                field_path=f"{path}.display",
                severity="suggestion",
                title="Display name differs from terminology",
                plain_language=(
                    f"The description says '{display}' but the official "
                    f"{sys_name} description is '{canonical}'."
                ),
                impact=(
                    "Different descriptions for the same code can cause "
                    "confusion when your record is shared with providers."
                ),
                suggestion=(
                    f"Update the display text to: '{canonical}'"
                ),
                code_system=system,
                code_value=code,
                suggested_value={"display": canonical},
            ))

        return issues

    # ------------------------------------------------------------------ #
    # Result construction                                                  #
    # ------------------------------------------------------------------ #

    def _build_result(
        self, resource_type: str, resource_id: str, issues: list
    ) -> CuratrResult:
        severities = [i.severity for i in issues]
        if "critical" in severities:
            quality = "critical"
        elif "warning" in severities:
            quality = "needs-review"
        elif "info" in severities or "suggestion" in severities:
            quality = "review-suggested"
        else:
            quality = "good"

        if not issues:
            summary = (
                "No data quality issues found in this "
                f"{resource_type} record."
            )
        else:
            c = severities.count("critical")
            w = severities.count("warning")
            i = severities.count("info") + severities.count("suggestion")
            parts = []
            if c:
                parts.append(f"{c} critical issue{'s' if c > 1 else ''}")
            if w:
                parts.append(f"{w} warning{'s' if w > 1 else ''}")
            if i:
                parts.append(
                    f"{i} suggestion{'s' if i > 1 else ''}"
                )
            summary = (
                "Found " + " and ".join(parts)
                + f" in your {resource_type} record that may affect "
                "how your health data is understood."
            )

        return CuratrResult(
            resource_type=resource_type,
            resource_id=resource_id,
            issues=issues,
            overall_quality=quality,
            summary=summary,
            checked_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )


# ------------------------------------------------------------------ #
# Fix application (writes to DB)                                      #
# ------------------------------------------------------------------ #

def apply_fix(
    resource_type: str,
    resource_id: str,
    approved_fixes: list,
    patient_intent: str,
    tenant_id: str,
    agent_id: str = "curatr",
) -> dict:
    """
    Apply patient-approved data quality fixes to a FHIR resource.

    Each fix is ``{"field_path": "Condition.code.coding[0].display",
    "new_value": "Type 2 diabetes mellitus without complications"}``.

    Creates a linked Provenance resource and immutable AuditEvents.

    Returns dict with 'updated_resource', 'provenance', 'issues_fixed'.
    """
    # Import here to avoid circular imports at module load time
    from models import db
    from r6.models import R6Resource
    from r6.audit import record_audit_event

    resource = R6Resource.query.filter_by(
        id=resource_id,
        resource_type=resource_type,
        is_deleted=False,
        tenant_id=tenant_id,
    ).first()

    if not resource:
        return {"error": f"{resource_type}/{resource_id} not found"}

    fhir_json = json.loads(resource.resource_json)
    changes_applied = []

    for fix in approved_fixes:
        field_path = fix.get("field_path", "")
        new_value = fix.get("new_value")
        if new_value is None:
            continue
        if _apply_field_fix(fhir_json, field_path, new_value):
            changes_applied.append(fix)

    if not changes_applied:
        return {
            "error": "No valid fixes could be applied",
            "fixes_attempted": len(approved_fixes),
        }

    new_json = json.dumps(
        fhir_json, separators=(",", ":"), sort_keys=True
    )
    resource.update_resource(new_json)

    change_summary = "; ".join(
        f.get("field_path", "?") + " updated"
        for f in changes_applied
    )

    # Build Provenance resource
    provenance_id = str(uuid.uuid4())
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    provenance = {
        "resourceType": "Provenance",
        "id": provenance_id,
        "target": [{"reference": f"{resource_type}/{resource_id}"}],
        "recorded": now_str,
        "activity": {
            "coding": [{
                "system": (
                    "http://terminology.hl7.org/CodeSystem/"
                    "v3-DataOperation"
                ),
                "code": "UPDATE",
                "display": "revise",
            }]
        },
        "agent": [{
            "type": {
                "coding": [{
                    "system": (
                        "http://terminology.hl7.org/CodeSystem/"
                        "provenance-participant-type"
                    ),
                    "code": "author",
                    "display": "Author",
                }]
            },
            "who": {"display": "Patient via HealthClaw Curatr"},
        }],
        "reason": [{
            "coding": [{
                "system": (
                    "http://terminology.hl7.org/CodeSystem/v3-ActReason"
                ),
                "code": "PATADMIN",
                "display": "patient administration",
            }]
        }],
        "extension": [{
            "url": (
                "https://healthclaw.example.org/fhir/StructureDefinition"
                "/curatr-correction"
            ),
            "extension": [
                {"url": "tool", "valueString": "HealthClaw Curatr"},
                {"url": "patient_intent", "valueString": patient_intent},
                {
                    "url": "changes_applied",
                    "valueInteger": len(changes_applied),
                },
                {
                    "url": "change_summary",
                    "valueString": change_summary,
                },
            ],
        }],
    }

    prov_json = json.dumps(
        provenance, separators=(",", ":"), sort_keys=True
    )
    prov_resource = R6Resource(
        resource_type="Provenance",
        resource_json=prov_json,
        resource_id=provenance_id,
        tenant_id=tenant_id,
    )

    try:
        db.session.add(prov_resource)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise RuntimeError(
            f"Failed to commit fix and provenance: {exc}"
        ) from exc

    record_audit_event(
        "update", resource_type, resource_id,
        agent_id=agent_id, tenant_id=tenant_id,
        detail=f"curatr-fix: {change_summary}",
    )
    record_audit_event(
        "create", "Provenance", provenance_id,
        agent_id=agent_id, tenant_id=tenant_id,
        detail=f"curatr-provenance for {resource_type}/{resource_id}",
    )

    return {
        "updated_resource": resource.to_fhir_json(),
        "provenance": provenance,
        "issues_fixed": len(changes_applied),
        "change_summary": change_summary,
    }


def _apply_field_fix(resource: dict, field_path: str, new_value) -> bool:
    """
    Apply a single field update to a FHIR resource dict in place.

    Supports dot-notation paths, stripping a leading resource type prefix.
    Array indices like ``coding[0]`` are supported.

    Returns True if the update was applied.
    """
    parts = field_path.split(".")
    # Strip leading resource-type segment (e.g. "Condition")
    if parts and parts[0][0].isupper():
        parts = parts[1:]
    if not parts:
        return False

    target = resource
    for part in parts[:-1]:
        if "[" in part:
            key, rest = part.split("[", 1)
            idx = int(rest.rstrip("]"))
            lst = target.get(key)
            if not isinstance(lst, list) or idx >= len(lst):
                return False
            target = lst[idx]
        else:
            if part not in target:
                target[part] = {}
            target = target[part]
            if not isinstance(target, dict):
                return False

    final = parts[-1]
    if "[" in final:
        key, rest = final.split("[", 1)
        idx = int(rest.rstrip("]"))
        lst = target.get(key)
        if not isinstance(lst, list) or idx >= len(lst):
            return False
        lst[idx] = new_value
    else:
        target[final] = new_value

    return True
