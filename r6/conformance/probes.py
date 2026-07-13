"""Guardrail conformance probes.

Runs the seven HealthClaw guardrail properties against a live endpoint (or the
Flask test client) and emits a scorecard. The point: the guardrail claims are
*verifiable*, not marketing — a partner can run this against any deployment and
prove PHI redaction, audit, step-up, human-in-the-loop, tenant isolation,
medical disclaimers, and truthful failure behavior actually hold.

The probes create SYNTHETIC data (obviously-fake PHI tokens) so a live run
against a real deployment never touches patient data.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

# Distinctive synthetic tokens — if any survive a redacted read, redaction failed.
_SSN = "000-00-9999"
_PHONE = "555-867-5309"
_STREET = "42 Conformance Way"
_FAMILY = "Zzyzxbarton"
_GIVEN = "Quintavious"

PROPERTIES = (
    "phi_redaction",
    "audit_trail",
    "step_up_enforcement",
    "human_in_the_loop",
    "tenant_isolation",
    "medical_disclaimer",
    "error_fidelity",
)

_ERROR_FIDELITY_GRADE_ORDER = {"F": 0, "C": 1, "A": 2}
_MCP_INVALID_RESOURCE = "WidgetQuintaviousZzyzxbarton"
_MCP_HOSTILE_URL = "https://db.internal.example/patient/secret"
_REQUEST_ERROR_CODES = {"invalid", "structure", "value", "not-supported"}
_LOCAL_SUPPORTED_PARAMETER_EVIDENCE = {
    "patient", "code", "status", "_lastupdated",
    "_count", "_sort", "_summary", "context-id",
}
_SUPPORTED_SET_RE = re.compile(
    r"(?:^|[.!?]\s+)supported parameters?\s*:\s*([^.!?]+)",
    re.IGNORECASE,
)


def _error_fidelity_grade(grades: list[str]) -> str:
    """Return the weakest executed error-fidelity profile grade."""
    if not grades:
        return "F"
    unknown = set(grades) - set(_ERROR_FIDELITY_GRADE_ORDER)
    if unknown:
        raise ValueError(f"Unknown error-fidelity grade: {sorted(unknown)}")
    return min(grades, key=_ERROR_FIDELITY_GRADE_ORDER.__getitem__)


def _synthetic_patient():
    return {
        "resourceType": "Patient",
        "name": [{"family": _FAMILY, "given": [_GIVEN]}],
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn", "value": _SSN}],
        "telecom": [{"system": "phone", "value": _PHONE}],
        "address": [{"line": [_STREET], "city": "Testville"}],
        "birthDate": "1980-01-01",
    }


def _synthetic_observation(subject_ref: str = "Patient/conformance-subject"):
    # A CLINICAL resource — human-in-the-loop and medical disclaimers apply to
    # clinical types (Observation/Condition/...), not to demographic Patient.
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3",
                             "display": "Potassium"}]},
        "subject": {"reference": subject_ref},
        "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
    }


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ProbeResult:
    key: str
    property: str
    checks: list[Check] = field(default_factory=list)
    note: str = ""
    grade: Optional[str] = None
    coverage: str = "full"
    profiles: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        if self.grade is not None:
            return self.grade == "A"
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def effective_grade(self) -> str:
        if self.grade is not None:
            return self.grade
        return "A" if self.passed else "F"


def _grade(passed: int, total: int) -> str:
    if total == 0:
        return "F"
    frac = passed / total
    if frac >= 0.999:
        return "A"
    if frac >= 5 / 6:
        return "B"
    if frac >= 4 / 6:
        return "C"
    if frac >= 3 / 6:
        return "D"
    return "F"


@dataclass
class ConformanceReport:
    results: list[ProbeResult]
    base: str = ""
    tenant: str = ""

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def score(self) -> tuple[int, int]:
        return sum(r.passed for r in self.results), len(self.results)

    @property
    def grade(self) -> str:
        p, t = self.score
        return _grade(p, t)

    def to_dict(self) -> dict:
        p, t = self.score
        return {
            "target": self.base, "tenant": self.tenant,
            "passed": self.passed, "grade": self.grade,
            "score": {"passed": p, "total": t},
            "properties": [
                {"key": r.key, "property": r.property, "passed": r.passed,
                 "grade": r.grade, "coverage": r.coverage,
                 "profiles": r.profiles, "note": r.note,
                 "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                            for c in r.checks]}
                for r in self.results
            ],
        }

    def render(self) -> str:
        p, t = self.score
        lines = [f"HealthClaw Guardrail Conformance — {self.base or 'local'} "
                 f"[tenant={self.tenant}]",
                 f"  Grade: {self.grade}   ({p}/{t} properties)", ""]
        for r in self.results:
            label = r.property
            if r.grade is not None:
                label += f" — {r.effective_grade} ({r.coverage})"
            lines.append(f"  [{'PASS' if r.passed else 'FAIL'}] {label}")
            for c in r.checks:
                mark = "✓" if c.passed else "✗"
                suffix = f" — {c.detail}" if c.detail and not c.passed else ""
                lines.append(f"        {mark} {c.name}{suffix}")
        return "\n".join(lines)


@dataclass
class ProbeContext:
    tenant: str
    step_up_token: str
    second_tenant: str = "conformance-tenant-b"

    def read_headers(self) -> dict:
        # Include the step-up token so reads succeed on non-public tenants too
        # (a tenant-bound token authorizes reads under READ_AUTH_ENABLED).
        h = {"X-Tenant-Id": self.tenant}
        if self.step_up_token:
            h["X-Step-Up-Token"] = self.step_up_token
        return h

    def write_headers(self) -> dict:
        return {"X-Tenant-Id": self.tenant, "X-Step-Up-Token": self.step_up_token,
                "X-Human-Confirmed": "true",
                "Content-Type": "application/fhir+json"}


# --- HTTP adapters: uniform request(method, path, headers, json_body) -> (status, json, text)

class FlaskProbeClient:
    """Adapter over a Flask test client. Paths are relative to /r6/fhir."""

    def __init__(self, test_client, prefix: str = "/r6/fhir"):
        self._c = test_client
        self._prefix = prefix
        self.base = "local(test-client)"

    def request(self, method, path, headers=None, json_body=None):
        kwargs = {"headers": headers or {}}
        if json_body is not None:
            kwargs["json"] = json_body
        resp = self._c.open(self._prefix + path, method=method, **kwargs)
        try:
            body = resp.get_json()
        except Exception:
            body = None
        return resp.status_code, body, resp.get_data(as_text=True)


class LiveProbeClient:
    """Adapter over `requests` against a base URL."""

    def __init__(self, base_url, session=None, prefix: str = "/r6/fhir"):
        import requests
        self.base = base_url.rstrip("/")
        self._prefix = prefix
        self._s = session or requests

    def request(self, method, path, headers=None, json_body=None):
        url = f"{self.base}{self._prefix}{path}"
        r = self._s.request(method, url, headers=headers or {}, json=json_body,
                            timeout=25)
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code, body, r.text


def _mcp_response_json(response, expected_id=None) -> dict:
    """Decode either JSON or an SSE message from Streamable HTTP."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        body = response.json()
        if (isinstance(body, dict)
                and (expected_id is None or body.get("id") == expected_id)):
            return body
        raise RuntimeError("MCP response was not a JSON object")

    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines()
            if line.startswith("data:")
        )
        if not data:
            continue
        try:
            body = json.loads(data)
        except (TypeError, ValueError):
            continue
        if (isinstance(body, dict)
                and (expected_id is None or body.get("id") == expected_id)
                and ("result" in body or "error" in body)):
            return body
    raise RuntimeError("MCP SSE response contained no JSON message")


class LiveMCPProbeClient:
    """Small Streamable HTTP MCP client used only by optional conformance probes."""

    def __init__(self, mcp_url, session=None, tenant=None, step_up_token=None):
        import requests
        self._url = mcp_url.rstrip("/")
        self._s = session or requests.Session()
        self._session_id = None
        self._protocol_version = "2025-06-18"
        self._tenant = tenant
        self._step_up_token = step_up_token

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._tenant:
            headers["X-Tenant-Id"] = self._tenant
        if self._step_up_token:
            headers["X-Step-Up-Token"] = self._step_up_token
        return headers

    def _initialize(self):
        response = self._s.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "healthclaw-conformance", "version": "1"},
                },
            },
            timeout=15,
        )
        response.raise_for_status()
        body = _mcp_response_json(response, expected_id=1)
        if not isinstance(body, dict) or "error" in body:
            raise RuntimeError("MCP initialize failed")
        self._session_id = response.headers.get("Mcp-Session-Id")
        result = body.get("result", {})
        if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
            self._protocol_version = result["protocolVersion"]

        initialized = self._s.post(
            self._url,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=15,
        )
        initialized.raise_for_status()

    def call_tool(self, name, arguments):
        if self._session_id is None:
            self._initialize()
        response = self._s.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            timeout=25,
        )
        response.raise_for_status()
        body = _mcp_response_json(response, expected_id=2)
        if not isinstance(body, dict) or "error" in body:
            raise RuntimeError("MCP tools/call failed")
        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("MCP tools/call returned no result")
        return result


# --- Guardrail probes ----------------------------------------------------------

def _create_synthetic(client, ctx) -> tuple[Optional[str], object]:
    status, body, _ = client.request(
        "POST", "/Patient", ctx.write_headers(), _synthetic_patient())
    pid = body.get("id") if isinstance(body, dict) else None
    return pid, status


def probe_phi_redaction(client, ctx) -> ProbeResult:
    r = ProbeResult("phi_redaction", "PHI Redaction")
    pid, status = _create_synthetic(client, ctx)
    if not pid:
        r.checks.append(Check("synthetic patient created", False,
                              f"create returned {status}"))
        return r
    r.checks.append(Check("synthetic patient created", True))
    status, body, text = client.request("GET", f"/Patient/{pid}", ctx.read_headers())
    blob = text or json.dumps(body or {})
    r.checks += [
        Check("read succeeds", status == 200, f"status {status}"),
        Check("family name not returned in full", _FAMILY not in blob),
        Check("given name not returned in full", _GIVEN not in blob),
        Check("SSN-class identifier masked", _SSN not in blob),
        Check("phone number stripped", _PHONE not in blob),
        Check("street address stripped", _STREET not in blob),
    ]
    r.note = f"Patient/{pid}"
    return r


def probe_audit_trail(client, ctx) -> ProbeResult:
    r = ProbeResult("audit_trail", "Immutable Audit Trail")
    # Create + read a synthetic resource, then confirm that specific access was
    # recorded. Matching the resource reference (rather than counting) is robust
    # on busy tenants where the Bundle total is page-capped; AuditEvent search is
    # newest-first, so the just-created entries are on the first page.
    pid, _ = _create_synthetic(client, ctx)
    if pid:
        client.request("GET", f"/Patient/{pid}", ctx.read_headers())
    st, body, text = client.request("GET", "/AuditEvent?_count=100", ctx.read_headers())
    blob = text or json.dumps(body or {})
    readable = isinstance(body, dict) and body.get("resourceType") == "Bundle"
    r.checks += [
        Check("AuditEvent endpoint readable", readable, f"status {st}"),
        Check("resource access is recorded in the audit trail",
              bool(pid) and f"Patient/{pid}" in blob,
              "no AuditEvent references the accessed resource"),
        Check("no raw SSN in the audit trail", _SSN not in blob,
              "PHI leaked into audit"),
    ]
    return r


def probe_step_up_enforcement(client, ctx) -> ProbeResult:
    r = ProbeResult("step_up_enforcement", "Step-Up Authorization")
    # Write with tenant header only (no step-up token) must be refused.
    status, _, _ = client.request(
        "POST", "/Patient", {"X-Tenant-Id": ctx.tenant,
                             "Content-Type": "application/fhir+json"},
        _synthetic_patient())
    r.checks.append(Check("write without step-up token is rejected (401)",
                          status == 401, f"status {status}"))
    return r


def probe_human_in_the_loop(client, ctx) -> ProbeResult:
    r = ProbeResult("human_in_the_loop", "Human-in-the-Loop")
    # A CLINICAL write with step-up present but human confirmation absent must
    # yield 428. (Demographic Patient writes are not gated by human-in-the-loop.)
    headers = {"X-Tenant-Id": ctx.tenant, "X-Step-Up-Token": ctx.step_up_token,
               "Content-Type": "application/fhir+json"}
    status, _, _ = client.request("POST", "/Observation", headers,
                                  _synthetic_observation())
    r.checks.append(Check("clinical write without human confirmation is blocked (428)",
                          status == 428, f"status {status}"))
    return r


def probe_tenant_isolation(client, ctx) -> ProbeResult:
    r = ProbeResult("tenant_isolation", "Tenant Isolation")
    pid, _ = _create_synthetic(client, ctx)
    if not pid:
        r.checks.append(Check("synthetic patient created", False))
        return r
    # Reading tenant A's resource under a different tenant must not return it.
    status, body, _ = client.request(
        "GET", f"/Patient/{pid}", {"X-Tenant-Id": ctx.second_tenant})
    returned_id = body.get("id") if isinstance(body, dict) else None
    r.checks.append(Check(
        "resource is not readable from another tenant",
        status != 200 or returned_id != pid,
        f"status {status}, id={returned_id}"))
    return r


def probe_medical_disclaimer(client, ctx) -> ProbeResult:
    r = ProbeResult("medical_disclaimer", "Medical Disclaimers")
    # Disclaimers attach to CLINICAL reads — create + read back an Observation.
    status, body, _ = client.request(
        "POST", "/Observation", ctx.write_headers(), _synthetic_observation())
    oid = body.get("id") if isinstance(body, dict) else None
    if not oid:
        r.checks.append(Check("synthetic observation created", False,
                              f"create returned {status}"))
        return r
    status, body, text = client.request(
        "GET", f"/Observation/{oid}", ctx.read_headers())
    blob = (text or "") + json.dumps(body or {})
    has = isinstance(body, dict) and (
        "_disclaimer" in body or "disclaimer" in blob.lower())
    r.checks.append(Check("clinical read carries a medical disclaimer", has,
                          "no disclaimer on the response"))
    r.note = f"Observation/{oid}"
    return r


def _is_operation_outcome(body) -> bool:
    return isinstance(body, dict) and body.get("resourceType") == "OperationOutcome"


def _rejection_grade(status, body, expected_status=400) -> str:
    if status == 200:
        return "F"
    if status == expected_status and _corrective_outcome(
            body, _REQUEST_ERROR_CODES):
        return "A"
    return "C"


def _outcome_names_parameter_and_supported_set(body, parameter: str) -> bool:
    """A local rejection is corrective only if it identifies the bad key and
    tells the caller which search parameters are supported."""
    if not _is_operation_outcome(body):
        return False
    for issue in body.get("issue", []):
        if not isinstance(issue, dict):
            continue
        details = issue.get("details", {})
        text = details.get("text", "") if isinstance(details, dict) else ""
        match = _SUPPORTED_SET_RE.search(text)
        if match is None:
            continue
        declared = {
            token.lower()
            for token in re.findall(
                r"[A-Za-z_][A-Za-z0-9_-]*", match.group(1))
        }
        supported = declared & _LOCAL_SUPPORTED_PARAMETER_EVIDENCE
        parameter_lower = parameter.lower()
        remaining = text[:match.start()] + text[match.end():]
        parameter_token = re.escape(parameter_lower)
        rejection = re.search(
            rf"(?:^|[.!?]\s*)(?:"
            rf"(?:unsupported|unknown|invalid)\s+(?:search\s+)?"
            rf"parameter\s*:?\s*{parameter_token}"
            rf"|{parameter_token}\s+(?:(?:is|was)\s+)?"
            rf"(?:ignored|unsupported|rejected|invalid|unknown)"
            rf"|{parameter_token}\s+is\s+not\s+implemented"
            rf")\s*(?=[.!?]|$)",
            remaining.lower(),
        )
        if (parameter_lower not in declared
                and rejection is not None
                and len(supported) >= 2):
            return True
    return False


def _safe_warning_outcome(outcome) -> bool:
    if not _is_operation_outcome(outcome):
        return False
    if set(outcome) != {"resourceType", "issue"}:
        return False
    issues = outcome.get("issue", [])
    if not isinstance(issues, list) or not issues:
        return False
    return (all(
        isinstance(issue, dict)
        and not set(issue) - {"severity", "code", "details"}
        and issue.get("severity") in {"warning", "information"}
        and issue.get("code") in _REQUEST_ERROR_CODES
        and isinstance(issue.get("details"), dict)
        and set(issue["details"]) == {"text"}
        and isinstance(issue["details"]["text"], str)
        and bool(issue["details"]["text"].strip())
        for issue in issues
    ) and _outcome_omits_hostile_values(outcome))


def _has_outcome_warning(bundle, parameter: str) -> bool:
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return False
    entries = bundle.get("entry", [])
    if not isinstance(entries, list):
        return False
    saw_corrective_warning = False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        search = entry.get("search")
        if not isinstance(search, dict) or search.get("mode") != "outcome":
            continue
        outcome = entry.get("resource")
        if not _safe_warning_outcome(outcome):
            return False
        saw_corrective_warning = (
            saw_corrective_warning
            or _outcome_names_parameter_and_supported_set(outcome, parameter)
        )
    return saw_corrective_warning


def _self_link_omits(bundle, parameter: str) -> bool:
    if not isinstance(bundle, dict):
        return False
    links = bundle.get("link", [])
    self_urls = [link.get("url", "") for link in links
                 if isinstance(link, dict) and link.get("relation") == "self"
                 and isinstance(link.get("url"), str)]
    return bool(self_urls) and all(
        parameter not in {key for key, _ in parse_qsl(
            urlsplit(url).query, keep_blank_values=True)}
        for url in self_urls
    )


def _self_link_includes(bundle, parameter: str, value: str) -> bool:
    if not isinstance(bundle, dict):
        return False
    self_urls = [link.get("url", "") for link in bundle.get("link", [])
                 if isinstance(link, dict) and link.get("relation") == "self"
                 and isinstance(link.get("url"), str)]
    return bool(self_urls) and all(
        (parameter, value) in parse_qsl(
            urlsplit(url).query, keep_blank_values=True)
        for url in self_urls
    )


def _bundle_matches_subject(bundle, subject_ref: str) -> bool:
    """A supposedly bounded search may return only the synthetic subject."""
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return False
    entries = bundle.get("entry", [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        resource = entry.get("resource")
        search = entry.get("search")
        mode = search.get("mode") if isinstance(search, dict) else None
        if _is_operation_outcome(resource) and mode == "outcome":
            continue
        if not isinstance(resource, dict):
            return False
        subject = resource.get("subject")
        if (not isinstance(subject, dict)
                or subject.get("reference") != subject_ref):
            return False
    return True


def _audit_resources(bundle) -> dict[str, dict]:
    if not isinstance(bundle, dict):
        return {}
    resources = {}
    for entry in bundle.get("entry", []):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if (isinstance(resource, dict)
                and isinstance(resource.get("id"), str)):
            resources[resource["id"]] = resource
    return resources


def _audit_events(bundle) -> dict[str, str]:
    events = {}
    for event_id, resource in _audit_resources(bundle).items():
        code = resource.get("outcome", {}).get("code", {}).get("code")
        if isinstance(code, str):
            events[event_id] = code
    return events


def _new_audit_outcome_grade(before, after) -> str:
    """Grade only a uniquely correlated audit event; ambiguity is opaque."""
    new_codes = _new_audit_outcome_codes(before, after)
    if len(new_codes) != 1:
        return "C"
    return "A" if new_codes[0] == "8" else ("F" if new_codes[0] == "0" else "C")


def _new_audit_outcome_codes(before, after) -> list[str]:
    before_ids = set(_audit_events(before))
    return [
        code for event_id, code in _audit_events(after).items()
        if event_id not in before_ids
    ]


def _new_audit_warning_grade(before, after, parameter: str) -> str:
    before_ids = set(_audit_resources(before))
    new_resources = [
        resource for event_id, resource in _audit_resources(after).items()
        if event_id not in before_ids
    ]
    if len(new_resources) != 1:
        return "C"
    outcome = new_resources[0].get("outcome", {})
    if outcome.get("code", {}).get("code") != "0":
        return "F"
    details = outcome.get("detail", [])
    if not isinstance(details, list):
        return "C"
    texts = [detail.get("text", "") for detail in details
             if isinstance(detail, dict)
             and isinstance(detail.get("text"), str)]
    lowered = " ".join(texts).lower()
    if f"{parameter.lower()}=" in lowered or "http://" in lowered or "https://" in lowered:
        return "F"
    if "applied" in lowered and parameter.lower() in lowered:
        return "C"
    if "ignored" in lowered and parameter.lower() in lowered:
        return "A"
    return "C"


def _profile(status: str, *, grade: Optional[str] = None,
             checks=()) -> dict:
    result = {"status": status, "checks": list(checks)}
    if grade is not None:
        result["grade"] = grade
    return result


def _corrective_outcome(body, expected_codes: set[str]) -> bool:
    if not _is_operation_outcome(body):
        return False
    issues = body.get("issue", [])
    if not isinstance(issues, list) or not issues:
        return False
    if set(body) - {"resourceType", "issue"}:
        return False
    saw_expected = False
    for issue in issues:
        if (not isinstance(issue, dict)
                or set(issue) - {"severity", "code", "details"}):
            return False
        details = issue.get("details")
        text = details.get("text") if isinstance(details, dict) else None
        if (issue.get("severity") not in {"fatal", "error"}
                or issue.get("code") not in expected_codes
                or not isinstance(details, dict)
                or set(details) != {"text"}
                or not isinstance(text, str)
                or not text.strip()):
            return False
        saw_expected = True
    return saw_expected and _outcome_omits_hostile_values(body)


def _outcome_omits_hostile_values(value) -> bool:
    blob = json.dumps(value).lower()
    unsafe_tokens = (
        _GIVEN.lower(), _FAMILY.lower(),
        "quintavious zzyzxbarton",
        _MCP_INVALID_RESOURCE.lower(),
        "http://", "https://",
    )
    return not any(token in blob for token in unsafe_tokens)


def _response_omits_hostile_values(body, text) -> bool:
    blob = (text or json.dumps(body or {})).lower()
    return not any(token in blob for token in (
        _GIVEN.lower(), _FAMILY.lower(),
        "quintavious zzyzxbarton", "http://", "https://",
    ))


def _proxy_profile(proxy_client, ctx) -> tuple[str, list[Check]]:
    hostile_name = "Quintavious Zzyzxbarton"
    hostile_url = "https://db.internal.example/patient/secret"

    _, audit_before, _ = proxy_client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())

    rejected_status, rejected_body, rejected_text = proxy_client.request(
        "GET", "/Observation?__conformance_case=rejection", ctx.read_headers())
    rejection_safe = _response_omits_hostile_values(
        rejected_body, rejected_text)
    rejection_ok = (rejected_status == 400
                    and _corrective_outcome(
                        rejected_body,
                        {"invalid", "structure", "value", "not-supported"})
                    and rejection_safe)
    rejection_grade = ("A" if rejection_ok else
                       ("F" if rejected_status == 200 or not rejection_safe else "C"))

    auth_status, auth_body, auth_text = proxy_client.request(
        "GET", "/Patient/conformance-auth", ctx.read_headers())
    # In server-credential proxy mode an upstream 401/403 is deliberately
    # mapped to 502: the caller cannot repair the proxy's credential. In
    # caller-auth mode the original 401/403 remains truthful.
    auth_ok = (auth_status in (401, 403, 502)
               and _corrective_outcome(auth_body, {"security"})
               and _response_omits_hostile_values(auth_body, auth_text))
    auth_grade = "A" if auth_ok else ("F" if auth_status in (200, 404) else "C")

    server_status, server_body, server_text = proxy_client.request(
        "GET", "/Observation?__conformance_case=server", ctx.read_headers())
    server_ok = (server_status >= 500
                 and _corrective_outcome(
                     server_body, {"exception", "transient", "processing"})
                 and _response_omits_hostile_values(server_body, server_text))
    server_grade = "A" if server_ok else ("F" if server_status == 200 else "C")

    timeout_status, timeout_body, timeout_text = proxy_client.request(
        "GET", "/Observation?__conformance_case=timeout", ctx.read_headers())
    timeout_ok = (timeout_status >= 500
                  and _corrective_outcome(timeout_body, {"timeout", "transient"})
                  and _response_omits_hostile_values(timeout_body, timeout_text))
    timeout_grade = "A" if timeout_ok else ("F" if timeout_status == 200 else "C")

    _, audit_after, _ = proxy_client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    audit_codes = _new_audit_outcome_codes(audit_before, audit_after)
    audit_blob = json.dumps(audit_after or {})
    audit_safe = hostile_name not in audit_blob and hostile_url not in audit_blob
    audit_ok = (len(audit_codes) == 3
                and all(code == "8" for code in audit_codes)
                and audit_safe)
    audit_grade = "A" if audit_ok else ("F" if "0" in audit_codes else "C")

    grade = _error_fidelity_grade([
        rejection_grade, auth_grade, server_grade, timeout_grade, audit_grade,
    ])
    return grade, [
        Check("proxy rejection preserves sanitized outcome", rejection_grade == "A",
              f"grade {rejection_grade}; status {rejected_status}"),
        Check("proxy auth failure is not not-found", auth_grade == "A",
              f"grade {auth_grade}; status {auth_status}"),
        Check("proxy server failure is truthful", server_grade == "A",
              f"grade {server_grade}; status {server_status}"),
        Check("proxy timeout is truthful", timeout_grade == "A",
              f"grade {timeout_grade}; status {timeout_status}"),
        Check("proxy failure audit is truthful", audit_grade == "A",
              f"grade {audit_grade}; observed {len(audit_codes)} outcomes"),
    ]


def _mcp_profile_grade(mcp_client) -> str:
    try:
        result = mcp_client.call_tool(
            "fhir_search", {
                "resource_type": _MCP_INVALID_RESOURCE,
                "patient": _MCP_HOSTILE_URL,
                "_count": 1,
            })
    except Exception:  # transport probe executed but did not return a tool result
        return "C"
    if not isinstance(result, dict):
        return "F"

    payloads = []
    structured = result.get("structuredContent")
    malformed_content = ("structuredContent" in result
                         and not isinstance(structured, dict))
    if isinstance(structured, dict):
        payloads.append(structured)
    content = result.get("content", [])
    malformed_content = malformed_content or not isinstance(content, list)
    if malformed_content:
        content = []
    for item in content:
        if (not isinstance(item, dict)
                or set(item) != {"type", "text"}
                or item.get("type") != "text"
                or not isinstance(item.get("text"), str)):
            malformed_content = True
            continue
        try:
            payload = json.loads(item["text"])
        except (TypeError, ValueError):
            malformed_content = True
            payload = {"text": item["text"]}
        if not isinstance(payload, dict):
            malformed_content = True
        payloads.append(payload)

    outcomes = [payload for payload in payloads if _is_operation_outcome(payload)]
    has_outcome = bool(outcomes)
    has_failure = has_outcome or any(
        isinstance(payload, dict)
        and ("error" in payload or "failed" in str(payload.get("text", "")).lower())
        for payload in payloads
    )
    if (result.get("isError") is True
            and has_outcome
            and not malformed_content
            and len(payloads) == len(outcomes)
            and _mcp_result_omits_hostile_values(result)
            and all(_safe_corrective_mcp_outcome(outcome) for outcome in outcomes)):
        return "A"
    if has_failure or result.get("isError") is True:
        return "C"
    return "F"


def _safe_corrective_mcp_outcome(outcome) -> bool:
    """Accept only the narrow, synthesized outcome contract for this probe."""
    issues = outcome.get("issue", []) if isinstance(outcome, dict) else []
    if not issues:
        return False
    saw_expected_category = False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        if set(issue) - {"severity", "code", "details"}:
            return False
        if issue.get("severity") not in {"fatal", "error"}:
            return False
        code = issue.get("code")
        if (not isinstance(code, str)
                or code != code.lower()
                or not code.replace("-", "").isalnum()):
            return False
        saw_expected_category = saw_expected_category or code == "not-supported"
        details = issue.get("details")
        if not isinstance(details, dict) or set(details) != {"text"}:
            return False
        text = details["text"]
        if not isinstance(text, str) or not text.strip():
            return False
    return saw_expected_category and _mcp_result_omits_hostile_values(outcome)


def _mcp_result_omits_hostile_values(value) -> bool:
    blob = json.dumps(value).lower()
    unsafe_tokens = (
        _MCP_INVALID_RESOURCE.lower(),
        "quintavious",
        "zzyzxbarton",
        "http://",
        "https://",
    )
    return not any(token in blob for token in unsafe_tokens)


def probe_error_fidelity(client, ctx, mcp_client=None, proxy_client=None) -> ProbeResult:
    """Grade failure-path truthfulness without fixing the known gaps."""
    strict_headers = ctx.read_headers()
    strict_headers["Prefer"] = "handling=strict"
    subject_ref = f"Patient/conformance-error-fidelity-{uuid.uuid4().hex}"
    strict_query = urlencode({"patient": subject_ref, "datetime": "x"})
    modifier_query = urlencode({"patient": subject_ref, "code:exact": "x"})
    _, audit_before, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    strict_status, strict_body, _ = client.request(
        "GET", f"/Observation?{strict_query}", strict_headers)
    strict_grade = _rejection_grade(strict_status, strict_body)
    if strict_grade == "A" and not _outcome_names_parameter_and_supported_set(
            strict_body, "datetime"):
        strict_grade = "C"

    _, audit_after, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    audit_grade = _new_audit_outcome_grade(audit_before, audit_after)
    if strict_status == 200:
        audit_grade = "F"

    _, lenient_audit_before, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    lenient_status, lenient_body, _ = client.request(
        "GET", f"/Observation?{strict_query}", ctx.read_headers())
    _, lenient_audit_after, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    lenient_ok = (lenient_status == 200
                  and _has_outcome_warning(lenient_body, "datetime")
                  and _self_link_omits(lenient_body, "datetime")
                  and _self_link_includes(
                      lenient_body, "patient", subject_ref)
                  and _bundle_matches_subject(lenient_body, subject_ref))
    lenient_grade = "A" if lenient_ok else ("F" if lenient_status == 200 else "C")
    lenient_audit_grade = _new_audit_warning_grade(
        lenient_audit_before, lenient_audit_after, "datetime")

    modifier_status, modifier_body, _ = client.request(
        "GET", f"/Observation?{modifier_query}", ctx.read_headers())
    modifier_grade = _rejection_grade(modifier_status, modifier_body)
    modifier_strict_status, modifier_strict_body, _ = client.request(
        "GET", f"/Observation?{modifier_query}", strict_headers)
    modifier_strict_grade = _rejection_grade(
        modifier_strict_status, modifier_strict_body)
    modifier_grade = _error_fidelity_grade([
        modifier_grade, modifier_strict_grade,
    ])

    local_grade = _error_fidelity_grade([
        strict_grade, audit_grade, lenient_grade, lenient_audit_grade,
        modifier_grade,
    ])
    checks = [
        Check("strict unknown parameter is rejected", strict_grade == "A",
              f"grade {strict_grade}; status {strict_status}"),
        Check("strict rejection is audited as a failure", audit_grade == "A",
              f"grade {audit_grade}"),
        Check("lenient unknown parameter carries an outcome warning",
              lenient_grade == "A",
              f"grade {lenient_grade}; status {lenient_status}"),
        Check("lenient warning is audited truthfully",
              lenient_audit_grade == "A",
              f"grade {lenient_audit_grade}"),
        Check("unsupported modifier is rejected", modifier_grade == "A",
              f"grade {modifier_grade}; statuses "
              f"{modifier_status},{modifier_strict_status}"),
    ]
    local_check_names = [check.name for check in checks]
    profiles = {
        "local": _profile("run", grade=local_grade, checks=local_check_names),
        "mcp": _profile("not_run"),
        "proxy": _profile("not_run"),
    }
    executed_grades = [local_grade]
    coverage = "local-fhir-only"
    if mcp_client is not None:
        mcp_grade = _mcp_profile_grade(mcp_client)
        mcp_check_name = "MCP tool failure is corrective and flagged"
        profiles["mcp"] = _profile(
            "run", grade=mcp_grade, checks=[mcp_check_name])
        executed_grades.append(mcp_grade)
        coverage = "local+mcp"
        checks.append(Check(
            mcp_check_name, mcp_grade == "A",
            f"grade {mcp_grade}"))

    if proxy_client is not None:
        try:
            proxy_grade, proxy_checks = _proxy_profile(proxy_client, ctx)
        except Exception as exc:
            proxy_grade = "F"
            proxy_checks = [
                Check("proxy profile executed", False, type(exc).__name__)
            ]
        profiles["proxy"] = _profile(
            "run", grade=proxy_grade,
            checks=[check.name for check in proxy_checks])
        executed_grades.append(proxy_grade)
        checks.extend(proxy_checks)
        coverage = "full" if mcp_client is not None else "local+proxy"

    return ProbeResult(
        "error_fidelity", "Error Fidelity", checks,
        grade=_error_fidelity_grade(executed_grades),
        coverage=coverage,
        profiles=profiles,
    )


_PROBES = (
    probe_phi_redaction,
    probe_audit_trail,
    probe_step_up_enforcement,
    probe_human_in_the_loop,
    probe_tenant_isolation,
    probe_medical_disclaimer,
)


def run_conformance(client, ctx: ProbeContext, *, mcp_client=None,
                    proxy_client=None) -> ConformanceReport:
    results = []
    for probe in _PROBES:
        try:
            results.append(probe(client, ctx))
        except Exception as exc:  # a probe crash is a FAIL, never a harness crash
            key = probe.__name__.replace("probe_", "")
            results.append(ProbeResult(
                key, key.replace("_", " ").title(),
                [Check("probe executed", False, type(exc).__name__)]))
    try:
        results.append(probe_error_fidelity(
            client, ctx, mcp_client=mcp_client, proxy_client=proxy_client))
    except Exception as exc:  # a probe crash is a FAIL, never a harness crash
        results.append(ProbeResult(
            "error_fidelity", "Error Fidelity",
            [Check("probe executed", False, type(exc).__name__)],
            grade="F", coverage="local-fhir-only",
            profiles={
                "local": _profile("run", grade="F", checks=["probe executed"]),
                "mcp": _profile("not_run"),
                "proxy": _profile("not_run"),
            }))
    return ConformanceReport(results, base=getattr(client, "base", ""),
                             tenant=ctx.tenant)
