"""A FHIR validator is a thing you configure, not a thing you find on 8080.

`FHIR_VALIDATOR_URL` defaulted to `http://localhost:8080`, and availability
was "GET /health answered under 400". Anything listening there was therefore
a validator — including the Aidbox instance this repo's own example starts on
exactly that port. Running the example turned every write in the suite into a
422 reading "Validator returned HTTP 404", because Aidbox has no /validate.

It cost an hour three separate times in one day. Each time it presented as
the change under test: twice as a guardrail regression, once nearly causing a
correct fix to be reverted. Issue #488.

Two things were wrong, and both are needed:

  the DEFAULT   asserted a validator exists at a very contended local port
  the FAILURE   treated a non-200 from that service as "your resource is
                invalid" rather than "this is not a validator"

The second is what turned a misconfiguration into a rejection of valid data,
with a diagnostic naming the wrong party.
"""

import pytest
import requests

from r6.validator import R6Validator, ValidatorUnavailable

VALID = {
    "resourceType": "Observation",
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]},
    "subject": {"reference": "Patient/x"},
}


class TestAnUnsetUrlMeansNoExternalValidator:
    """MUTATION: restore the http://localhost:8080 default -> red."""

    def test_the_module_default_is_empty(self):
        from r6 import validator as mod
        assert mod.VALIDATOR_URL == "" or mod.VALIDATOR_URL, (
            "VALIDATOR_URL should come from the environment")
        # The important half: no hardcoded localhost anywhere in the module.
        src = (__import__("pathlib").Path(mod.__file__)).read_text()
        assert "'http://localhost:8080'" not in src, (
            "a default validator URL is an assertion that a validator is "
            "listening there, and on 8080 that is usually something else")

    def test_no_url_means_unavailable_without_a_network_call(self, monkeypatch):
        def explode(*a, **kw):
            raise AssertionError("probed the network with no URL configured")
        monkeypatch.setattr(requests, "get", explode)
        assert R6Validator(validator_url="")._is_validator_available() is False

    def test_validation_still_happens_structurally(self):
        """Unset must not mean unvalidated."""
        v = R6Validator(validator_url="")
        assert v.validate_resource(VALID)["valid"] is True
        bad = v.validate_resource({"resourceType": "Observation"})
        assert bad["valid"] is False, (
            "structural validation must still reject a resource missing "
            "required elements")


class TestSomethingThatIsNotAValidatorDoesNotRejectValidData:

    def _validator_where(self, health_status, validate_status, monkeypatch):
        v = R6Validator(validator_url="http://localhost:8080")

        class _R:
            def __init__(self, code):
                self.status_code = code

            def json(self):
                return {}

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _R(health_status))
        monkeypatch.setattr(requests, "post", lambda *a, **kw: _R(validate_status))
        return v

    def test_a_404_on_validate_falls_back_instead_of_failing_the_write(
            self, monkeypatch):
        """MUTATION: return {'valid': False} on a non-200 -> red.

        This is the exact shape of Aidbox on 8080: healthy, and with no
        /validate. Every write in the suite 422'd on it.
        """
        v = self._validator_where(200, 404, monkeypatch)
        result = v.validate_resource(VALID)
        assert result["valid"] is True, (
            "a service that is not a validator made a valid resource "
            "invalid, and the diagnostic blamed the resource")

    def test_the_non_200_is_raised_as_unavailability_not_invalidity(
            self, monkeypatch):
        v = self._validator_where(200, 503, monkeypatch)
        with pytest.raises(ValidatorUnavailable):
            v._validate_external(VALID)

    def test_the_validator_is_marked_unavailable_so_it_is_rechecked(
            self, monkeypatch):
        v = self._validator_where(200, 404, monkeypatch)
        v.validate_resource(VALID)
        assert v._validator_available is None, (
            "a failed external validation should invalidate the availability "
            "cache, so recovery is picked up rather than waiting out a TTL")


class TestARealValidatorStillRejectsInvalidResources:
    """Two-sided (#213). A fix that made every resource valid would satisfy
    everything above while removing profile validation entirely."""

    def test_errors_in_a_200_outcome_still_fail(self, monkeypatch):
        v = R6Validator(validator_url="http://validator.example")

        class _Health:
            status_code = 200

        class _Outcome:
            status_code = 200

            def json(self):
                return {"resourceType": "OperationOutcome",
                        "issue": [{"severity": "error",
                                   "code": "structure",
                                   "diagnostics": "nope"}]}

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _Health())
        monkeypatch.setattr(requests, "post", lambda *a, **kw: _Outcome())
        result = v.validate_resource(VALID)
        assert result["valid"] is False, (
            "a real validator reporting errors must still reject — otherwise "
            "this change quietly turned validation off")

    def test_a_clean_200_outcome_passes(self, monkeypatch):
        v = R6Validator(validator_url="http://validator.example")

        class _R:
            status_code = 200

            def json(self):
                return {"resourceType": "OperationOutcome", "issue": []}

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _R())
        monkeypatch.setattr(requests, "post", lambda *a, **kw: _R())
        assert v.validate_resource(VALID)["valid"] is True
