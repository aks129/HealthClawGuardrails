"""prod-watch's build-provenance check (#258).

Every other check in scripts/prod_watch.py is satisfied just as well by a
months-old build — which is how both CareAgents deployments ran code older
than PR #241 while the monitor reported 9/9 green. This pins the check that
closes that gap, and the exit-code split that keeps a stale build from being
reported as an outage.

No network: `prod_watch.get` is replaced wholesale.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import prod_watch  # noqa: E402

TIP = "4f2a91cbeef1a9d3c05e7b21fd8460ac9e13d7f5"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_get(build="4f2a91cbeef1", built_at=1754056800, grade="A"):
    def get(url, timeout, **kw):
        if url.endswith("/r6/fhir/health"):
            return _Resp(200)
        if "$conformance" in url:
            return _Resp(200, {"grade": grade})
        if "Condition" in url:
            return _Resp(200, {"entry": [{"resource": {
                "resourceType": "Condition", "code": {"text": "Asthma"}}}]})
        if url.endswith("/healthz"):
            return _Resp(200, {"status": "ok", "provider": "openai",
                               "accounts": True, "build": build,
                               "built_at": built_at})
        if url.endswith("/auth"):
            return _Resp(200, text='<input maxlength="8">')
        if url.endswith("/mcp"):
            return _Resp(401)
        if url.endswith("/health"):
            return _Resp(200)
        return _Resp(200, text='<a href="/auth">start</a>')
    return get


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    prod_watch.results.clear()
    monkeypatch.setattr(prod_watch, "get", _fake_get())
    yield
    prod_watch.results.clear()


def _named(name):
    return [(n, ok, d) for n, ok, d in prod_watch.results if n == name]


def test_a_current_build_passes():
    assert prod_watch.run(1.0, [TIP]) == 0
    assert _named(prod_watch.BUILD_CHECK)[0][1] is True


def test_a_stale_build_exits_2_and_names_the_remedy():
    # Exit 2, not 1: production is up. Whoever reads this at 03:00 should not
    # have to go looking for what to do about it.
    assert prod_watch.run(1.0, ["0" * 40]) == 2
    (_, ok, detail), = _named(prod_watch.BUILD_CHECK)
    assert ok is False
    assert "4f2a91cbeef1" in detail
    assert "does not auto-deploy" in detail and "RELEASING.md" in detail
    assert "0000000" in detail, "the tip belongs in the message"


def test_an_outage_outranks_a_stale_build(monkeypatch):
    # A stale-build alarm must never be what a real outage is reported as.
    monkeypatch.setattr(prod_watch, "get", _fake_get(grade="B"))
    assert prod_watch.run(1.0, ["0" * 40]) == 1


def test_a_dirty_build_is_never_accepted(monkeypatch):
    # The sha prefix matches, but the tree it was built from did not.
    monkeypatch.setattr(prod_watch, "get",
                        _fake_get(build="4f2a91cbeef1-dirty"))
    assert prod_watch.run(1.0, [TIP]) == 2


def test_an_unmarked_build_is_never_accepted(monkeypatch):
    monkeypatch.setattr(prod_watch, "get",
                        _fake_get(build="unknown", built_at=0))
    assert prod_watch.run(1.0, [TIP]) == 2


def test_without_an_expected_sha_the_build_is_reported_not_asserted(capsys):
    # The script's honesty property: it runs unauthenticated from a laptop,
    # where there is no expected set, and it must not manufacture a verdict.
    assert prod_watch.run(1.0, []) == 0
    assert _named(prod_watch.BUILD_CHECK) == [], "must not count as a check"
    out = capsys.readouterr().out
    assert "4f2a91cbeef1" in out and prod_watch.BUILD_CHECK in out
