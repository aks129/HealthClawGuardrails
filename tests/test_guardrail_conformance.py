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


def test_error_fidelity_grade_uses_the_worst_executed_profile():
    from r6.conformance import _error_fidelity_grade
    from r6.conformance.probes import _rejection_grade

    assert _error_fidelity_grade(["A"]) == "A"
    assert _error_fidelity_grade(["A", "C"]) == "C"
    assert _error_fidelity_grade(["C", "F", "A"]) == "F"
    assert _error_fidelity_grade([]) == "F"
    assert _rejection_grade(200, {}) == "F"
    assert _rejection_grade(502, {"resourceType": "OperationOutcome"}) == "C"
    assert _rejection_grade(400, {"resourceType": "OperationOutcome"}) == "A"


def test_local_error_fidelity_records_the_known_f_baseline(client, tenant_id,
                                                            step_up_token):
    report = run_conformance(
        FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))

    result = next(r for r in report.results if r.key == "error_fidelity")
    assert result.grade == "F"
    assert result.coverage == "local-fhir-only"
    assert result.profiles == {
        "local": {"status": "run", "grade": "F"},
        "mcp": {"status": "not_run"},
        "proxy": {"status": "not_run"},
    }
    assert {check.name for check in result.checks} == {
        "strict unknown parameter is rejected",
        "strict rejection is audited as a failure",
        "lenient unknown parameter carries an outcome warning",
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
            assert arguments["resource_type"] == "Widget"
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
    assert result.profiles["mcp"] == {"status": "run", "grade": "C"}
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
    assert result.profiles["mcp"] == {"status": "run", "grade": "A"}


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
                Response({"result": {"protocolVersion": "2025-06-18"}},
                         {"Mcp-Session-Id": "session-1"}),
                Response({}),
                Response({"result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "isError": True,
                }}),
            ]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    session = Session()
    client = LiveMCPProbeClient("https://mcp.example.test/mcp", session=session)
    result = client.call_tool("fhir_search", {"resource_type": "Widget"})

    assert result["isError"] is True
    assert len(session.calls) == 3
    assert session.calls[1][1]["json"]["method"] == "notifications/initialized"
    assert session.calls[2][1]["headers"]["Mcp-Session-Id"] == "session-1"
    assert session.calls[2][1]["json"]["method"] == "tools/call"


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
    assert result.profiles["proxy"] == {"status": "run", "grade": "A"}
    proxy_checks = [c for c in result.checks if c.name.startswith("proxy ")]
    assert proxy_checks and all(c.passed for c in proxy_checks)
    assert hostile_name not in json.dumps(result.profiles)
    assert hostile_url not in report.render()
    assert hostile_name not in caplog.text
    assert hostile_url not in caplog.text
