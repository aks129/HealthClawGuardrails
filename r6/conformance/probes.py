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
from dataclasses import dataclass, field
from typing import Optional

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
                 "grade": r.effective_grade, "coverage": r.coverage,
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


class LiveMCPProbeClient:
    """Small Streamable HTTP MCP client used only by optional conformance probes."""

    def __init__(self, mcp_url, session=None):
        import requests
        self._url = mcp_url.rstrip("/")
        self._s = session or requests.Session()
        self._session_id = None
        self._protocol_version = "2025-06-18"

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
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
        body = response.json()
        if not isinstance(body, dict) or "error" in body:
            raise RuntimeError("MCP initialize failed")
        self._session_id = response.headers.get("Mcp-Session-Id")
        if not self._session_id:
            raise RuntimeError("MCP initialize returned no session id")
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
        body = response.json()
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
    if status == expected_status and _is_operation_outcome(body):
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
        if parameter in text and "supported" in text.lower():
            return True
    return False


def _has_outcome_warning(bundle) -> bool:
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return False
    for entry in bundle.get("entry", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("search", {}).get("mode") != "outcome":
            continue
        outcome = entry.get("resource")
        if not _is_operation_outcome(outcome):
            continue
        issues = outcome.get("issue", [])
        severities = {i.get("severity") for i in issues if isinstance(i, dict)}
        if issues and not severities.intersection({"fatal", "error"}):
            return True
    return False


def _self_link_omits(bundle, parameter: str) -> bool:
    if not isinstance(bundle, dict):
        return False
    links = bundle.get("link", [])
    self_urls = [link.get("url", "") for link in links
                 if isinstance(link, dict) and link.get("relation") == "self"]
    return bool(self_urls) and all(f"{parameter}=" not in url for url in self_urls)


def _latest_audit_outcome_code(bundle):
    if not isinstance(bundle, dict):
        return None
    entries = bundle.get("entry", [])
    if not entries or not isinstance(entries[0], dict):
        return None
    resource = entries[0].get("resource", {})
    return resource.get("outcome", {}).get("code", {}).get("code")


def _audit_outcome_codes(bundle) -> list[str]:
    if not isinstance(bundle, dict):
        return []
    codes = []
    for entry in bundle.get("entry", []):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource", {})
        code = resource.get("outcome", {}).get("code", {}).get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


def _proxy_profile(proxy_client, ctx) -> tuple[str, list[Check]]:
    hostile_name = "Quintavious Zzyzxbarton"
    hostile_url = "https://db.internal.example/patient/secret"

    rejected_status, rejected_body, rejected_text = proxy_client.request(
        "GET", "/Observation?__conformance_case=rejection", ctx.read_headers())
    rejected_blob = rejected_text or json.dumps(rejected_body or {})
    rejection_safe = hostile_name not in rejected_blob and hostile_url not in rejected_blob
    rejection_ok = (rejected_status == 400
                    and _is_operation_outcome(rejected_body)
                    and rejection_safe)
    rejection_grade = ("A" if rejection_ok else
                       ("F" if rejected_status == 200 or not rejection_safe else "C"))

    auth_status, auth_body, _ = proxy_client.request(
        "GET", "/Patient/conformance-auth", ctx.read_headers())
    auth_issue = (auth_body.get("issue", [{}])[0]
                  if _is_operation_outcome(auth_body) else {})
    auth_ok = (auth_status in (401, 403, 502)
               and _is_operation_outcome(auth_body)
               and auth_issue.get("code") == "security")
    auth_grade = "A" if auth_ok else ("F" if auth_status in (200, 404) else "C")

    server_status, server_body, _ = proxy_client.request(
        "GET", "/Observation?__conformance_case=server", ctx.read_headers())
    server_ok = server_status >= 500 and _is_operation_outcome(server_body)
    server_grade = "A" if server_ok else ("F" if server_status == 200 else "C")

    timeout_status, timeout_body, _ = proxy_client.request(
        "GET", "/Observation?__conformance_case=timeout", ctx.read_headers())
    timeout_ok = timeout_status >= 500 and _is_operation_outcome(timeout_body)
    timeout_grade = "A" if timeout_ok else ("F" if timeout_status == 200 else "C")

    _, audit_body, _ = proxy_client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=3", ctx.read_headers())
    audit_codes = _audit_outcome_codes(audit_body)
    audit_blob = json.dumps(audit_body or {})
    audit_safe = hostile_name not in audit_blob and hostile_url not in audit_blob
    audit_ok = (len(audit_codes) >= 3
                and all(code == "8" for code in audit_codes[:3])
                and audit_safe)
    audit_grade = "A" if audit_ok else ("F" if "0" in audit_codes[:2] else "C")

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
              f"grade {audit_grade}; outcomes {','.join(audit_codes) or 'missing'}"),
    ]


def _mcp_profile_grade(mcp_client) -> str:
    try:
        result = mcp_client.call_tool(
            "fhir_search", {"resource_type": "Widget", "_count": 1})
    except Exception:  # transport probe executed but did not return a tool result
        return "C"
    if not isinstance(result, dict):
        return "F"

    payloads = []
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        payloads.append(structured)
    for item in result.get("content", []):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            payloads.append(json.loads(item["text"]))
        except (TypeError, ValueError):
            payloads.append({"text": item["text"]})

    has_outcome = any(_is_operation_outcome(payload) for payload in payloads)
    has_failure = has_outcome or any(
        isinstance(payload, dict)
        and ("error" in payload or "failed" in str(payload.get("text", "")).lower())
        for payload in payloads
    )
    if result.get("isError") is True and has_outcome:
        return "A"
    if has_failure or result.get("isError") is True:
        return "C"
    return "F"


def probe_error_fidelity(client, ctx, mcp_client=None, proxy_client=None) -> ProbeResult:
    """Grade failure-path truthfulness without fixing the known gaps."""
    strict_headers = ctx.read_headers()
    strict_headers["Prefer"] = "handling=strict"
    strict_status, strict_body, _ = client.request(
        "GET", "/Observation?datetime=x", strict_headers)
    strict_grade = _rejection_grade(strict_status, strict_body)
    if strict_grade == "A" and not _outcome_names_parameter_and_supported_set(
            strict_body, "datetime"):
        strict_grade = "C"

    _, audit_body, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=1", ctx.read_headers())
    audit_code = _latest_audit_outcome_code(audit_body)
    audit_grade = "A" if audit_code == "8" else ("F" if audit_code == "0" else "C")

    lenient_status, lenient_body, _ = client.request(
        "GET", "/Observation?datetime=x", ctx.read_headers())
    lenient_ok = (lenient_status == 200
                  and _has_outcome_warning(lenient_body)
                  and _self_link_omits(lenient_body, "datetime"))
    lenient_grade = "A" if lenient_ok else ("F" if lenient_status == 200 else "C")

    modifier_status, modifier_body, _ = client.request(
        "GET", "/Observation?code:exact=x", ctx.read_headers())
    modifier_grade = _rejection_grade(modifier_status, modifier_body)
    modifier_strict_status, modifier_strict_body, _ = client.request(
        "GET", "/Observation?code:exact=x", strict_headers)
    modifier_strict_grade = _rejection_grade(
        modifier_strict_status, modifier_strict_body)
    modifier_grade = _error_fidelity_grade([
        modifier_grade, modifier_strict_grade,
    ])

    local_grade = _error_fidelity_grade([
        strict_grade, audit_grade, lenient_grade, modifier_grade,
    ])
    checks = [
        Check("strict unknown parameter is rejected", strict_grade == "A",
              f"grade {strict_grade}; status {strict_status}"),
        Check("strict rejection is audited as a failure", audit_grade == "A",
              f"grade {audit_grade}; outcome {audit_code or 'missing'}"),
        Check("lenient unknown parameter carries an outcome warning",
              lenient_grade == "A",
              f"grade {lenient_grade}; status {lenient_status}"),
        Check("unsupported modifier is rejected", modifier_grade == "A",
              f"grade {modifier_grade}; statuses "
              f"{modifier_status},{modifier_strict_status}"),
    ]
    profiles = {
        "local": {"status": "run", "grade": local_grade},
        "mcp": {"status": "not_run"},
        "proxy": {"status": "not_run"},
    }
    executed_grades = [local_grade]
    coverage = "local-fhir-only"
    if mcp_client is not None:
        mcp_grade = _mcp_profile_grade(mcp_client)
        profiles["mcp"] = {"status": "run", "grade": mcp_grade}
        executed_grades.append(mcp_grade)
        coverage = "local+mcp"
        checks.append(Check(
            "MCP tool failure is corrective and flagged", mcp_grade == "A",
            f"grade {mcp_grade}"))

    if proxy_client is not None:
        proxy_grade, proxy_checks = _proxy_profile(proxy_client, ctx)
        profiles["proxy"] = {"status": "run", "grade": proxy_grade}
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
                "local": {"status": "run", "grade": "F"},
                "mcp": ({"status": "run", "grade": "F"}
                        if mcp_client is not None else {"status": "not_run"}),
                "proxy": ({"status": "run", "grade": "F"}
                          if proxy_client is not None else {"status": "not_run"}),
            }))
    return ConformanceReport(results, base=getattr(client, "base", ""),
                             tenant=ctx.tenant)
