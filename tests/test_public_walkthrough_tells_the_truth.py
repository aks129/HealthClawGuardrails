"""`scripts/walkthrough-upstream.sh` is now load-bearing. Pin it.

Issue #530: three merged documents asserted that two of four connector kinds
were proven live, and the run behind that claim came from a script in an
uncommitted scratch directory. Nobody but its author could re-execute it, so
the most load-bearing measured claim in the process documents sat in the
document whose thesis is that unreproducible assertions are the defect.

Committing the script fixes that once. This file is what stops it coming back,
and it guards two different things:

  1. The script asserts the status codes THIS SERVER returns. Its Aidbox
     sibling shipped asserting 401 for a bare clinical write where the server
     returns 428 — the human-in-the-loop check runs in a before_request hook,
     ahead of the handler's auth gate — and a reader's first run would have
     gone red on the first assertion.
     `tests/test_aidbox_example_tells_the_truth.py` is that guard for
     `walkthrough.sh`; this is the same guard for the public-server script,
     written now rather than after the same defect ships twice.

  2. The documents name paths that exist. A doc citing a deleted script is
     #530 again with an extra step. Deleting the script or the transcripts
     while the topology, the PRDs and hard-truths still point at them turns
     this red.

Needs no Docker, no network, and no public FHIR server: the status codes come
from this repo's app, and the script is checked against them.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALKTHROUGH = ROOT / "scripts" / "walkthrough-upstream.sh"
TRANSCRIPTS = ROOT / "docs" / "evidence" / "2026-09-04-set2-rerun"
RERUN_PACK = ROOT / "docs" / "evidence" / "2026-09-04-set2-connectors-rerun.md"

#: A `write <expected> "<label>" [-H 'Header: value' ...]` call in step 1b.
_WRITE_CALL = re.compile(r"^\s*write\s+(\d{3})\s+\"([^\"]+)\"(.*)$", re.M)
_HEADER_ARG = re.compile(r"-H\s+(['\"])([^'\"]+)\1")


def _write_matrix():
    """(expected_status, label, {header: value}) for each write in step 1b."""
    rows = []
    # Join shell line-continuations first. The last row spans two lines, and
    # a line-anchored match reads it as having one header instead of two —
    # which makes this test agree with a matrix that has no fourth row.
    script = re.sub(r"\\\n\s*", " ", WALKTHROUGH.read_text())
    for status, label, rest in _WRITE_CALL.findall(script):
        headers = {}
        for _, raw in _HEADER_ARG.findall(rest):
            name, _, value = raw.partition(":")
            headers[name.strip()] = value.strip()
        rows.append((int(status), label, headers))
    return rows


@pytest.fixture(scope="module")
def matrix():
    rows = _write_matrix()
    # A pin that stops finding its subject reports success forever. Four is
    # the point of the matrix — one row per combination of the two gates.
    assert len(rows) == 4, (
        f"expected 4 write() calls in walkthrough-upstream.sh, found "
        f"{len(rows)}. If step 1b was restructured, restructure this test "
        "with it rather than relaxing the count.")
    return rows


@pytest.fixture
def no_stray_validator(monkeypatch):
    """Pin the profile validator to unavailable (#488).

    `FHIR_VALIDATOR_URL` defaults to http://localhost:8080 and availability is
    "GET /health answered under 400", so anything bound on the developer's
    8080 is mistaken for a validator and turns the 201 row into a 422. The
    script runs against a remote upstream with nothing on local 8080, so
    unavailable is what it actually runs with.
    """
    from r6.validator import R6Validator
    monkeypatch.setattr(R6Validator, "_is_validator_available",
                        lambda self: False)


class TestTheWalkthroughAssertsWhatTheServerReturns:
    """MUTATION: change any expected status in the script -> red."""

    def test_every_row_matches(self, client, tenant_id, step_up_token, matrix,
                               no_stray_validator):
        body = {
            "resourceType": "Observation",
            "status": "final",
            "subject": {"reference": "Patient/pt-demo"},
            "effectiveDateTime": "2026-09-04",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "85354-9"}]},
            "valueQuantity": {"value": 128, "unit": "mmHg"},
        }
        wrong = []
        for expected, label, headers in matrix:
            sent = {"X-Tenant-Id": tenant_id,
                    "Content-Type": "application/fhir+json"}
            for name, value in headers.items():
                # The script interpolates the token it minted at run time.
                sent[name] = (step_up_token
                              if "${token}" in value or "$token" in value
                              else value)
            response = client.post("/r6/fhir/Observation",
                                   data=json.dumps(body), headers=sent)
            if response.status_code != expected:
                wrong.append(f"{label!r}: script says {expected}, "
                             f"server returns {response.status_code}")
        assert not wrong, (
            "walkthrough-upstream.sh asserts status codes this server does "
            "not return. Someone re-running the #530 evidence against a "
            "fresh checkout sees FAIL:\n  " + "\n  ".join(wrong))

    def test_the_two_gates_are_independent(self, matrix):
        """Neither credential nor confirmation alone may reach 2xx.

        The property the matrix exists to demonstrate. An edit that leaves
        four rows but lets one gate carry the write on its own has removed
        the demonstration while keeping its shape.
        """
        alone = [(status, label) for status, label, headers in matrix
                 if len(headers) == 1 and status < 300]
        assert not alone, (
            "a single gate reached a 2xx in the matrix, so the script no "
            f"longer shows the two gates are independent: {alone}")

        both = [status for status, _, headers in matrix if len(headers) == 2]
        assert both == [201], (
            "expected exactly one row presenting both gates, expecting 201; "
            f"found {both}")


class TestTheWalkthroughCannotPassVacuously:
    """The checks that separate this run from a false pass.

    `smoke_medplum.py` reported 7 of 8 checks green against a Medplum that did
    not exist (2026-08-16 pack, R2), and two of its passes were on the body of
    a 401 (R3). Both are the same defect: an assertion satisfied by a refusal.
    Three lines in this script are the whole distance from that, so a future
    edit may not quietly drop them.
    """

    def test_it_refuses_to_run_in_local_mode(self):
        script = WALKTHROUGH.read_text()
        assert 'mode != "upstream"' in script, (
            "the preflight no longer refuses local mode. Every step below it "
            "would pass against the proxy's own SQLite and prove nothing "
            "about the connector.")

    def test_it_asserts_the_record_came_from_the_upstream(self):
        """The COMPARISON, not the word.

        This assertion first read `'_source' in script`, which stayed green
        when the conditional around it was replaced with `if False:` — the
        string survives in the print statement and in the field list that
        formats the output. A guard written from the name of the property
        rather than from the property is hard-truths §5, committed by the
        person who wrote that document's re-run.
        """
        script = WALKTHROUGH.read_text()
        assert 'proxied.get("_source") != "upstream"' in script, (
            "the _source comparison is gone or was rewritten. It is the one "
            "line that failed in the Medplum QA run against no Medplum at "
            "all (R2), and without it this script passes against the proxy's "
            "own SQLite store.")
        assert "sys.exit(1)" in script.split(
            'proxied.get("_source") != "upstream"')[1][:400], (
            "the _source check no longer exits on failure, so it reports a "
            "false pass instead of refusing.")

    def test_it_checks_the_resource_before_checking_its_contents(self):
        """Redaction assertions must be gated on having received a record."""
        script = WALKTHROUGH.read_text()
        resource_check = script.find('resourceType") != "Patient"')
        leak_check = script.find("survived redaction")
        assert resource_check != -1, "the shape check before redaction is gone"
        assert leak_check != -1, "the redaction assertion is gone"
        assert resource_check < leak_check, (
            "the redaction assertion now runs before the check that a Patient "
            "was returned, so it passes vacuously on an OperationOutcome — "
            "the defect #499 fixed in the Aidbox script.")

    def test_the_conformance_step_names_its_one_known_failure(self):
        """Not 'grade >= B'. A threshold hides a second regression."""
        script = WALKTHROUGH.read_text()
        assert "KNOWN = {'error_fidelity'}" in script, (
            "the conformance step no longer asserts that error_fidelity is "
            "the ONLY failure. A softer threshold passes while a second "
            "property regresses, and stops passing at 7/7 when #498 closes.")


class TestTheDocumentsPointAtSomethingThatExists:
    """#530's own property: the claim must stay checkable by a stranger."""

    def test_the_script_and_transcripts_are_committed(self):
        assert WALKTHROUGH.exists(), f"{WALKTHROUGH} is gone"
        assert RERUN_PACK.exists(), f"{RERUN_PACK} is gone"
        for name in ("hapi-run.txt", "generic-run.txt",
                     "negative-control-local-mode.txt"):
            assert (TRANSCRIPTS / name).exists(), (
                f"{name} is gone. The documents cite it as the evidence for "
                "'2 of 4 proven live'.")

    @pytest.mark.parametrize("doc", [
        "docs/2026-08-16-system-topology.md",
        "docs/prd/02-connectors.md",
        "docs/prd/README.md",
        "docs/2026-08-16-hard-truths.md",
        "docs/2026-08-16-tester-program.md",
        "docs/evidence/2026-08-16-set2-connectors.md",
    ])
    def test_every_claim_site_says_when_it_was_re_run(self, doc):
        """A bare '2 of 4' with no date and no runner is the #530 defect."""
        text = (ROOT / doc).read_text()
        assert "2026-09-04" in text, (
            f"{doc} carries the connector claim but no longer says when it "
            "was last re-run, or by whom. That is what #530 was.")

    def test_no_operator_username_in_the_transcripts(self):
        """Redaction the 2026-08-16 pack needed and this one must not."""
        for path in sorted(TRANSCRIPTS.glob("*.txt")):
            text = path.read_text()
            assert "/Users/" not in text and "/home/" not in text, (
                f"{path.name} contains a home-directory path. An operator's "
                "OS username in an evidence pack merged to a public repo is "
                "the 2026-08-16 incident.")
