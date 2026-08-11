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
import textwrap
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

# Stated in every report output (#186): the grade must declare its own scope
# so a Grade A is never mistaken for a HIPAA assessment or third-party audit.
SCOPE_STATEMENT = (
    "This grade covers the HealthClaw guardrail layer only (self-test, "
    "synthetic data). It is NOT a HIPAA Security Rule assessment, a "
    "third-party audit, or a penetration test of your deployment. "
    "Infrastructure, BAAs, encryption, and access controls are the "
    "deployer's responsibility. A third party can run this same harness "
    "against any instance as one input to a real assessment — it does not "
    "substitute for one."
)

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

#: Guardrails this harness does NOT grade, published beside the seven it does.
#:
#: The grade is read as a product claim. A reader who sees "7/7, 35 checks"
#: and no list of exclusions will assume the seven are the whole surface, and
#: the first thing a skeptical adopter checks — can a stranger read patient
#: records — is not among them. Printing this list on /r6-dashboard costs a
#: paragraph; letting someone discover it themselves costs the grade's
#: credibility entirely.
#:
#: It lives HERE, next to PROPERTIES, rather than in the template, because
#: this is the file you edit when you add a property. A list of exclusions
#: kept anywhere else drifts into claiming we still do not measure something
#: we started measuring last month, and a stale exclusion is as dishonest as
#: a stale claim — it just fails in the safe-looking direction.
#:
#: tests/test_ungraded_is_published.py fails when a key appears in both
#: tuples, so removing a probe's entry is part of adding the probe.
#:
#: Source: #401 (read auth, and the list it enumerates). Read auth cannot be
#: graded by adding a probe — the harness has no way to tell "read auth is
#: off because this is a public demo tenant" from "off because someone
#: disabled it", so it needs a declared-posture design first. Until that
#: lands, the honest move is to say so.
UNGRADED = (
    ("read authentication",
     "whether an unauthenticated caller can read another tenant's records. "
     "READ_AUTH_ENABLED defaults off and is off in this harness's fixture, "
     "so a deployment serving records to strangers scores what a gated one "
     "scores (#401)"),
    ("the action rail's separation",
     "that propose, commit and confirm cannot be collapsed into one step"),
    ("step-up refusals beyond a forged token",
     "expired, cross-tenant, read-scope-on-write and replayed-nonce are "
     "each verified by unit tests, none by this harness"),
    ("redaction on $lastn and SubscriptionTopic/$list",
     "graded on reads and searches, not on these two operations"),
    ("rate limiting, the mint gate, and purge",
     "no probe exercises any of them, so this grade says nothing about "
     "whether a caller can flood the API, mint a token they should not "
     "have, or leave data behind after a delete"),
)

_ERROR_FIDELITY_GRADE_ORDER = {"F": 0, "C": 1, "A": 2}
_MCP_INVALID_RESOURCE = "WidgetQuintaviousZzyzxbarton"
_MCP_HOSTILE_URL = "https://db.internal.example/patient/secret"
_HOSTILE_VALUE_TOKENS = (
    _GIVEN.lower(), _FAMILY.lower(), "db.internal.example",
)
_URL_SCHEME_TOKENS = ("http://", "https://")
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
    """One assertion in the scorecard, with its two halves kept apart.

    `observed` is what the probe MEASURED — "status 200", "grade A",
    "id=None". It is true whichever way the check went, so it is safe beside
    a PASS.

    `on_failure` says what a failure would MEAN — "PHI leaked into audit",
    "no disclaimer on the response". It is a sentence about a world in which
    `passed` is False.

    Both used to share one `detail` field. The text renderer hid that field on
    a pass, which kept one output honest and left every other consumer to
    rediscover the rule; none did. Fifteen checks shipped emitting

        {"name": "no raw SSN in the audit trail",
         "passed": true, "detail": "PHI leaked into audit"}

    Two readers hit it independently and both concluded the deployment was
    leaking PHI. The second was the physician advisor, about thirty hours
    before a demo recording. The checks were right; the report was not.

    Splitting the field is what makes the report unable to say it. A consumer
    that prints everything it is handed now prints only true sentences,
    without having to know this rule.
    """

    name: str
    passed: bool
    #: Third positional on purpose. The measurement is both the common case
    #: and the safe one, so the thirty-odd sites that pass one positionally
    #: are correct untouched — and a failure explanation has to be named to
    #: get in, which is exactly where the author should have to think.
    observed: str = ""
    on_failure: str = ""

    @property
    def detail(self) -> str:
        """Both halves as one string, true of THIS run.

        Kept because `detail` is the key partner scripts already read, and
        derived rather than stored so it cannot drift from the two halves it
        summarises.
        """
        if self.passed or not self.on_failure:
            return self.observed
        if not self.observed:
            return self.on_failure
        return f"{self.observed}; {self.on_failure}"


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
            "scope": SCOPE_STATEMENT,
            "score": {"passed": p, "total": t},
            "properties": [
                {"key": r.key, "property": r.property, "passed": r.passed,
                 "grade": r.grade, "coverage": r.coverage,
                 "profiles": r.profiles, "note": r.note,
                 # `on_failure` is withheld on a pass rather than left to the
                 # consumer to suppress. That was the bug: the rule existed
                 # only inside render(), so every other reader of this JSON
                 # printed a failure sentence next to a PASS.
                 "checks": [{"name": c.name, "passed": c.passed,
                             "observed": c.observed,
                             "on_failure": "" if c.passed else c.on_failure,
                             "detail": c.detail}
                            for c in r.checks]}
                for r in self.results
            ],
        }

    def render(self) -> str:
        p, t = self.score
        lines = [f"HealthClaw Guardrail Conformance — {self.base or 'local'} "
                 f"[tenant={self.tenant}]",
                 f"  Grade: {self.grade}   ({p}/{t} properties)", ""]
        lines += textwrap.wrap(
            f"SCOPE: {SCOPE_STATEMENT}", width=78,
            initial_indent="  ", subsequent_indent="  ")
        lines.append("")
        for r in self.results:
            label = r.property
            if r.grade is not None:
                label += f" — {r.effective_grade} ({r.coverage})"
            lines.append(f"  [{'PASS' if r.passed else 'FAIL'}] {label}")
            # Notes were JSON-only, so the one caveat that limits what a PASS
            # means — human-in-the-loop grades the gate, not the attestation
            # behind it (#213/#214) — never reached the scorecard a partner
            # actually reads.
            if r.note:
                lines.append(f"        note: {r.note}")
            for profile_name, profile in r.profiles.items():
                status = profile.get("status", "unknown")
                profile_grade = profile.get("grade")
                grade_suffix = f" — {profile_grade}" if profile_grade else ""
                lines.append(
                    f"        {profile_name}: {status}{grade_suffix}")
                profile_checks = profile.get("checks", [])
                if profile_checks:
                    lines.append(
                        f"          checks: {', '.join(profile_checks)}")
            for c in r.checks:
                mark = "✓" if c.passed else "✗"
                # No `and not c.passed` here any more. `detail` is now true of
                # this run either way, so the text and the JSON can share one
                # rule instead of disagreeing about the same report. It also
                # earns the reader something: a passing check now shows the
                # status it actually got back, not just a tick.
                suffix = f" — {c.detail}" if c.detail else ""
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

    def __init__(self, mcp_url, session=None, tenant=None, step_up_token=None,
                 mcp_auth_token=None):
        import requests
        self._url = mcp_url.rstrip("/")
        self._s = session or requests.Session()
        self._session_id = None
        self._protocol_version = "2025-06-18"
        self._tenant = tenant
        self._step_up_token = step_up_token
        self._mcp_auth_token = mcp_auth_token

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
        if self._mcp_auth_token:
            headers["Authorization"] = f"Bearer {self._mcp_auth_token}"
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

# LOINC 2093-3 -> "Cholesterol (total)" in r6/terminology.py's static table,
# so the positive relabel check below needs no terminology server.
_EXPECTED_LABEL = "Cholesterol (total)"
_UPSTREAM_JUNK = "UPSTREAM-DISPLAY-MUST-NOT-SURVIVE"


def _synthetic_labelled_obs() -> dict:
    """An Observation whose upstream display is junk we expect to LOSE, and
    whose code we expect the server to label from its own table."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2093-3",
                             "display": _UPSTREAM_JUNK}]},
        "valueQuantity": {"value": 188, "unit": "mg/dL"},
    }


def _create_synthetic(client, ctx) -> tuple[Optional[str], object]:
    status, body, _ = client.request(
        "POST", "/Patient", ctx.write_headers(), _synthetic_patient())
    pid = body.get("id") if isinstance(body, dict) else None
    return pid, status


def _is_resource(body, resource_type: str, rid: str) -> bool:
    """The response IS the resource that was asked for."""
    return (isinstance(body, dict)
            and body.get("resourceType") == resource_type
            and body.get("id") == rid)


def _bundle_contains(bundle, resource_type: str, rid: str) -> bool:
    """A searchset actually carries the resource that was asked for."""
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return False
    return any(_is_resource((entry or {}).get("resource"), resource_type, rid)
               for entry in bundle.get("entry") or [])


def _tampered(token: str) -> str:
    """The issued token with one byte changed — i.e. a forged one.

    Missing-header 401 is producible by a route that only checks the header is
    PRESENT. This is the input that separates "validates the token" from
    "notices a string", and it needs no knowledge of the token format: whatever
    the deployment issued, this is not it.
    """
    if not token:
        return "conformance-not-a-real-step-up-token"
    i = len(token) // 2
    return token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1:]


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
        # Two-sided (#213). The five ABSENCE checks below are all satisfied by
        # a deployment that returns nothing at all — a redactor that deletes,
        # a store that lost the row, an endpoint stubbed out. Redaction's
        # contract is that the record survives it, so the record has to be
        # here for the absences to mean anything.
        Check("the redacted record is still returned",
              _is_resource(body, "Patient", pid),
              on_failure="the read did not return the Patient that was asked "
                         "for; every absence check below passes trivially on "
                         "an empty response"),
        Check("family name not returned in full", _FAMILY not in blob),
        Check("given name not returned in full", _GIVEN not in blob),
        Check("SSN-class identifier masked", _SSN not in blob),
        Check("phone number stripped", _PHONE not in blob),
        Check("street address stripped", _STREET not in blob),
    ]

    # Search, not only read-by-id. This probe issued a single GET by id, so the
    # grade it produces said nothing about the search path — and search is what
    # builds an agent's context (`careagents/healthclaw.py` search()). Removing
    # redaction from the search route left the whole suite green and the grade
    # at A, which made the badge honest about the wrong path.
    status, body, text = client.request(
        "GET", f"/Patient?_id={pid}", ctx.read_headers())
    sblob = text or json.dumps(body or {})
    r.checks += [
        Check("search succeeds", status == 200, f"status {status}"),
        # The same trap, and the one the badge was most exposed to: an empty
        # searchset is a 200 with no PHI in it. Search is what builds an
        # agent's context, so "search returns nothing" is a live outage that
        # scored A.
        Check("the search returns the resource it was asked for",
              _bundle_contains(body, "Patient", pid),
              on_failure="the searchset did not contain the Patient; an empty "
                         "Bundle passes every redaction check in this block"),
        Check("family name not returned in full (search)", _FAMILY not in sblob),
        Check("given name not returned in full (search)", _GIVEN not in sblob),
        Check("SSN-class identifier masked (search)", _SSN not in sblob),
        Check("phone number stripped (search)", _PHONE not in sblob),
        Check("street address stripped (search)", _STREET not in sblob),
    ]

    # A POSITIVE check, and the only one in this file. Every assertion above
    # is "a bad string is ABSENT", which a broken labeller passes HARDER — it
    # strictly shrinks the response. That is not hypothetical: the RxNorm
    # lookup returned None for its entire life (#376) and every guardrail
    # check here stayed green while a patient was told their medications were
    # unreadable. The grade said A about the wrong property.
    #
    # Redaction removes the upstream `display`; r6/terminology.py must put a
    # server-derived one back, keyed by code. LOINC 2093-3 is in the static
    # table, so this needs no network and no feature flag — it fails when the
    # relabel step stops running, which is the half of the contract nothing
    # else measures.
    status, body, _ = client.request(
        "POST", "/Observation", ctx.write_headers(), _synthetic_labelled_obs())
    oid = body.get("id") if isinstance(body, dict) else None
    if not oid:
        r.checks.append(Check("labelled observation created", False,
                              f"create returned {status}"))
    else:
        status, obody, otext = client.request(
            "GET", f"/Observation/{oid}", ctx.read_headers())
        oblob = otext or json.dumps(obody or {})
        r.checks += [
            Check("a recognised code is re-labelled after redaction",
                  status == 200 and _EXPECTED_LABEL in oblob,
                  f"status {status}; expected {_EXPECTED_LABEL!r}"),
            # The other half of the same contract: the label must be OURS.
            # Asserting only that a display exists would pass if redaction
            # stopped running and the feed's own text survived.
            Check("the upstream display did not survive",
                  _UPSTREAM_JUNK not in oblob),
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

    # Look for a READ specifically. Matching only `Patient/{pid}` in the blob
    # could not fail: the create above already emits an event whose
    # `entity.what.reference` is exactly that string, so the check passed with
    # read auditing deleted entirely. A check that the setup satisfies is not
    # a check.
    read_audited = False
    if isinstance(body, dict):
        for entry in body.get("entry") or []:
            res = entry.get("resource") or {}
            if res.get("resourceType") != "AuditEvent":
                continue
            action = res.get("action")
            refs = json.dumps(res.get("entity") or [])
            if action == "R" and pid and f"Patient/{pid}" in refs:
                read_audited = True
                break

    r.checks += [
        Check("AuditEvent endpoint readable", readable, f"status {st}"),
        Check("resource READ is recorded in the audit trail", read_audited,
              on_failure="no AuditEvent with action=R references the resource "
                         "that was read (a create event alone does not "
                         "demonstrate read auditing)"),
        Check("no raw SSN in the audit trail", _SSN not in blob,
              on_failure="PHI leaked into audit"),
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

    # Two-sided (#213). A deployment that refuses every write passes the check
    # above, and so does one whose gate is a `if header missing: 401` with no
    # validation behind it. The gate has to DISCRIMINATE: the token we were
    # issued works, a forged one does not.
    status, _, _ = client.request(
        "POST", "/Patient", {"X-Tenant-Id": ctx.tenant,
                             "X-Step-Up-Token": _tampered(ctx.step_up_token),
                             "Content-Type": "application/fhir+json"},
        _synthetic_patient())
    r.checks.append(Check("write with a forged step-up token is rejected (401)",
                          status == 401, f"status {status}",
                          on_failure="a token that is not the one this "
                                     "deployment issued authorized a write"))

    pid, status = _create_synthetic(client, ctx)
    r.checks.append(Check("write carrying a valid step-up token is accepted",
                          bool(pid) and status in (200, 201),
                          f"status {status}",
                          on_failure="the gate refuses authorized writes too, "
                                     "so its 401s prove nothing"))
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

    # Two-sided (#213). Blanket-428 on every clinical write passes the check
    # above while gating nothing, so the confirmed write has to go through.
    #
    # What this pair does NOT establish: that a human confirmed anything. The
    # probe supplies X-Human-Confirmed itself, and that header is a known gap
    # (#214) — the action rail's separate approval endpoint is the real
    # mechanism. This grades the gate's behavior, not the attestation behind
    # it, and the report says so in the note.
    status, body, _ = client.request("POST", "/Observation", ctx.write_headers(),
                                     _synthetic_observation())
    oid = body.get("id") if isinstance(body, dict) else None
    r.checks.append(Check("confirmed clinical write is accepted",
                          bool(oid) and status in (200, 201),
                          f"status {status}",
                          on_failure="the gate blocks confirmed writes too, "
                                     "so its 428s prove nothing"))
    r.note = ("the confirmation header is supplied by the probe: this grades "
              "the gate, not the human attestation behind it (#214)")
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

    # Two-sided (#213). A deployment that 404s everything isolates perfectly
    # and serves nobody; isolation means the OWNING tenant still gets it.
    status, body, _ = client.request(
        "GET", f"/Patient/{pid}", ctx.read_headers())
    r.checks.append(Check(
        "resource IS readable from its own tenant",
        status == 200 and _is_resource(body, "Patient", pid),
        f"status {status}",
        on_failure="a deployment that returns nothing to anyone passes the "
                   "isolation check above without isolating anything"))
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
                          on_failure="no disclaimer on the response"))
    # Two-sided (#213). The check above is a substring test, so an error page
    # that happens to say "disclaimer" satisfies it, and so does a response
    # that is nothing BUT a disclaimer. The disclaimer has to be attached to
    # the clinical data it disclaims.
    r.checks.append(Check(
        "the disclaimer accompanies the clinical record, not an error",
        status == 200 and _is_resource(body, "Observation", oid),
        f"status {status}",
        on_failure="the disclaimer was not attached to the Observation that "
                   "was read"))
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


def _outcome_has_unsafe_last_updated_suggestion(body) -> bool:
    """Reject `_lastUpdated` as a proposed substitute for clinical datetime.

    A supported-parameter list may name `_lastUpdated` factually. Any mention
    elsewhere in any issue is ambiguous or unsafe correction evidence.
    """
    if not _is_operation_outcome(body):
        return False

    def strip_exact_supported_set(match: re.Match) -> str:
        declared = {
            token.lower()
            for token in re.findall(
                r"[A-Za-z_][A-Za-z0-9_-]*", match.group(1))
        }
        if declared == _LOCAL_SUPPORTED_PARAMETER_EVIDENCE:
            return ""
        return match.group(0)

    for issue in body.get("issue", []):
        if not isinstance(issue, dict):
            continue
        details = issue.get("details")
        text = details.get("text", "") if isinstance(details, dict) else ""
        if (isinstance(text, str)
                and "_lastupdated" in _SUPPORTED_SET_RE.sub(
                    strip_exact_supported_set, text).lower()):
            return True
    return False


def _outcome_names_parameter_and_supported_set(body, parameter: str) -> bool:
    """A local rejection is corrective only if it identifies the bad key and
    tells the caller which search parameters are supported."""
    if not _is_operation_outcome(body):
        return False
    if _outcome_has_unsafe_last_updated_suggestion(body):
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
                and declared == _LOCAL_SUPPORTED_PARAMETER_EVIDENCE):
            return True
    return False


def _outcome_names_unsupported_modifier(body, modifier: str) -> bool:
    """Require a corrective rejection to identify the unsupported modifier."""
    if not _is_operation_outcome(body):
        return False
    token = re.escape(modifier.lower())
    rejection = re.compile(
        rf"(?:^|[.!?]\s*)(?:"
        rf"(?:unsupported|invalid|rejected)\s+modifier\s*:?\s*{token}"
        rf"|modifier\s*:?\s*{token}\s+(?:(?:is|was)\s+)?"
        rf"(?:unsupported|not\s+supported|rejected|invalid|not\s+implemented)"
        rf"|{token}\s+modifier\s+(?:(?:is|was)\s+)?"
        rf"(?:unsupported|not\s+supported|rejected|invalid|not\s+implemented)"
        rf")\s*(?=[.!?]|$)",
        re.IGNORECASE,
    )
    for issue in body.get("issue", []):
        if not isinstance(issue, dict):
            continue
        details = issue.get("details")
        text = details.get("text", "") if isinstance(details, dict) else ""
        if isinstance(text, str) and rejection.search(text):
            return True
    return False


def _modifier_rejection_grade(status, body, modifier: str) -> str:
    grade = _rejection_grade(status, body)
    if (grade == "A"
            and not _outcome_names_unsupported_modifier(body, modifier)):
        return "C"
    return grade


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
    ) and _values_omit_hostile_tokens(
        outcome, extra_tokens=_URL_SCHEME_TOKENS))


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
        if _outcome_has_unsafe_last_updated_suggestion(outcome):
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


def _new_audit_failures_grade(before, after, expected_count: int) -> str:
    """Grade a bounded batch of rejected requests from their new audit codes."""
    new_codes = _new_audit_outcome_codes(before, after)
    if len(new_codes) != expected_count:
        return "C"
    if all(code == "8" for code in new_codes):
        return "A"
    return "F" if "0" in new_codes else "C"


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
    return saw_expected and _values_omit_hostile_tokens(
        body, extra_tokens=_URL_SCHEME_TOKENS)


def _values_omit_hostile_tokens(*values, extra_tokens=()) -> bool:
    """Fail closed if any serialized evidence contains a synthetic leak token."""
    try:
        blob = "\n".join(
            value if isinstance(value, str) else json.dumps(value)
            for value in values
        ).lower()
    except (TypeError, ValueError):
        return False
    unsafe_tokens = (*_HOSTILE_VALUE_TOKENS, *extra_tokens)
    return not any(token in blob for token in unsafe_tokens)


def _response_omits_hostile_values(body, text) -> bool:
    return _values_omit_hostile_tokens(
        body, text, extra_tokens=_URL_SCHEME_TOKENS)


def _proxy_profile(proxy_client, ctx) -> tuple[str, list[Check]]:
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
    audit_safe = _values_omit_hostile_tokens(audit_after)
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
            and _values_omit_hostile_tokens(
                result, extra_tokens=_URL_SCHEME_TOKENS)
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
    return saw_expected_category and _values_omit_hostile_tokens(
        outcome, extra_tokens=_URL_SCHEME_TOKENS)


def _run_optional_error_fidelity_profiles(
        result: ProbeResult, ctx: ProbeContext, *, mcp_client=None,
        proxy_client=None) -> ProbeResult:
    """Run each configured optional profile even if another profile failed."""
    executed_grades = [result.grade or "F"]
    if mcp_client is not None:
        mcp_grade = _mcp_profile_grade(mcp_client)
        mcp_check_name = "MCP tool failure is corrective and flagged"
        result.profiles["mcp"] = _profile(
            "run", grade=mcp_grade, checks=[mcp_check_name])
        executed_grades.append(mcp_grade)
        result.coverage = "local+mcp"
        result.checks.append(Check(
            mcp_check_name, mcp_grade == "A", f"grade {mcp_grade}"))

    if proxy_client is not None:
        try:
            proxy_grade, proxy_checks = _proxy_profile(proxy_client, ctx)
        except Exception as exc:
            proxy_grade = "F"
            proxy_checks = [
                Check("proxy profile executed", False, type(exc).__name__)
            ]
        result.profiles["proxy"] = _profile(
            "run", grade=proxy_grade,
            checks=[check.name for check in proxy_checks])
        executed_grades.append(proxy_grade)
        result.checks.extend(proxy_checks)
        result.coverage = "full" if mcp_client is not None else "local+proxy"

    result.grade = _error_fidelity_grade(executed_grades)
    return result


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

    _, modifier_audit_before, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    modifier_status, modifier_body, _ = client.request(
        "GET", f"/Observation?{modifier_query}", ctx.read_headers())
    modifier_grade = _modifier_rejection_grade(
        modifier_status, modifier_body, "code:exact")
    modifier_strict_status, modifier_strict_body, _ = client.request(
        "GET", f"/Observation?{modifier_query}", strict_headers)
    modifier_strict_grade = _modifier_rejection_grade(
        modifier_strict_status, modifier_strict_body, "code:exact")
    lenient_headers = ctx.read_headers()
    lenient_headers["Prefer"] = "handling=lenient"
    modifier_lenient_status, modifier_lenient_body, _ = client.request(
        "GET", f"/Observation?{modifier_query}", lenient_headers)
    modifier_lenient_grade = _modifier_rejection_grade(
        modifier_lenient_status, modifier_lenient_body, "code:exact")
    modifier_grade = _error_fidelity_grade([
        modifier_grade, modifier_strict_grade, modifier_lenient_grade,
    ])
    _, modifier_audit_after, _ = client.request(
        "GET", "/AuditEvent?entity-type=Observation&_count=200", ctx.read_headers())
    modifier_audit_grade = _new_audit_failures_grade(
        modifier_audit_before, modifier_audit_after, 3)
    if 200 in (
            modifier_status, modifier_strict_status, modifier_lenient_status):
        modifier_audit_grade = "F"

    local_grade = _error_fidelity_grade([
        strict_grade, audit_grade, lenient_grade, lenient_audit_grade,
        modifier_grade, modifier_audit_grade,
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
              f"{modifier_status},{modifier_strict_status},"
              f"{modifier_lenient_status}"),
        Check("unsupported modifier rejections are audited as failures",
              modifier_audit_grade == "A",
              f"grade {modifier_audit_grade}"),
    ]
    local_check_names = [check.name for check in checks]
    profiles = {
        "local": _profile("run", grade=local_grade, checks=local_check_names),
        "mcp": _profile("not_run"),
        "proxy": _profile("not_run"),
    }
    result = ProbeResult(
        "error_fidelity", "Error Fidelity", checks,
        grade=local_grade,
        coverage="local-fhir-only",
        profiles=profiles,
    )
    return _run_optional_error_fidelity_profiles(
        result, ctx, mcp_client=mcp_client, proxy_client=proxy_client)


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
        failure = ProbeResult(
            "error_fidelity", "Error Fidelity",
            [Check("probe executed", False, type(exc).__name__)],
            grade="F", coverage="local-fhir-only",
            profiles={
                "local": _profile("run", grade="F", checks=["probe executed"]),
                "mcp": _profile("not_run"),
                "proxy": _profile("not_run"),
            })
        results.append(_run_optional_error_fidelity_profiles(
            failure, ctx, mcp_client=mcp_client, proxy_client=proxy_client))
    return ConformanceReport(results, base=getattr(client, "base", ""),
                             tenant=ctx.tenant)
