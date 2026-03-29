"""
Fasten Connect integration for HealthClaw Guardrails.

Provides patient-authorized FHIR R4 record ingestion via Fasten Connect
(standard EHR portal mode) and TEFCA IAS (identity-verified multi-provider).

Blueprint prefix: /fasten
"""
from r6.fasten.routes import fasten_blueprint  # noqa: F401
