"""Security probe for FHI-60: loggers other than the request log.

The request log now hashes the id headers, but other r6 loggers interpolate
the VALIDATED tenant id. `secrets.token_urlsafe` values pass the tenant
pattern, so a token pasted into X-Tenant-Id reaches those lines. Synthetic
values only.
"""

from __future__ import annotations

import logging
import secrets

from r6.stepup import generate_step_up_token


def _logged(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


def test_a_token_as_tenant_on_a_refused_write_never_reaches_the_log(
        client, caplog):
    """r6/access.py:487 logs `step-up refused for tenant %s` at INFO."""
    token = secrets.token_urlsafe(32)
    with caplog.at_level(logging.DEBUG):
        client.post("/r6/fhir/Patient", json={"resourceType": "Patient"},
                    headers={"X-Tenant-Id": token,
                             "X-Step-Up-Token":
                                 generate_step_up_token("someone-else")})
    assert token not in _logged(caplog)


def test_a_token_as_tenant_on_connect_diagnostic_never_reaches_the_log(
        client, caplog):
    """r6/routes.py:1919 logs `tenant=%s` at WARNING, unauthenticated."""
    token = secrets.token_urlsafe(32)
    with caplog.at_level(logging.DEBUG):
        client.post("/r6/fhir/internal/connect-diagnostic",
                    json={"payload": {"code": "config_error"}},
                    headers={"X-Tenant-Id": token})
    assert token not in _logged(caplog)

