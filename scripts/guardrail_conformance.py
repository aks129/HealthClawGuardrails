"""Guardrail conformance scorecard for any HealthClaw deployment.

Proves the seven guardrail properties actually hold — including error fidelity
on rejected requests — by probing a live endpoint with synthetic data. Partners
can run this against their own deployment (or ours) to verify the guardrails are
real, not marketing.

Usage:
    python scripts/guardrail_conformance.py \
        --base-url https://app.healthclaw.io \
        --step-up-token <token>          # mint via POST /r6/fhir/internal/step-up-token
    # For protected MCP coverage, also set MCP_AUTH_TOKEN and pass --mcp-url.
    # add --json for machine-readable output; exits non-zero if grade < A.

The probes WRITE. They create synthetic Patients and Observations in the tenant
they grade, and they do not remove them.

That is why --tenant now defaults to the dedicated self-test tenant instead of
being required. The example above used to name the public demo tenant, and
anyone who followed it left probe patients in the tenant the product demos
from: six of them accumulated in production, and a physician advisor found the
resulting mess on camera the day before a launch recording (#463).

The demo tenant is not named anywhere in this file on purpose. A tenant id
written here is one somebody will paste, and the test that enforces that
caught this very paragraph on its first draft.

The self-conformance ENDPOINT already had this right — r6/conformance/routes.py
writes to a dedicated tenant "so a caller's data is never touched." The two
paths now agree, and share one constant.

This docstring used to close by promising a live run leaves real patient
records alone. Read carefully, that was about the DATA the probes send, which
is obviously fake. Read the way anyone actually reads it, it said the run is
harmless to whatever tenant you point it at. It is not, and that gap between
the two readings is the whole defect.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from r6.conformance import (  # noqa: E402
    LiveMCPProbeClient,
    LiveProbeClient,
    ProbeContext,
    run_conformance,
)
from r6.conformance.snapshot import SELFTEST_TENANT  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="HealthClaw guardrail conformance scorecard")
    ap.add_argument("--base-url", required=True, help="e.g. https://app.healthclaw.io")
    ap.add_argument("--tenant", default=SELFTEST_TENANT,
                    help=(f"tenant to grade (default: {SELFTEST_TENANT}). "
                          "THE PROBES WRITE HERE and do not clean up. Point "
                          "this at a tenant whose contents you do not mind "
                          "growing — never at a demo or a real one."))
    ap.add_argument("--step-up-token", required=True,
                    help="write-capable step-up token for --tenant")
    ap.add_argument("--second-tenant", default="conformance-tenant-b",
                    help="a different tenant id, for the isolation probe")
    ap.add_argument("--mcp-url",
                    help="optional Streamable HTTP MCP endpoint for tools/call coverage")
    ap.add_argument(
        "--mcp-auth-token",
        default=os.environ.get("MCP_AUTH_TOKEN"),
        help=("optional MCP transport bearer token; defaults to the "
              "MCP_AUTH_TOKEN environment variable"),
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a scorecard")
    args = ap.parse_args()

    # Say it out loud when the operator overrides the default. A warning they
    # can read before the writes happen is worth more than a note in --help
    # they read after.
    if args.tenant != SELFTEST_TENANT:
        print(f"warning: grading '{args.tenant}' rather than the dedicated "
              f"'{SELFTEST_TENANT}'. This run will leave synthetic Patients "
              f"and Observations in '{args.tenant}' and will not remove "
              "them.", file=sys.stderr)

    ctx = ProbeContext(tenant=args.tenant, step_up_token=args.step_up_token,
                       second_tenant=args.second_tenant)
    mcp_client = (LiveMCPProbeClient(
        args.mcp_url,
        tenant=args.tenant,
        step_up_token=args.step_up_token,
        mcp_auth_token=args.mcp_auth_token,
    ) if args.mcp_url else None)
    report = run_conformance(
        LiveProbeClient(args.base_url), ctx, mcp_client=mcp_client)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
        p, t = report.score
        print(f"\n{'✅' if report.passed else '⚠️ '} Grade {report.grade} — "
              f"{p}/{t} guardrail properties verified.")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
