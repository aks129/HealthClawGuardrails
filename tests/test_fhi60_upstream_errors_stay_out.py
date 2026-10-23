"""Upstream and CareAgents database errors keep caller values out of the log
(FHI-60).

An httpx error names the full request URL, query string included, and the
wearables OAuth kickoff sends `user_id=hc-<tenant_id>` upstream from an
unauthenticated query parameter. The client now logs the error class and HTTP
status only, and the route refuses anything that is not a tenant id.
CareAgents' engine hides bound parameters like the engine's does.
Synthetic values only.
"""

from __future__ import annotations

import logging
import secrets

import httpx
import pytest
from sqlalchemy import text

from r6.wearables import client as wearables_client


def _logged(caplog):
    out = []
    for record in caplog.records:
        out.append(record.getMessage())
        if record.exc_info:
            out.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(out)


def _client_lines(caplog):
    """The client's own lines. httpx's request line names the configured
    upstream host, which is ours, not the caller's; only its query string
    is caller-chosen, and the filter drops that."""
    return "\n".join(r.getMessage() for r in caplog.records
                     if r.name == "r6.wearables.client")


@pytest.fixture
def upstream(monkeypatch):
    """Open Wearables answering every request with the given status."""
    def install(status):
        monkeypatch.setenv("OPEN_WEARABLES_URL", "https://ow.invalid")
        transport = httpx.MockTransport(lambda request: httpx.Response(status))
        monkeypatch.setattr(
            wearables_client.WearablesClient, "_client",
            lambda self: httpx.Client(base_url=self.base_url,
                                      transport=transport))
    return install


def test_a_failed_authorize_logs_the_class_and_status_only(
        app, upstream, caplog):
    """`app` imports main, which installs the filter on httpx's own INFO
    request line: that line names the URL too."""
    upstream(401)
    marker = "hc-" + secrets.token_urlsafe(16)
    with caplog.at_level(logging.DEBUG):
        url = wearables_client.WearablesClient().oauth_authorize_url(
            "garmin", ow_user_id=marker)

    assert url is None
    logged = _logged(caplog)
    assert marker not in logged
    assert "ow.invalid" not in _client_lines(caplog)
    assert "HTTPStatusError status=401" in logged


def test_a_failed_provider_list_logs_the_class_and_status_only(
        upstream, caplog):
    upstream(503)
    with caplog.at_level(logging.DEBUG):
        wearables_client.WearablesClient().list_providers()

    logged = _logged(caplog)
    assert "ow.invalid" not in _client_lines(caplog)
    assert "HTTPStatusError status=503" in logged


@pytest.mark.parametrize("tenant", [
    "bad tenant", "../../etc/passwd", "a" * 65, "x.y", "tenant\n"])
def test_oauth_start_refuses_a_malformed_tenant_with_a_fixed_400(
        client, upstream, tenant):
    upstream(401)
    r = client.get("/wearables/oauth/start",
                   query_string={"provider": "garmin", "tenant_id": tenant})
    assert r.status_code == 400
    assert r.get_json() == {"error": "invalid tenant_id"}


def test_oauth_start_still_reaches_upstream_for_a_valid_tenant(
        client, upstream):
    """The format check must not refuse the ids it exists to let through."""
    upstream(401)
    r = client.get("/wearables/oauth/start",
                   query_string={"provider": "garmin",
                                 "tenant_id": "desktop-demo"})
    assert r.status_code == 503   # upstream refused: not configured here


# --- CareAgents' engine -----------------------------------------------------

def test_the_careagents_engine_hides_bound_parameters(tmp_path):
    from careagents.models import make_engine
    engine = make_engine(f"sqlite:///{tmp_path}/careagents.db")
    assert engine.hide_parameters is True

    email = f"{secrets.token_hex(6)}@example.test"
    with engine.connect() as conn, pytest.raises(Exception) as caught:
        conn.execute(text("SELECT 1 FROM no_such_table WHERE email = :e"),
                     {"e": email})
    assert email not in str(caught.value)
