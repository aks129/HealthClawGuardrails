"""HealthClaw guardrail conformance harness.

Verify the six guardrail properties against any deployment:
    from r6.conformance import LiveProbeClient, ProbeContext, run_conformance
    report = run_conformance(LiveProbeClient(base_url), ProbeContext(tenant, token))
    print(report.render())
"""

from r6.conformance.probes import (  # noqa: F401
    Check,
    ConformanceReport,
    FlaskProbeClient,
    LiveProbeClient,
    PROPERTIES,
    ProbeContext,
    ProbeResult,
    _grade,
    run_conformance,
)
