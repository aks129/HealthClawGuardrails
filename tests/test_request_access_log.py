"""Request access-log level discrimination (#375).

`log_request` looks like an audit trail and is a debug access log. The real
audit trail is AuditEvent, which is durable and PHI-free. Logging every
request at INFO made retention a line budget rather than a time budget: the
idle run-claim poll alone was ~100% of the engine's log volume, which evicted
the lines an operator actually needs (the Fasten reaper's zombie-job warnings,
ingest and webhook errors) inside minutes.

So: successes go to DEBUG, anything that did not succeed stays at INFO, and
request_id correlation survives both.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def probe_client(app):
    """Client with routes pinned to the statuses under test.

    204 is not an arbitrary success — it is exactly what the run-claim poll
    returns 620k times a day.
    """

    @app.route("/__probe/claim")
    def _probe_claim():
        return "", 204

    @app.route("/__probe/ok")
    def _probe_ok():
        return "fine", 200

    @app.route("/__probe/boom")
    def _probe_boom():
        return "", 500

    @app.route("/__probe/moved")
    def _probe_moved():
        return "", 302, {"Location": "/__probe/ok"}

    return app.test_client()


def _lines(caplog):
    """Access-log records as {path: record}."""
    import json

    found = {}
    for record in caplog.records:
        if record.name != "request":
            continue
        entry = json.loads(record.getMessage())
        found[entry["path"]] = (record.levelno, entry)
    return found


def test_successful_requests_log_at_debug_and_failures_at_info(
        probe_client, caplog):
    with caplog.at_level(logging.DEBUG, logger="request"):
        probe_client.get("/__probe/claim")
        probe_client.get("/__probe/ok")
        probe_client.get("/__probe/boom")
        probe_client.get("/__probe/no-such-route")

    lines = _lines(caplog)

    assert lines["/__probe/claim"][0] == logging.DEBUG
    assert lines["/__probe/ok"][0] == logging.DEBUG
    assert lines["/__probe/boom"][0] == logging.INFO
    assert lines["/__probe/no-such-route"][0] == logging.INFO


def test_default_level_drops_the_success_lines_and_keeps_the_failures(
        probe_client, caplog):
    """The volume claim, measured the way production sees it.

    Production runs the root logger at INFO, so this is the level at which
    the ~11x/620k-line reduction is actually realised. A change that only
    reworded the line would fail here.
    """
    with caplog.at_level(logging.INFO, logger="request"):
        for _ in range(10):
            probe_client.get("/__probe/claim")
        probe_client.get("/__probe/boom")

    paths = [record.getMessage() for record in caplog.records
             if record.name == "request"]

    assert len(paths) == 1
    assert "/__probe/boom" in paths[0]


def test_request_id_correlation_survives_at_both_levels(
        probe_client, caplog):
    with caplog.at_level(logging.DEBUG, logger="request"):
        ok = probe_client.get("/__probe/claim",
                              headers={"X-Request-Id": "corr-ok"})
        bad = probe_client.get("/__probe/boom",
                               headers={"X-Request-Id": "corr-bad"})

    assert ok.headers["X-Request-Id"] == "corr-ok"
    assert bad.headers["X-Request-Id"] == "corr-bad"

    lines = _lines(caplog)
    assert lines["/__probe/claim"][1]["request_id"] == "corr-ok"
    assert lines["/__probe/boom"][1]["request_id"] == "corr-bad"


def test_redirects_stay_visible_at_info(probe_client, caplog):
    # Only 2xx is "it worked". A 302 is a routing surprise worth seeing, and
    # the loop that a misconfigured redirect causes is invisible at DEBUG.
    with caplog.at_level(logging.DEBUG, logger="request"):
        moved = probe_client.get("/__probe/moved")

    assert moved.status_code == 302
    lines = _lines(caplog)
    assert lines["/__probe/moved"][0] == logging.INFO
