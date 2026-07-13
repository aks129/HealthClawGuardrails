"""Guardrail conformance scorecard for any HealthClaw deployment.

Proves the seven guardrail properties actually hold — including error fidelity
on rejected requests — by probing a live endpoint with synthetic data. Partners
can run this against their own deployment (or ours) to verify the guardrails are
real, not marketing.

Usage:
    python scripts/guardrail_conformance.py \
        --base-url https://app.healthclaw.io \
        --tenant desktop-demo \
        --step-up-token <token>          # mint via POST /r6/fhir/internal/step-up-token
    # For protected MCP coverage, also set MCP_AUTH_TOKEN and pass --mcp-url.
    # add --json for machine-readable output; exits non-zero if grade < A.

The probes create SYNTHETIC data (obviously-fake PHI); a live run never touches
real patient records.
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


def main():
    ap = argparse.ArgumentParser(description="HealthClaw guardrail conformance scorecard")
    ap.add_argument("--base-url", required=True, help="e.g. https://app.healthclaw.io")
    ap.add_argument("--tenant", required=True,
                    help="tenant id (a public/synthetic tenant is fine)")
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
