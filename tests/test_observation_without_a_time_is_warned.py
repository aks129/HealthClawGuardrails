"""#485, decided as option 2: an Observation with no effective[x] is
accepted and told so.

Rejecting would be a wire-contract change with no deprecation window for
every caller that omits it today (the action rail, the MCP tools, partner
integrations). The warning names the gap the way #484 made coverage
visible; option 1 or 3 stays open behind a deprecation window if undated
readings keep arriving.
"""
from r6.validator import R6Validator


def _obs(**extra):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "2093-3"}]},
            "valueQuantity": {"value": 188, "unit": "mg/dL"}, **extra}


def _issues(resource):
    result = R6Validator().validate_resource(resource)
    return result["valid"], result["operation_outcome"]["issue"]


def test_an_undated_observation_is_accepted_with_a_warning():
    valid, issues = _issues(_obs())
    assert valid is True
    warnings = [i for i in issues if i["severity"] == "warning"]
    assert [i["expression"] for i in warnings] == [["Observation.effective[x]"]]
    assert "cannot be trended" in warnings[0]["diagnostics"]


def test_any_effective_variant_satisfies_it():
    for key, value in (("effectiveDateTime", "2026-09-14T10:00:00Z"),
                       ("effectivePeriod", {"start": "2026-09-14"}),
                       ("effectiveInstant", "2026-09-14T10:00:00Z")):
        valid, issues = _issues(_obs(**{key: value}))
        assert valid is True
        assert not [i for i in issues
                    if i.get("expression") == ["Observation.effective[x]"]], key


def test_the_warning_never_masks_a_real_error():
    valid, issues = _issues({"resourceType": "Observation", "status": "final"})
    assert valid is False
    assert {i["severity"] for i in issues} == {"error", "warning"}


def test_the_warning_does_not_cost_the_caller_the_coverage_note():
    """#484's disclosure of what was NOT checked rides on every accepted
    resource, warned or clean."""
    valid, issues = _issues(_obs())
    assert valid is True
    info = " ".join(i["diagnostics"] for i in issues
                    if i["severity"] == "information")
    assert "NOT checked: profile conformance" in info
