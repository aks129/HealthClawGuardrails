"""The guardrail conformance harness must certify our OWN deployment.

This is both the unit test for r6/conformance and a standing CI gate: if any of
the six guardrail properties regresses, this suite fails. The same harness runs
against a live partner deployment via scripts/guardrail_conformance.py.
"""

from r6.conformance import (
    FlaskProbeClient, ProbeContext, run_conformance, PROPERTIES,
)


def _ctx(app, tenant_id, step_up_token):
    return ProbeContext(tenant=tenant_id, step_up_token=step_up_token)


def test_our_deployment_passes_every_guardrail_property(client, tenant_id, step_up_token):
    report = run_conformance(FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))
    failed = [r.property for r in report.results if not r.passed]
    assert report.passed, (
        f"Guardrail conformance FAILED for: {failed}\n" + report.render())
    # all six properties were actually exercised
    assert {r.key for r in report.results} == set(PROPERTIES)
    assert report.score == (len(PROPERTIES), len(PROPERTIES))
    assert report.grade == "A"


def test_report_is_json_serializable(client, tenant_id, step_up_token):
    report = run_conformance(FlaskProbeClient(client), _ctx(None, tenant_id, step_up_token))
    d = report.to_dict()
    assert d["passed"] is True
    assert d["grade"] == "A"
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
