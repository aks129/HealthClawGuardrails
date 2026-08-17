"""Live smoke: prove the HealthClaw guardrails wrap a Medplum-backed FHIR store.

Runs against a HealthClaw Flask instance configured with MEDPLUM_BASE_URL /
MEDPLUM_CLIENT_ID / MEDPLUM_CLIENT_SECRET (see
docs/recipes/healthclaw-in-front-of-medplum.md). Creates a SYNTHETIC patient in
Medplum through HealthClaw, reads it back, and verifies the guardrails.

Usage:
    python scripts/smoke_medplum.py --base-url https://your-healthclaw \
        --tenant-id <tenant> --step-up-token <token>

Exit code 0 = every check RAN and passed.

WHY THAT SENTENCE IS PHRASED THAT WAY. This script reported "7/8 guardrail
checks passed" against a HealthClaw with no Medplum behind it at all — every
check but one describing local SQLite, and the one that would have caught it
("Medplum-sourced") counted as a single lost point among seven wins.

Worse, with read auth enabled the read-back returns 401, and two of the
redaction checks are `"000-00-1234" not in blob`. An empty body contains no
SSN either, so they PASSED on the body of a refusal. That is the same vacuous
assertion as the Aidbox example's redaction demo (#499): a check that cannot
distinguish "the value was removed" from "there was no response".

So this file has GATES as well as checks. A gate is a precondition, and a
failing one STOPS the run — the code after it does not execute, so there is
no way to evaluate a check against a response that never arrived. The summary
then names the gate that stopped it instead of printing a fraction, because
the fraction would be over the checks that happened to run. That fraction is
what made a HealthClaw with no Medplum behind it read as 7/8.
"""

import argparse
import json
import sys

SYNTHETIC_PATIENT = {
    "resourceType": "Patient",
    "name": [{"family": "Testpatient", "given": ["Smoke"]}],
    "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                    "value": "000-00-1234"}],
    "telecom": [{"system": "phone", "value": "555-000-1234"}],
    "birthDate": "1980-01-01",
}


class StoppedEarly(Exception):
    """A gate failed. Raised so nothing downstream is even evaluated."""


class Runner:
    """Checks, gates, and a summary that cannot round a partial run up.

    Importable and driven directly by tests/test_seed_scripts_still_run.py,
    because CI cannot run this script for real — it needs a live Medplum
    behind a live HealthClaw. The previous guard on this file was a grep for
    the string "Content-Type", which is the best a procedural script allows
    and is not much.
    """

    PASS, FAIL = "PASS", "FAIL"

    def __init__(self):
        self.results = []
        self.stopped_by = None

    def check(self, name, ok, detail=""):
        """An observation. Failing one does not invalidate the others."""
        if self.stopped_by is not None:
            raise AssertionError(
                f"check {name!r} ran after gate {self.stopped_by!r} failed")
        self._record(name, self.PASS if ok else self.FAIL, detail)
        return bool(ok)

    def gate(self, name, ok, detail=""):
        """A precondition. Failing one STOPS the run.

        This is the whole point of the file. A check evaluated against a
        response that never arrived is not a passing check — `"secret" not in
        ""` is True — and a run that never reached Medplum has nothing to say
        about Medplum. Raising means the code after it does not execute, so
        there is no way to accidentally evaluate it anyway.
        """
        self._record(name, self.PASS if ok else self.FAIL, detail)
        if ok:
            return True
        self.stopped_by = name
        raise StoppedEarly(name)

    def _record(self, name, verdict, detail):
        self.results.append((name, verdict, detail))
        print(f"  [{verdict}] {name}" + (f" — {detail}" if detail else ""))

    def summary(self):
        passed = sum(1 for _, v, _ in self.results if v == self.PASS)
        line = f"{passed}/{len(self.results)} guardrail checks passed"
        if self.stopped_by is not None:
            # Never "7/8" for a run that stopped. The denominator would be the
            # checks that HAPPENED to run, which is the number that made a
            # HealthClaw with no Medplum behind it look 87% healthy.
            line += (f" before the run STOPPED at the gate "
                     f"'{self.stopped_by}'. The checks after it did not run, "
                     "and none of them counts as passed")
        return line + "."

    def exit_code(self):
        failed = any(v == self.FAIL for _, v, _ in self.results)
        return 1 if failed or self.stopped_by is not None else 0

    def finish(self):
        print("\n" + self.summary())
        sys.exit(self.exit_code())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--tenant-id", required=True)
    ap.add_argument("--step-up-token", required=True,
                    help="tenant-bound step-up token (POST /r6/fhir/internal/step-up-token)")
    args = ap.parse_args()

    import requests
    base = args.base_url.rstrip("/")
    read_hdr = {"X-Tenant-Id": args.tenant_id}
    write_hdr = {**read_hdr, "Content-Type": "application/fhir+json",
                 "X-Step-Up-Token": args.step_up_token,
                 "X-Human-Confirmed": "true"}

    run = Runner()
    try:
        _run_checks(run, requests, base, read_hdr, write_hdr)
    except StoppedEarly:
        pass  # the gate already recorded and printed itself
    run.finish()


def _run_checks(run, requests, base, read_hdr, write_hdr):
    check, gate = run.check, run.gate

    # 1. Write is gated: create without step-up must be refused before Medplum.
    #
    # Content-Type MATTERS here, and its absence is why this check spent its
    # life testing something else. Without it the body never parses, the
    # handler returns 400 ("Request body must be valid JSON") from the depth-
    # bounded parse that deliberately runs ahead of the auth gate (#312), and
    # the step-up gate this line is named after is never reached at all.
    # A malformed body is refused before authentication on purpose; this
    # check is about the credential, so it has to send a well-formed one.
    no_step_up = {**read_hdr, "Content-Type": "application/fhir+json"}
    r = requests.post(f"{base}/r6/fhir/Patient", headers=no_step_up,
                      data=json.dumps(SYNTHETIC_PATIENT))
    check("write blocked without step-up (401)", r.status_code == 401,
          f"got {r.status_code}")

    # 2. Guardrailed write reaches Medplum.
    r = requests.post(f"{base}/r6/fhir/Patient", headers=write_hdr,
                      data=json.dumps(SYNTHETIC_PATIENT))
    ok_create = r.status_code in (200, 201)
    pid = (r.json() or {}).get("id") if ok_create else None
    gate("guardrailed create -> Medplum (201)", ok_create and bool(pid),
         f"id={pid}" if pid else f"status {r.status_code}")

    # 3. Read back through HealthClaw is redacted.
    r = requests.get(f"{base}/r6/fhir/Patient/{pid}", headers=read_hdr)
    # GATE, not check: a 401 body carries no SSN either, and the three
    # redaction assertions below would pass on it while proving nothing.
    gate("read returns 200", r.status_code == 200, f"status {r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    blob = json.dumps(body)
    fam = (body.get("name", [{}])[0] or {}).get("family", "")

    # GATE: this script is named after Medplum. If the resource came from
    # local storage, everything below describes SQLite, and reporting it as
    # a score out of eight is how a run with no Medplum in existence read as
    # 7/8.
    gate("Medplum-sourced (not local storage)",
         body.get("_source") == "upstream", f"_source={body.get('_source')!r}")

    check("name redacted (initial only)", fam == "T.", f"family={fam!r}")
    check("SSN masked in read", "000-00-1234" not in blob)
    check("phone redacted in read", "555-000-1234" not in blob)

    # 4. Audit trail recorded.
    a = requests.get(f"{base}/r6/fhir/AuditEvent?_count=10", headers=read_hdr)
    audit_ok = a.status_code == 200 and (a.json().get("total", 0) or
                                         len(a.json().get("entry", []))) >= 1
    check("access audited (AuditEvent present)", audit_ok)


if __name__ == "__main__":
    main()
