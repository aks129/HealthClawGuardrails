"""Security probe for FHI-60: an httpx error message carries the request URL.

GET /wearables/oauth/start takes `tenant_id` from the query string, unchecked
and unauthenticated, and sends it upstream as `user_id=hc-<tenant_id>`. When
Open Wearables answers non-2xx (its documented default for this endpoint is
401), the client logs str(exc), which names the full URL, so whatever the
caller sent is written to the log. Synthetic values only.
"""

from __future__ import annotations

import logging
import secrets

import httpx

from r6.wearables import client as wearables_client


def test_a_token_as_tenant_on_wearables_start_never_reaches_the_log(
        client, caplog, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("OPEN_WEARABLES_URL", "https://ow.invalid")
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    monkeypatch.setattr(
        wearables_client.WearablesClient, "_client",
        lambda self: httpx.Client(base_url=self.base_url,
                                  transport=transport))
    with caplog.at_level(logging.DEBUG):
        client.get("/wearables/oauth/start",
                   query_string={"provider": "garmin", "tenant_id": token})
    assert token not in "\n".join(r.getMessage() for r in caplog.records)
