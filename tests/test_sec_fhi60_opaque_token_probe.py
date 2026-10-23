"""Security probe for FHI-60: opaque credentials in the id headers.

The engine's own OAuth access and refresh tokens and client secrets are
`secrets.token_urlsafe(n)` values (r6/oauth.py): base64url, no prefix, no
`eyJ`, not hex. A caller that pastes one into X-Tenant-Id or X-Agent-Id is
the same mistake FHI-60 reported for a step-up token. Synthetic values only.
"""

from __future__ import annotations

import logging
import secrets

import pytest


def _logged(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("nbytes,header", [
    (48, "X-Tenant-Id"),   # access/refresh token shape: 64 chars, a valid tenant id
    (32, "X-Tenant-Id"),   # client secret shape
    (48, "X-Agent-Id"),
    (32, "X-Request-Id"),
])
def test_an_opaque_oauth_token_in_an_id_header_never_reaches_the_log(
        client, caplog, tenant_id, nbytes, header):
    token = secrets.token_urlsafe(nbytes)
    headers = {"X-Tenant-Id": tenant_id, header: token}
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient", headers=headers)
    assert token not in _logged(caplog)


def test_a_128_bit_hex_secret_in_the_agent_header_never_reaches_the_log(
        client, caplog, tenant_id):
    token = secrets.token_hex(16)  # 32 hex, under the 40-char rule
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": tenant_id, "X-Agent-Id": token})
    assert token not in _logged(caplog)
