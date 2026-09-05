"""The conformance probe must survive being run more than once.

`_synthetic_patient()` returned a constant body. A constant body cannot be
created twice on a server that de-duplicates, and the public HAPI server
does. Reproduced against hapi.fhir.org directly:

    create 1: 201
    create 2: 412  HAPI-2840: Can not create resource duplicating existing
                   resource: Patient/137354953

So the first `$conformance` run against a HAPI deployment passed and every
run after it failed. It failed in the worst available way: the Patient create
returned no id, the dependent Observation then 400'd on a dangling subject
reference, and the scorecard attributed both to the guardrails. Measured on a
live run, 2026-08-16 against hapi.fhir.org: **Grade F, 1/7 — five of the six
failing properties were the collision and not a defect (only `error_fidelity`,
#498, was real), and two of those named a gate the same session had just
watched work.**

That is the flagship artifact accusing the thing it exists to certify, in
front of whoever ran it against their own server. The grade is published, and
a partner evaluating us reruns it — which is precisely the second run.

The F expired the day it was measured: #514 is this fix, and the same
deployment re-measured **Grade B, 6/7** on 2026-09-04. The transcript of the
F is `docs/evidence/2026-08-16-set2-connectors.md` §3. The count above read
"four of the six" here and in four other places until 2026-09-04; no count in
that transcript yields four (#605).

TWO PROPERTIES, and the second is why this file is not one assertion:

  the body must DIFFER between calls        or the create collides
  the five redacted values must NOT differ  or the redaction checks are
                                            searching for something they
                                            themselves just made up
"""

import json

from r6.conformance.probes import (
    _FAMILY,
    _GIVEN,
    _PHONE,
    _SSN,
    _STREET,
    _synthetic_patient,
)

_RUN_MARKER = "urn:healthclaw:conformance-run"


def _markers(patient):
    return [i["value"] for i in patient["identifier"]
            if i.get("system") == _RUN_MARKER]


def test_two_calls_do_not_produce_the_same_resource():
    """MUTATION: make the marker a module-level constant -> red.

    A constant computed once at import is not enough either: a long-lived
    server answers $conformance many times from one process, so every run
    after the first would collide exactly as before.
    """
    first, second = _synthetic_patient(), _synthetic_patient()
    assert json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True)
    assert _markers(first) and _markers(second)
    assert _markers(first) != _markers(second)


def test_the_values_redaction_is_checked_on_stay_constant():
    """The other half, and the one a careless fix breaks.

    The redaction probes assert that `_SSN`, `_FAMILY` and friends are absent
    from the response. Randomising those would make every check pass against
    a server that returned the record verbatim, because the probe would be
    searching for a string it invented after the fact.

    MUTATION: randomise the SSN, name, phone or street -> red.
    """
    first, second = _synthetic_patient(), _synthetic_patient()
    for patient in (first, second):
        blob = json.dumps(patient)
        for value in (_SSN, _FAMILY, _GIVEN, _PHONE, _STREET):
            assert value in blob, (
                f"{value!r} is what a redaction check looks for; a probe "
                "subject that does not contain it cannot detect a leak")

    assert first["name"] == second["name"]
    assert first["telecom"] == second["telecom"]
    assert first["address"] == second["address"]


def test_the_marker_is_an_identifier_so_redaction_removes_it():
    """It rides where redaction already strips wholesale, so uniqueness never
    reaches a caller and never appears in a scorecard."""
    patient = _synthetic_patient()
    systems = [i.get("system") for i in patient["identifier"]]
    assert _RUN_MARKER in systems
    assert "http://hl7.org/fhir/sid/us-ssn" in systems

    from r6.redaction import apply_redaction

    redacted = json.dumps(apply_redaction(dict(patient)))
    assert _markers(patient)[0] not in redacted, (
        "the run marker survived redaction, so it would appear in a response "
        "and could be mistaken for a real identifier")
