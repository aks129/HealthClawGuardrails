"""The guardrail conformance harness must grade our OWN deployment honestly.

This is both the unit test for r6/conformance and a standing CI baseline.  The
same harness runs against a live partner deployment via
scripts/guardrail_conformance.py.
"""

from r6.conformance import (
    FlaskProbeClient, ProbeContext, run_conformance, PROPERTIES,
)


def _ctx(app, tenant_id, step_up_token):
    return ProbeContext(tenant=tenant_id, step_up_token=step_up_token)


def test_our_deployment_records_the_known_error_fidelity_baseline(
        client, tenant_id, step_up_token):
    report = run_conformance(FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))

    assert {r.key for r in report.results} == set(PROPERTIES)
    assert report.score == (6, 7)
    assert report.grade == "B"
    assert report.passed is False
    assert [r.key for r in report.results if not r.passed] == ["error_fidelity"]


def test_report_is_json_serializable(client, tenant_id, step_up_token):
    report = run_conformance(FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))
    d = report.to_dict()
    assert d["passed"] is False
    assert d["grade"] == "B"
    assert len(d["properties"]) == len(PROPERTIES)
    for prop in d["properties"]:
        assert "checks" in prop and prop["checks"]


def test_grade_scales_with_failures():
    # Grading is a pure function of pass fraction — verify the boundaries.
    from r6.conformance import _grade
    assert _grade(6, 6) == "A"
    assert _grade(5, 6) == "B"
    assert _grade(4, 6) == "C"
    assert _grade(3, 6) == "D"
    assert _grade(2, 6) == "F"
    assert _grade(0, 6) == "F"


def test_report_exposes_property_grade_and_profile_coverage():
    """A graded property remains a normal report property, but it passes only
    at A and tells callers exactly which probe profiles were exercised."""
    from r6.conformance import Check, ConformanceReport, ProbeResult

    result = ProbeResult(
        "error_fidelity",
        "Error Fidelity",
        [Check("tool error is corrective", False, "status-only error")],
        grade="C",
        coverage="local+mcp",
        profiles={
            "local": {"status": "run", "grade": "A"},
            "mcp": {"status": "run", "grade": "C"},
            "proxy": {"status": "not_run"},
        },
    )
    report = ConformanceReport([result], base="local", tenant="t1")

    body = report.to_dict()
    prop = body["properties"][0]
    assert prop["grade"] == "C"
    assert prop["coverage"] == "local+mcp"
    assert prop["profiles"]["proxy"] == {"status": "not_run"}
    assert prop["passed"] is False
    assert report.passed is False
    assert "Error Fidelity — C (local+mcp)" in report.render()


def test_report_serialization_preserves_legacy_implicit_grade():
    from r6.conformance import Check, ConformanceReport, ProbeResult

    report = ConformanceReport([
        ProbeResult("legacy", "Legacy Property", [Check("passes", True)])
    ])

    prop = report.to_dict()["properties"][0]
    assert prop["grade"] is None
    assert "Legacy Property — A (full)" not in report.render()


def test_error_fidelity_grade_uses_the_worst_executed_profile():
    from r6.conformance import _error_fidelity_grade
    from r6.conformance.probes import _rejection_grade

    assert _error_fidelity_grade(["A"]) == "A"
    assert _error_fidelity_grade(["A", "C"]) == "C"
    assert _error_fidelity_grade(["C", "F", "A"]) == "F"
    assert _error_fidelity_grade([]) == "F"
    assert _rejection_grade(200, {}) == "F"
    assert _rejection_grade(502, {"resourceType": "OperationOutcome"}) == "C"
    assert _rejection_grade(400, {"resourceType": "OperationOutcome"}) == "C"


def test_local_contract_rejects_false_a_evidence():
    from r6.conformance.probes import (
        _bundle_matches_subject,
        _corrective_outcome,
        _has_outcome_warning,
        _outcome_names_parameter_and_supported_set,
        _self_link_omits,
    )

    unsupported_only = {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": "error",
            "code": "invalid",
            "details": {"text": "datetime is unsupported."},
        }],
    }
    assert not _outcome_names_parameter_and_supported_set(
        unsupported_only, "datetime")
    for negated in (
        "Unsupported parameters: datetime, patient, code.",
        "The unsupported parameter datetime conflicts with patient and code.",
        "datetime is not a supported parameter; patient and code are examples.",
        "datetime rejected. Supported parameters: patiently, barcode.",
        "datetime rejected. Supported parameters: status-code, patientish.",
        "Supported parameters: patient, datetime, code.",
        "datetime is not invalid. Supported parameters: patient, code, status.",
        "datetime is not unsupported. Supported parameters: patient, code, status.",
        "datetime was not rejected. Supported parameters: patient, code, status.",
        "There are no unknown problems with datetime. Supported parameters: "
        "patient, code, status.",
    ):
        body = {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "error",
                "code": "invalid",
                "details": {"text": negated},
            }],
        }
        assert not _outcome_names_parameter_and_supported_set(body, "datetime")

    unrelated_warning = {
        "resourceType": "Bundle",
        "entry": [{
            "search": {"mode": "outcome"},
            "resource": {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "banana", "code": "invalid"}],
            },
        }],
    }
    assert not _has_outcome_warning(unrelated_warning, "datetime")
    bad_category_warning = {
        "resourceType": "Bundle",
        "entry": [{
            "search": {"mode": "outcome"},
            "resource": {
                "resourceType": "OperationOutcome",
                "issue": [{
                    "severity": "warning",
                    "code": "banana",
                    "details": {"text": (
                        "datetime ignored. Supported parameters: "
                        "patient, code, status."
                    )},
                }],
            },
        }],
    }
    assert not _has_outcome_warning(bad_category_warning, "datetime")

    safe_warning = {
        "search": {"mode": "outcome"},
        "resource": {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "warning",
                "code": "not-supported",
                "details": {"text": (
                    "datetime ignored. Supported parameters: patient, code."
                )},
            }],
        },
    }
    hostile_extra_warning = {
        "search": {"mode": "outcome"},
        "resource": {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "warning",
                "code": "not-supported",
                "details": {"text": "Patient Quintavious Zzyzxbarton"},
            }],
        },
    }
    assert not _has_outcome_warning({
        "resourceType": "Bundle",
        "entry": [safe_warning, hostile_extra_warning],
    }, "datetime")

    encoded_ignored_key = {
        "resourceType": "Bundle",
        "link": [{
            "relation": "self",
            "url": "https://example.test/Observation?%64atetime=x",
        }],
    }
    assert not _self_link_omits(encoded_ignored_key, "datetime")

    foreign_match = {
        "resourceType": "Bundle",
        "entry": [{
            "search": {"mode": "match"},
            "resource": {
                "resourceType": "Observation",
                "subject": {"reference": "Patient/real-patient"},
            },
        }],
    }
    assert not _bundle_matches_subject(
        foreign_match, "Patient/conformance-synthetic")

    corrective_plus_hostile = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "invalid",
                "details": {"text": "The request was invalid."},
            },
            {
                "severity": "error",
                "code": "invalid",
                "diagnostics": "Patient Alice at https://db.internal/secret",
            },
        ],
    }
    assert not _corrective_outcome(corrective_plus_hostile, {"invalid"})


def test_lenient_audit_requires_ignored_parameter_evidence():
    from r6.conformance.probes import _new_audit_warning_grade

    def bundle(event_id, detail=None):
        outcome = {"code": {"code": "0"}}
        if detail is not None:
            outcome["detail"] = [{"text": detail}]
        return {
            "resourceType": "Bundle",
            "entry": [{"resource": {
                "resourceType": "AuditEvent",
                "id": event_id,
                "outcome": outcome,
            }}],
        }

    before = {"resourceType": "Bundle", "entry": []}
    assert _new_audit_warning_grade(before, bundle("missing"), "datetime") == "C"
    assert _new_audit_warning_grade(
        before, bundle("honest", "ignored parameter: datetime"), "datetime") == "A"
    assert _new_audit_warning_grade(
        before, bundle("lie", "applied filter: datetime"), "datetime") == "C"
    assert _new_audit_warning_grade(
        before,
        bundle("contradiction", "ignored parameter: datetime; then applied filter datetime"),
        "datetime",
    ) == "C"
    assert _new_audit_warning_grade(
        before, bundle("echo", "ignored parameter: datetime=x"), "datetime") == "F"


def test_local_error_fidelity_records_the_known_f_baseline(client, tenant_id,
                                                            step_up_token):
    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.grade == "F"
    assert result.coverage == "local-fhir-only"
    assert result.profiles == {
        "local": {
            "status": "run",
            "grade": "F",
            "checks": [
                "strict unknown parameter is rejected",
                "strict rejection is audited as a failure",
                "lenient unknown parameter carries an outcome warning",
                "lenient warning is audited truthfully",
                "unsupported modifier is rejected",
            ],
        },
        "mcp": {"status": "not_run", "checks": []},
        "proxy": {"status": "not_run", "checks": []},
    }
    assert {check.name for check in result.checks} == {
        "strict unknown parameter is rejected",
        "strict rejection is audited as a failure",
        "lenient unknown parameter carries an outcome warning",
        "lenient warning is audited truthfully",
        "unsupported modifier is rejected",
    }
    assert report.score == (6, 7)
    assert report.grade == "B"
    assert report.passed is False


def test_optional_mcp_profile_records_status_only_errors_as_c(client, tenant_id,
                                                              step_up_token):
    class StatusOnlyMCPClient:
        def call_tool(self, name, arguments):
            assert name == "fhir_search"
            assert arguments["resource_type"] == "WidgetQuintaviousZzyzxbarton"
            return {
                "content": [{
                    "type": "text",
                    "text": '{"error":"Search failed with status 400"}',
                }],
            }

    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token),
        mcp_client=StatusOnlyMCPClient())

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.grade == "F"  # local F is weaker than MCP C
    assert result.coverage == "local+mcp"
    assert result.profiles["mcp"] == {
        "status": "run", "grade": "C",
        "checks": ["MCP tool failure is corrective and flagged"],
    }
    assert any(c.name == "MCP tool failure is corrective and flagged"
               for c in result.checks)


def test_optional_mcp_profile_accepts_flagged_operation_outcome(
        client, tenant_id, step_up_token):
    class CorrectiveMCPClient:
        def call_tool(self, name, arguments):
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "resourceType": "OperationOutcome",
                        "issue": [{
                            "severity": "error",
                            "code": "not-supported",
                            "details": {"text": "Resource type is not supported."},
                        }],
                    }),
                }],
            }

    import json

    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token),
        mcp_client=CorrectiveMCPClient())

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.profiles["mcp"] == {
        "status": "run", "grade": "A",
        "checks": ["MCP tool failure is corrective and flagged"],
    }


def test_optional_mcp_profile_rejects_unsanitized_operation_outcome(
        client, tenant_id, step_up_token):
    class UnsafeMCPClient:
        def call_tool(self, name, arguments):
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "resourceType": "OperationOutcome",
                        "issue": [{
                            "severity": "error",
                            "code": "not-supported",
                            "details": {"text": "Patient Quintavious Zzyzxbarton"},
                            "diagnostics": "https://db.internal.example/secret",
                        }],
                    }),
                }],
            }

    import json

    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token),
        mcp_client=UnsafeMCPClient())

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.profiles["mcp"]["grade"] != "A"


def test_malformed_mcp_result_remains_an_executed_profile(
        client, tenant_id, step_up_token):
    class MalformedMCPClient:
        def call_tool(self, name, arguments):
            return {"isError": True, "content": None}

    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token),
        mcp_client=MalformedMCPClient())

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.profiles["mcp"]["status"] == "run"
    assert result.profiles["mcp"]["grade"] == "C"
    assert result.coverage == "local+mcp"


def test_mcp_safe_outcome_plus_malformed_content_is_not_a():
    import json

    from r6.conformance.probes import _mcp_profile_grade

    class MixedMCPClient:
        def call_tool(self, name, arguments):
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [{
                    "severity": "error",
                    "code": "not-supported",
                    "details": {"text": "Resource type is not supported."},
                }],
            }
            return {
                "isError": True,
                "content": [
                    {"type": "text", "text": json.dumps(outcome)},
                    {},
                ],
            }

    assert _mcp_profile_grade(MixedMCPClient()) == "C"


def test_mcp_safe_outcome_in_non_text_content_block_is_not_a():
    import json

    from r6.conformance.probes import _mcp_profile_grade

    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": "error",
            "code": "not-supported",
            "details": {"text": "Resource type is not supported."},
        }],
    }

    class MalformedBlockMCPClient:
        def __init__(self, block):
            self.block = block

        def call_tool(self, name, arguments):
            return {"isError": True, "content": [self.block]}

    encoded = json.dumps(outcome)
    assert _mcp_profile_grade(MalformedBlockMCPClient(
        {"type": "image", "text": encoded})) == "C"
    assert _mcp_profile_grade(MalformedBlockMCPClient(
        {"text": encoded})) == "C"


def test_audit_correlation_uses_new_event_ids_not_only_latest_entry():
    from r6.conformance.probes import (
        _new_audit_outcome_codes,
        _new_audit_outcome_grade,
    )

    def bundle(*events):
        return {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {
                    "resourceType": "AuditEvent",
                    "id": event_id,
                    "outcome": {"code": {"code": outcome}},
                }}
                for event_id, outcome in events
            ],
        }

    before = bundle(("old", "0"))
    after = bundle(("probe", "8"), ("old", "0"))
    ambiguous = bundle(("concurrent", "0"), ("probe", "8"), ("old", "0"))

    assert _new_audit_outcome_grade(before, after) == "A"
    assert _new_audit_outcome_grade(before, ambiguous) == "C"
    assert _new_audit_outcome_codes(before, ambiguous) == ["0", "8"]


def test_proxy_a_requires_a_corrective_operation_outcome():
    from r6.conformance.probes import _corrective_outcome

    assert not _corrective_outcome(
        {"resourceType": "OperationOutcome"}, {"invalid"})
    assert not _corrective_outcome({
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "security"}],
    }, {"invalid"})
    assert _corrective_outcome({
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": "error",
            "code": "invalid",
            "details": {"text": "The request was invalid."},
        }],
    }, {"invalid"})


def test_proxy_profile_does_not_echo_malformed_audit_codes():
    import json

    from r6.conformance.probes import _proxy_profile

    hostile = "https://db.internal.example/audit-secret"

    def outcome(code):
        return {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "error",
                "code": code,
                "details": {"text": "Synthetic corrective message."},
            }],
        }

    def audit(event_id=None, code=None):
        entries = []
        if event_id is not None:
            entries.append({"resource": {
                "resourceType": "AuditEvent",
                "id": event_id,
                "outcome": {"code": {"code": code}},
            }})
        return {"resourceType": "Bundle", "entry": entries}

    responses = iter([
        (200, audit(), ""),
        (400, outcome("invalid"), ""),
        (502, outcome("security"), ""),
        (502, outcome("transient"), ""),
        (502, outcome("transient"), ""),
        (200, audit("hostile", hostile), ""),
    ])

    class Client:
        def request(self, *args, **kwargs):
            return next(responses)

    _, checks = _proxy_profile(Client(), ProbeContext("tenant", "token"))
    assert hostile not in json.dumps([check.detail for check in checks])


def test_error_fidelity_crash_does_not_claim_optional_profiles_ran():
    class BrokenLocalClient:
        base = "broken"

        def request(self, *args, **kwargs):
            raise RuntimeError("synthetic failure")

    report = run_conformance(
        BrokenLocalClient(), ProbeContext("tenant", "token"),
        mcp_client=object(), proxy_client=object())

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.coverage == "local-fhir-only"
    assert result.profiles["mcp"] == {"status": "not_run", "checks": []}
    assert result.profiles["proxy"] == {"status": "not_run", "checks": []}


def test_live_mcp_probe_client_initializes_then_calls_tool():
    from r6.conformance import LiveMCPProbeClient

    class Response:
        def __init__(self, body, headers=None):
            self._body = body
            self.headers = headers or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Session:
        def __init__(self):
            self.calls = []
            self.responses = [
                Response({"id": 1, "result": {"protocolVersion": "2025-06-18"}},
                         {"Mcp-Session-Id": "session-1"}),
                Response({}),
                Response({"id": 2, "result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "isError": True,
                }}),
            ]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    session = Session()
    client = LiveMCPProbeClient(
        "https://mcp.example.test/mcp",
        session=session,
        tenant="tenant-a",
        step_up_token="synthetic-token",
    )
    result = client.call_tool("fhir_search", {"resource_type": "Widget"})

    assert result["isError"] is True
    assert len(session.calls) == 3
    assert session.calls[1][1]["json"]["method"] == "notifications/initialized"
    assert session.calls[2][1]["headers"]["Mcp-Session-Id"] == "session-1"
    assert session.calls[2][1]["headers"]["X-Tenant-Id"] == "tenant-a"
    assert session.calls[2][1]["headers"]["X-Step-Up-Token"] == "synthetic-token"
    assert session.calls[2][1]["json"]["method"] == "tools/call"


def test_live_mcp_probe_client_accepts_stateless_json_transport():
    from r6.conformance import LiveMCPProbeClient

    class Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Session:
        def __init__(self):
            self.responses = [
                Response({"id": 1, "result": {"protocolVersion": "2025-06-18"}}),
                Response({}),
                Response({"id": 2, "result": {"isError": True, "content": []}}),
            ]

        def post(self, url, **kwargs):
            return self.responses.pop(0)

    result = LiveMCPProbeClient(
        "https://mcp.example.test/mcp", session=Session()).call_tool(
            "fhir_search", {"resource_type": "Widget"})
    assert result["isError"] is True


def test_live_mcp_probe_client_decodes_sse_responses():
    from r6.conformance.probes import _mcp_response_json

    class Response:
        headers = {"Content-Type": "text/event-stream"}
        text = (
            "event: message\n"
            "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n"
            "event: message\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":1,"
            "\"result\":{\"content\":[]}}\n\n"
        )

    assert _mcp_response_json(
        Response(), expected_id=1)["result"] == {"content": []}


def test_local_error_probes_always_bound_searches_to_a_synthetic_subject():
    from r6.conformance.probes import probe_error_fidelity

    class RecordingClient:
        base = "recording"

        def __init__(self):
            self.paths = []

        def request(self, method, path, headers=None, json_body=None):
            self.paths.append(path)
            if path.startswith("/AuditEvent"):
                return 200, {"resourceType": "Bundle", "entry": []}, ""
            return 200, {
                "resourceType": "Bundle",
                "link": [{"relation": "self", "url": path}],
                "entry": [],
            }, ""

    client = RecordingClient()
    probe_error_fidelity(client, ProbeContext("tenant", "token"))

    observation_paths = [
        path for path in client.paths if path.startswith("/Observation?")]
    assert observation_paths
    assert all("patient=" in path for path in observation_paths)


def test_scripted_local_contract_can_reach_a_without_reading_real_data():
    from urllib.parse import parse_qs, urlencode, urlsplit

    from r6.conformance.probes import probe_error_fidelity

    corrective_text = (
        "datetime is not implemented. Supported parameters: patient, code, status."
    )

    def audit_event(event_id, code, detail=None):
        outcome = {"code": {"code": code}}
        if detail is not None:
            outcome["detail"] = [{"text": detail}]
        return {
            "resourceType": "AuditEvent",
            "id": event_id,
            "outcome": outcome,
        }

    def audit_bundle(*events):
        return {
            "resourceType": "Bundle",
            "entry": [{"resource": event} for event in events],
        }

    class LocalAClient:
        base = "scripted-local-a"

        def __init__(self):
            self.audit_call = 0
            self.observation_call = 0

        def request(self, method, path, headers=None, json_body=None):
            if path.startswith("/AuditEvent"):
                self.audit_call += 1
                old = audit_event("old", "0")
                strict = audit_event("strict", "8")
                if self.audit_call == 1:
                    body = audit_bundle(old)
                elif self.audit_call in (2, 3):
                    body = audit_bundle(strict, old)
                else:
                    body = audit_bundle(
                        audit_event(
                            "lenient", "0", "ignored parameter: datetime"),
                        strict,
                        old,
                    )
                return 200, body, ""

            self.observation_call += 1
            query = parse_qs(urlsplit(path).query)
            subject = query["patient"][0]
            if self.observation_call == 1:
                body = {
                    "resourceType": "OperationOutcome",
                    "issue": [{
                        "severity": "error",
                        "code": "not-supported",
                        "details": {"text": corrective_text},
                    }],
                }
                return 400, body, ""
            if self.observation_call == 2:
                self_url = "/Observation?" + urlencode({"patient": subject})
                body = {
                    "resourceType": "Bundle",
                    "link": [{"relation": "self", "url": self_url}],
                    "entry": [{
                        "search": {"mode": "outcome"},
                        "resource": {
                            "resourceType": "OperationOutcome",
                            "issue": [{
                                "severity": "warning",
                                "code": "not-supported",
                                "details": {"text": corrective_text},
                            }],
                        },
                    }],
                }
                return 200, body, ""
            body = {
                "resourceType": "OperationOutcome",
                "issue": [{
                    "severity": "error",
                    "code": "not-supported",
                    "details": {"text": "The requested modifier is not supported."},
                }],
            }
            return 400, body, ""

    result = probe_error_fidelity(
        LocalAClient(), ProbeContext("tenant", "token"))
    assert result.grade == "A"
    assert result.passed is True


def test_injected_mock_proxy_profile_passes_the_full_error_contract(
        client, tenant_id, step_up_token, caplog):
    import httpx
    import json
    from unittest.mock import MagicMock, patch

    from r6.fhir_proxy import FHIRUpstreamProxy

    hostile_name = "Quintavious Zzyzxbarton"
    hostile_url = "https://db.internal.example/patient/secret"

    def response(status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.content = json.dumps(body).encode()
        resp.json.return_value = body
        return resp

    rejected = response(400, {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": "error", "code": "invalid",
            "details": {"text": f"Patient {hostile_name} rejected"},
            "diagnostics": hostile_url,
        }],
    })
    unauthorized = response(401, {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "login",
                   "diagnostics": "expired proxy credential"}],
    })
    server_error = response(500, {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "exception",
                   "diagnostics": hostile_url}],
    })

    proxy = FHIRUpstreamProxy("https://mock-fhir.example")
    proxy._client.get = MagicMock(
        side_effect=[
            rejected,
            unauthorized,
            server_error,
            httpx.ReadTimeout(hostile_url),
        ])
    local_client = FlaskProbeClient(client)

    class InjectedProxyClient:
        def request(self, *args, **kwargs):
            with patch("r6.routes.get_proxy_for_request", return_value=proxy):
                return local_client.request(*args, **kwargs)

    try:
        report = run_conformance(
            local_client, _ctx(None, tenant_id, step_up_token),
            proxy_client=InjectedProxyClient())
    finally:
        proxy.close()

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.grade == "F"  # local F remains the weakest profile
    assert result.coverage == "local+proxy"
    assert result.profiles["proxy"] == {
        "status": "run", "grade": "A",
        "checks": [
            "proxy rejection preserves sanitized outcome",
            "proxy auth failure is not not-found",
            "proxy server failure is truthful",
            "proxy timeout is truthful",
            "proxy failure audit is truthful",
        ],
    }
    proxy_checks = [c for c in result.checks if c.name.startswith("proxy ")]
    assert proxy_checks and all(c.passed for c in proxy_checks)
    assert hostile_name not in json.dumps(result.profiles)
    assert hostile_url not in report.render()
    assert hostile_name not in caplog.text
    assert hostile_url not in caplog.text
    from r6.models import AuditEventRecord
    details = [
        event.detail or ""
        for event in AuditEventRecord.query.filter_by(tenant_id=tenant_id).all()
    ]
    assert all(hostile_name not in detail for detail in details)
    assert all(hostile_url not in detail for detail in details)
