"""The rate-limit bucket key must identify a client, or admit that it cannot.

Measured in production on 2026-08-06: every untenanted request reaching the
Vercel deployment shared ONE bucket, so 120 requests/minute from anywhere on
the internet throttled everyone, including the liveness probe. The cause is
that `_client_ip` took the rightmost X-Forwarded-For hop, which is the real
peer behind a ONE-hop proxy (Railway) and the platform's own internal address
behind a TWO-hop one (Vercel). The same code serves both and nothing in it
could tell which it was running on.

That is this repo's defect species aimed at its own availability: the key
resolver produced a value that meant "I could not identify the client", and
the limiter read it as "here is the client".
"""

import pytest
from flask import Flask

import r6.rate_limit as rate_limit


@pytest.fixture
def app():
    return Flask(__name__)


def _key(app, monkeypatch, headers, hops=None, remote="10.0.0.9"):
    if hops is not None:
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", str(hops))
    else:
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    with app.test_request_context("/r6/fhir/Patient", headers=headers,
                                  environ_base={"REMOTE_ADDR": remote}):
        return rate_limit.rate_limit_key()


def test_one_hop_proxy_keys_on_the_peer_the_proxy_saw(app, monkeypatch):
    """Railway: client value is spoofable, the appended rightmost hop is not."""
    key = _key(app, monkeypatch,
               {"X-Forwarded-For": "1.2.3.4, 203.0.113.7"}, hops=1)
    assert key == "ip:203.0.113.7"


def test_two_hop_proxy_skips_the_platforms_own_address(app, monkeypatch):
    """Vercel: edge appends the client, a second internal hop appends itself.

    MUTATION: revert to hops[-1] -> this returns the constant internal
    address and every caller lands in one bucket.
    """
    key = _key(app, monkeypatch,
               {"X-Forwarded-For": "1.2.3.4, 203.0.113.7, 10.11.12.13"},
               hops=2)
    assert key == "ip:203.0.113.7"


def test_two_callers_behind_one_platform_do_not_share_a_bucket(app, monkeypatch):
    """The property the outage violated, stated directly."""
    first = _key(app, monkeypatch,
                 {"X-Forwarded-For": "x, 198.51.100.1, 10.11.12.13"}, hops=2)
    second = _key(app, monkeypatch,
                  {"X-Forwarded-For": "x, 198.51.100.2, 10.11.12.13"}, hops=2)
    assert first != second


def test_a_chain_too_short_to_trust_is_unidentified_not_guessed(app, monkeypatch):
    """Fewer hops than configured means the proxy chain is not what we think.

    Returning the leftmost value here would hand the caller its own bucket
    key, which is exactly the opt-out limiter #339 removed. The honest answer
    is a distinct bucket that says so.
    """
    key = _key(app, monkeypatch, {"X-Forwarded-For": "1.2.3.4"}, hops=2)
    assert key == "ip:unidentified"


def test_no_forwarding_header_falls_back_to_the_socket_peer(app, monkeypatch):
    key = _key(app, monkeypatch, {}, hops=1, remote="192.0.2.55")
    assert key == "ip:192.0.2.55"


def test_the_default_is_the_single_proxy_topology(app, monkeypatch):
    """Unset config must not change behaviour for existing deployments."""
    key = _key(app, monkeypatch, {"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
    assert key == "ip:203.0.113.7"


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_an_unusable_hop_setting_falls_back_to_one_rather_than_crashing(
        app, monkeypatch, bad):
    key = _key(app, monkeypatch,
               {"X-Forwarded-For": "1.2.3.4, 203.0.113.7"}, hops=bad)
    assert key == "ip:203.0.113.7"


def test_liveness_is_never_throttled(app, monkeypatch):
    """A monitor that reads "busy" as "down" is the defect, not the report.

    prod_watch calls /health to ask whether the process is alive. Throttling
    it turns load into a false outage report — and during the 08-06 incident
    it did exactly that.
    """
    blueprint = Flask(__name__)
    rate_limit.rate_limit_middleware(blueprint)
    calls = []
    monkeypatch.setattr(rate_limit, "check_rate_limit",
                        lambda *a, **k: calls.append(a) or (False, 0, 0))

    for path in ("/r6/fhir/health", "/r6/fhir/metadata"):
        with blueprint.test_request_context(path):
            for func in blueprint.before_request_funcs[None]:
                assert func() is None, f"{path} must not be throttled"
    assert calls == []
