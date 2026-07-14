"""CareAgents — the hosted consumer experience layer for HealthClaw.

A small Flask app (careagents.cloud) that lets anyone spin up a personal
health agent in under a minute. It runs a server-side agent loop whose ONLY
data path is the HealthClaw guardrail layer's HTTP API — redaction, audit,
step-up, tenant isolation, and the forms rail are inherited, never
reimplemented. See docs/superpowers/specs/2026-07-14-careagents-hosted-
experience-design.md.
"""

from careagents.app import create_app  # noqa: F401
