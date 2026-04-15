"""
Wearables integration (v1.3.0).

Thin adapter that turns Open Wearables (the-momentum/open-wearables)
proprietary timeseries into FHIR R4 Observations + ingests them through
HealthClaw's existing guardrail stack.

This package owns:
- WearableConnection model — maps tenant -> Open Wearables user
- mapper — proprietary JSON -> FHIR Observation (LOINC + UCUM)
- client — HTTP client for the Open Wearables REST API
- poller — daemon thread that polls each connection and ingests deltas
- routes — Flask Blueprint for /wearables OAuth + status endpoints

Opt-in via the OPEN_WEARABLES_URL env var. When unset, nothing runs.
"""

from r6.wearables.models import WearableConnection
from r6.wearables.poller import start_poller, stop_poller

__all__ = ['WearableConnection', 'start_poller', 'stop_poller']
