#!/usr/bin/env python3
"""The positive half of the dependency-audit gate.

"No known vulnerabilities found" is satisfied *hardest* by an environment
containing nothing, and that is precisely how Compliance Gate 2 passed for
as long as it existed: CI ran pip-audit under `uvx`, which builds an
ephemeral interpreter holding pip-audit and nothing else. The gate audited
zero project packages, printed "No known vulnerabilities found", and exited
0. Two real advisories sat on the default branch underneath it.

This is the repo's recurring defect shape (docs/2026-08-02-retro.md): a
guardrail produced nothing and the caller read that as an answer. The
countermeasure is the one PR #400 applied to the conformance grade — assert
the positive, not only the absence. A clean audit only means something once
you know what was audited.

Usage:
    check_audit_coverage.py <pip-audit --format json output>

Exits non-zero if the audited set does not contain the project's own
dependencies, naming which ones were missing.
"""
import json
import sys
from pathlib import Path

# Direct dependencies from pyproject.toml, spread across the areas this
# service actually depends on: the web framework, the ORM, the HTTP client
# every outbound call crosses, and the crypto library backing token signing.
# Any interpreter that really holds this project has all four. An empty or
# wrong one has none.
EXPECTED = {"flask", "sqlalchemy", "requests", "cryptography"}

# A floor on the audited count. The locked set is ~99 packages; a handful
# would mean pip-audit found *an* environment but not this project's.
MIN_AUDITED = 40


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"FAIL: {report} does not exist — pip-audit wrote no report, so "
              "there is no evidence any package was examined.", file=sys.stderr)
        return 1

    try:
        payload = json.loads(report.read_text())
    except json.JSONDecodeError as exc:
        print(f"FAIL: {report} is not JSON ({exc}); the audit produced no "
              "readable evidence.", file=sys.stderr)
        return 1

    audited = {d.get("name", "").lower() for d in payload.get("dependencies", [])}
    missing = sorted(EXPECTED - audited)

    if missing:
        print(f"FAIL: pip-audit examined {len(audited)} package(s) and none of "
              f"{missing} were among them. The gate is pointed at the wrong "
              "interpreter — a clean result from it means nothing. Run it "
              "under the project environment (uv sync --frozen, then "
              "uv run --with pip-audit ...).", file=sys.stderr)
        return 1

    if len(audited) < MIN_AUDITED:
        print(f"FAIL: pip-audit examined only {len(audited)} package(s); the "
              f"locked set is far larger than {MIN_AUDITED}. Something is "
              "auditing a partial environment.", file=sys.stderr)
        return 1

    print(f"pip-audit examined {len(audited)} packages, including "
          f"{sorted(EXPECTED)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
