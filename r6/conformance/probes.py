"""Guardrail conformance probes.

Runs the six HealthClaw guardrail properties against a live endpoint (or the
Flask test client) and emits a scorecard. The point: the guardrail claims are
*verifiable*, not marketing — a partner can run this against any deployment and
prove PHI redaction, audit, step-up, human-in-the-loop, tenant isolation, and
medical disclaimers actually hold.

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
)


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

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


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
                 "note": r.note,
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
            lines.append(f"  [{'PASS' if r.passed else 'FAIL'}] {r.property}")
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
        return {"X-Tenant-Id": self.tenant}

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


# --- The six probes ------------------------------------------------------------

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


_PROBES = (
    probe_phi_redaction,
    probe_audit_trail,
    probe_step_up_enforcement,
    probe_human_in_the_loop,
    probe_tenant_isolation,
    probe_medical_disclaimer,
)


def run_conformance(client, ctx: ProbeContext) -> ConformanceReport:
    results = []
    for probe in _PROBES:
        try:
            results.append(probe(client, ctx))
        except Exception as exc:  # a probe crash is a FAIL, never a harness crash
            key = probe.__name__.replace("probe_", "")
            results.append(ProbeResult(
                key, key.replace("_", " ").title(),
                [Check("probe executed", False, type(exc).__name__)]))
    return ConformanceReport(results, base=getattr(client, "base", ""),
                             tenant=ctx.tenant)
