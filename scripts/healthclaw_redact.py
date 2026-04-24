"""HealthClaw Guardrails PHI redaction, in-process.

Mirrors the canonical rules applied by the HealthClaw guardrail proxy:
- HumanName truncated to initials
- Address line/city/district/postalCode stripped (state/country kept)
- Identifier values hashed (SHA-256, optional salt)
- birthDate truncated to year
- Phone/email/fax telecom values masked
- text.div and note free-text removed
- Generic PHI keys redacted at any nesting depth for non-FHIR shapes

Import: from healthclaw_redact import redact, RedactionStats
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any


# Keys that indicate PHI in non-FHIR shaped payloads (HealthEx convenience
# responses, flattened dashboards, etc). Matching is case-insensitive on the
# exact key name, not substring, to avoid false positives on "patient_name_id"
# or similar identifier-shaped keys.
GENERIC_PHI_KEYS: frozenset[str] = frozenset(
    k.lower() for k in {
        "name", "fullName", "firstName", "lastName", "middleName", "givenName",
        "familyName", "displayName", "patientName",
        "email", "emailAddress", "phone", "phoneNumber", "mobile", "cell", "fax",
        "ssn", "socialSecurityNumber", "mrn", "medicalRecordNumber", "memberId",
        "subscriberId", "accountNumber",
        "address", "streetAddress", "street", "addressLine", "addressLine1",
        "addressLine2", "city", "postalCode", "zipCode", "zip",
        "dateOfBirth", "dob", "birthdate",
        "driverLicense", "driversLicense", "passportNumber",
    }
)

# FHIR telecom systems whose values are PHI
PHI_TELECOM_SYSTEMS: frozenset[str] = frozenset({"phone", "email", "sms", "fax", "pager"})

# FHIR resourceTypes that carry Patient demographics. For these, we apply
# full FHIR-shaped redaction. For everything else, we only touch identifiers,
# narrative text, and generic PHI keys if present.
DEMOGRAPHIC_RESOURCES: frozenset[str] = frozenset({
    "Patient", "Person", "RelatedPerson", "Practitioner", "PractitionerRole",
})

BIRTHDATE_RE = re.compile(r"^(\d{4})-\d{2}-\d{2}")


@dataclass
class RedactionStats:
    names_truncated: int = 0
    addresses_stripped: int = 0
    identifiers_hashed: int = 0
    telecom_masked: int = 0
    birthdates_coarsened: int = 0
    free_text_dropped: int = 0
    generic_keys_redacted: int = 0

    def merge(self, other: "RedactionStats") -> None:
        self.names_truncated += other.names_truncated
        self.addresses_stripped += other.addresses_stripped
        self.identifiers_hashed += other.identifiers_hashed
        self.telecom_masked += other.telecom_masked
        self.birthdates_coarsened += other.birthdates_coarsened
        self.free_text_dropped += other.free_text_dropped
        self.generic_keys_redacted += other.generic_keys_redacted

    def as_dict(self) -> dict:
        return asdict(self)


def _hash_identifier(value: str, salt: str = "") -> str:
    """SHA-256 prefix of the identifier value. Deterministic when salt is empty."""
    digest = hashlib.sha256((salt + value).encode("utf-8")).hexdigest()
    return f"redacted:sha256:{digest[:16]}"


def _redact_human_name(name: dict, stats: RedactionStats) -> dict:
    """Collapse a HumanName to initials-only. Keeps use/period for context."""
    given = name.get("given") or []
    family = name.get("family")
    initials = []
    if given and isinstance(given[0], str) and given[0]:
        initials.append(given[0][0].upper() + ".")
    if family and isinstance(family, str) and family:
        initials.append(family[0].upper() + ".")
    redacted: dict = {k: v for k, v in name.items() if k in ("use", "period")}
    if initials:
        redacted["text"] = " ".join(initials)
    stats.names_truncated += 1
    return redacted


def _redact_address(addr: dict, stats: RedactionStats) -> dict:
    """Keep state + country. Drop everything that narrows to an individual."""
    kept_keys = {"use", "type", "state", "country", "period"}
    redacted = {k: v for k, v in addr.items() if k in kept_keys}
    stats.addresses_stripped += 1
    return redacted


def _redact_telecom(cp: dict, stats: RedactionStats, salt: str) -> dict:
    system = (cp.get("system") or "").lower()
    if system in PHI_TELECOM_SYSTEMS and "value" in cp:
        redacted = dict(cp)
        redacted["value"] = "***"
        stats.telecom_masked += 1
        return redacted
    return cp


def _redact_identifier(ident: dict, stats: RedactionStats, salt: str) -> dict:
    if "value" in ident and isinstance(ident["value"], str):
        redacted = dict(ident)
        redacted["value"] = _hash_identifier(ident["value"], salt)
        stats.identifiers_hashed += 1
        return redacted
    return ident


def _coarsen_birthdate(value: Any, stats: RedactionStats) -> Any:
    if not isinstance(value, str):
        return value
    match = BIRTHDATE_RE.match(value)
    if match:
        stats.birthdates_coarsened += 1
        return match.group(1)
    if re.match(r"^\d{4}$", value):
        return value
    # Non-standard birthdate string: drop it rather than leak.
    stats.birthdates_coarsened += 1
    return None


def _is_demographic_resource(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("resourceType") in DEMOGRAPHIC_RESOURCES


def _redact_demographic_fields(obj: dict, stats: RedactionStats, salt: str) -> dict:
    out = dict(obj)

    if "name" in out and isinstance(out["name"], list):
        out["name"] = [_redact_human_name(n, stats) if isinstance(n, dict) else n
                       for n in out["name"]]
    elif "name" in out and isinstance(out["name"], dict):
        out["name"] = _redact_human_name(out["name"], stats)

    if "address" in out and isinstance(out["address"], list):
        out["address"] = [_redact_address(a, stats) if isinstance(a, dict) else a
                          for a in out["address"]]

    if "telecom" in out and isinstance(out["telecom"], list):
        out["telecom"] = [_redact_telecom(t, stats, salt) if isinstance(t, dict) else t
                          for t in out["telecom"]]

    if "birthDate" in out:
        new = _coarsen_birthdate(out["birthDate"], stats)
        if new is None:
            out.pop("birthDate")
        else:
            out["birthDate"] = new

    # Drop photo entirely: it's either a URL or inline base64, both PHI.
    if "photo" in out:
        out.pop("photo")
        stats.free_text_dropped += 1

    return out


def _walk(obj: Any, stats: RedactionStats, salt: str, parent_key: str = "") -> Any:
    """Recursive walk. Applies FHIR rules when it sees FHIR shapes, and
    generic PHI rules at any depth."""
    if isinstance(obj, dict):
        # FHIR narrative div: drop entirely.
        if "div" in obj and "status" in obj and set(obj.keys()) <= {"div", "status", "id"}:
            stats.free_text_dropped += 1
            return {"status": obj.get("status"), "div": ""}

        # FHIR Identifier-shaped objects at any location (identifier arrays
        # or nested identifier refs). Heuristic: dict with a value key and
        # either system or type but no resourceType.
        if (
            "value" in obj
            and "resourceType" not in obj
            and ("system" in obj or "type" in obj or "assigner" in obj)
            and parent_key in ("identifier", "identifiers", "subscriberId", "memberId")
        ):
            return _redact_identifier(obj, stats, salt)

        out: dict = {}
        for k, v in obj.items():
            lk = k.lower()

            # Generic PHI keys in non-FHIR shapes get wiped.
            if lk in GENERIC_PHI_KEYS and not _looks_like_fhir_field(k, v, obj):
                out[k] = _redact_generic_value(lk, v, stats, salt)
                stats.generic_keys_redacted += 1
                continue

            # Free-text FHIR fields.
            if k in ("note",) and isinstance(v, list):
                stats.free_text_dropped += len(v)
                out[k] = []
                continue
            if k in ("comment", "description") and isinstance(v, str) and parent_key in (
                "Observation", "Condition", "Procedure", "DiagnosticReport",
            ):
                stats.free_text_dropped += 1
                out[k] = ""
                continue

            out[k] = _walk(v, stats, salt, parent_key=k)

        # If this is a demographic resource, apply structured rules after the walk.
        if _is_demographic_resource(out):
            out = _redact_demographic_fields(out, stats, salt)

        return out

    if isinstance(obj, list):
        return [_walk(item, stats, salt, parent_key=parent_key) for item in obj]

    return obj


def _looks_like_fhir_field(key: str, value: Any, parent: dict) -> bool:
    """Don't redact a generic key if it's part of a FHIR structure we already
    handle via _redact_demographic_fields. Example: a Patient.name array
    gets processed as FHIR, not as a generic 'name' key."""
    if key == "name" and isinstance(value, list) and parent.get("resourceType") in DEMOGRAPHIC_RESOURCES:
        return True
    if key == "address" and isinstance(value, list) and parent.get("resourceType") in DEMOGRAPHIC_RESOURCES:
        return True
    if key == "birthDate" and parent.get("resourceType") in DEMOGRAPHIC_RESOURCES:
        return True
    return False


def _redact_generic_value(lower_key: str, value: Any, stats: RedactionStats, salt: str) -> Any:
    if lower_key in {"dateofbirth", "dob", "birthdate"}:
        return _coarsen_birthdate(value, stats) if isinstance(value, str) else None
    if lower_key in {"ssn", "mrn", "medicalrecordnumber", "memberid", "subscriberid",
                     "accountnumber", "driverlicense", "driverslicense", "passportnumber"}:
        if isinstance(value, str):
            return _hash_identifier(value, salt)
        return None
    if lower_key in {"address", "streetaddress", "street", "addressline", "addressline1",
                     "addressline2", "city", "postalcode", "zipcode", "zip"}:
        return None
    if lower_key in {"email", "emailaddress", "phone", "phonenumber", "mobile", "cell", "fax"}:
        return "***"
    # name variants
    return None


def redact(payload: Any, salt: str | None = None) -> tuple[Any, RedactionStats]:
    """Apply PHI redaction to an arbitrary JSON-shaped payload.

    Returns (redacted_payload, stats). Never mutates the input.
    """
    if salt is None:
        salt = os.environ.get("HEALTHCLAW_REDACT_SALT", "")
    stats = RedactionStats()
    redacted = _walk(payload, stats, salt)
    return redacted, stats


# Proxy mode: POST to a running HealthClaw guardrail endpoint.
def redact_via_proxy(payload: Any, healthclaw_url: str, tenant_id: str) -> tuple[Any, RedactionStats]:
    """Send payload to the HealthClaw guardrail proxy for server-side redaction.

    Returns the proxy's redacted response. Stats come back in an X-Redaction-Stats
    header; if missing, we return a zero-filled stats object.
    """
    import httpx  # local import so local mode has no httpx hard dep

    endpoint = healthclaw_url.rstrip("/") + "/r6/fhir/internal/redact"
    headers = {"X-Tenant-ID": tenant_id, "Content-Type": "application/json"}
    resp = httpx.post(endpoint, json=payload, headers=headers, timeout=30.0)
    resp.raise_for_status()

    stats = RedactionStats()
    raw = resp.headers.get("X-Redaction-Stats")
    if raw:
        import json as _json
        try:
            parsed = _json.loads(raw)
            for k in stats.__dict__:
                if k in parsed:
                    setattr(stats, k, int(parsed[k]))
        except Exception:
            pass
    return resp.json(), stats
