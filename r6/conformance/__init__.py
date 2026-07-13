"""HealthClaw guardrail conformance harness.

Verify the seven guardrail properties against any deployment:
    from r6.conformance import LiveProbeClient, ProbeContext, run_conformance
    report = run_conformance(LiveProbeClient(base_url), ProbeContext(tenant, token))
    print(report.render())
"""

from r6.conformance.probes import (  # noqa: F401
    Check,
    ConformanceReport,
    FlaskProbeClient,
    LiveMCPProbeClient,
    LiveProbeClient,
    PROPERTIES,
    ProbeContext,
    ProbeResult,
    _error_fidelity_grade,
    _grade,
    run_conformance,
)
