"""prod-watch's completeness guard: did this run run what it says it runs?

`all N checks passing` was a completeness claim derived from whatever happened
to execute. A check that stopped running — moved under a condition that no
longer fires, lost in a merge, skipped by an early return — shrank N, and the
line still read as complete. The scheduled production run
(`.github/workflows/prod-watch.yml`) invokes the script directly and never
pytest, so the counts pinned elsewhere in this suite were no backstop for the
one run that watches production.

The expectation is now read out of the script's own `check(...)` call sites, so
adding a check adds its expectation in the same edit and there is no total for
two branches to disagree about. These tests pin that it is derived rather than
written down, that a declared name nobody decided fails the run under its own
exit code, and that the failure can be told apart from an outage.

No network: `prod_watch.get` and `prod_watch.post` are replaced wholesale, and
`requests.get`/`requests.post` are tripwired under them so a test that reaches
production fails loudly instead of passing on production's say-so (#433).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import prod_watch  # noqa: E402

TIP = "4f2a91cbeef1a9d3c05e7b21fd8460ac9e13d7f5"
# The name of a check that exists in the declaration and never runs. Standing
# in for the real thing — a call site that stopped executing — because the real
# thing cannot be produced without editing the file under test.
PHANTOM = "careagents: a check that stopped running"

# Captured before any test replaces it, so the phantom declarations below can
# build on the real reading instead of recursing into their own replacement.
_REAL_DECLARED = prod_watch._declared_checks


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_get(grade="A"):
    def get(url, timeout, **kw):
        if "$conformance" in url:
            return _Resp(200, {"grade": grade})
        if "Patient" in url:
            return _Resp(200, {"entry": [
                {"resource": {"resourceType": "Patient", "id": i}}
                for i in prod_watch.DEMO_PATIENTS]})
        if "Condition" in url:
            return _Resp(200, {"entry": [{"resource": {
                "resourceType": "Condition", "code": {"text": "Asthma"}}}]})
        if url.endswith("/healthz"):
            return _Resp(200, {"status": "ok", "accounts": True,
                               "build": "4f2a91cbeef1",
                               "built_at": 1754056800})
        if url.endswith("/auth"):
            return _Resp(200, text='<input maxlength="8">')
        if url.endswith("/mcp"):
            return _Resp(401)
        if url.endswith("/health"):
            return _Resp(200)
        return _Resp(200, text='<a href="/auth">start</a>')
    return get


def _fake_post(url, timeout, **kw):
    # The public demo's keyless `initialize` — the one POST in the script.
    return _Resp(200, {"jsonrpc": "2.0", "id": 1, "result": {}})


def _no_network(url, *a, **kw):
    # Not a RequestException on purpose: prod_watch.get/post swallow those into
    # a status string, which is how a real request hid here once before (#433).
    raise AssertionError(f"a test in this file reached the network: {url}")


_FRESH_BUILD_INFO = {"deployed": None, "built_at": None, "built": None,
                     "asserted": False, "ok": None}
_FRESH_COMPLETENESS = {"complete": False, "missing": [], "unreadable": []}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    prod_watch.results.clear()
    prod_watch.reported.clear()
    prod_watch.build_info.update(_FRESH_BUILD_INFO)
    prod_watch.completeness.update(_FRESH_COMPLETENESS)
    monkeypatch.setattr(prod_watch, "_human_to_stderr", False)
    monkeypatch.setattr(prod_watch, "get", _fake_get())
    monkeypatch.setattr(prod_watch, "post", _fake_post)
    monkeypatch.setattr(requests, "get", _no_network)
    monkeypatch.setattr(requests, "post", _no_network)
    yield
    prod_watch.results.clear()
    prod_watch.reported.clear()
    prod_watch.build_info.update(_FRESH_BUILD_INFO)
    prod_watch.completeness.update(_FRESH_COMPLETENESS)


def _declares_a_phantom(source=None):
    declared, unreadable = _REAL_DECLARED(source)
    return frozenset(declared | {PHANTOM}), unreadable


def _decided():
    return {n for n, _, _ in prod_watch.results} | set(prod_watch.reported)


# --- the declaration itself -------------------------------------------------

def test_every_check_name_in_the_script_can_be_read_statically():
    # The backstop for the backstop. A name assembled at run time — an
    # f-string, a variable — cannot be declared, so the guard would either miss
    # it or demand it forever; either way the scheduled run stops meaning what
    # it says. Catching that here is the difference between a red test and
    # every production run exiting 3.
    _, unreadable = prod_watch._declared_checks()
    assert unreadable == [], "\n".join(unreadable)


def test_a_module_constant_is_read_as_the_name_it_holds():
    declared, _ = prod_watch._declared_checks()
    assert prod_watch.BUILD_CHECK in declared
    assert "healthclaw: alive" in declared


def test_a_name_the_guard_cannot_read_is_reported_not_skipped():
    declared, unreadable = prod_watch._declared_checks(
        'def f(x):\n    check(f"careagents: {x}", True)\n')
    assert declared == frozenset()
    assert len(unreadable) == 1 and "line 2" in unreadable[0]


def test_a_name_that_only_ever_reports_is_never_demanded():
    # `report` asserts nothing, so a name with no `check` call site is not a
    # promise this run has to keep. Demanding it would make an honest
    # informational line into a missing check.
    declared, unreadable = prod_watch._declared_checks(
        'def f():\n    report("just a fact", "detail")\n')
    assert declared == frozenset() and unreadable == []


def test_adding_a_check_adds_its_expectation():
    # The property the whole design rests on: there is no second place to
    # update, so a branch that adds a check cannot forget the number, and two
    # branches that each add one cannot disagree about it.
    before, _ = prod_watch._declared_checks('def f():\n    check("a", True)\n')
    after, _ = prod_watch._declared_checks(
        'def f():\n    check("a", True)\n    check("b", True)\n')
    assert before == {"a"} and after == {"a", "b"}


# --- what a run does with it ------------------------------------------------

def test_a_healthy_run_decides_every_name_it_declares():
    assert prod_watch.run(1.0, [TIP]) == 0
    declared, unreadable = prod_watch._declared_checks()
    assert unreadable == []
    assert declared == _decided(), (
        f"declared but never decided: {sorted(declared - _decided())}")


def test_an_unasserted_check_counts_as_decided_not_as_missing(capsys):
    # Informational mode: the build check reports instead of asserting (#272).
    # That is a decision, not a silence, so the run stays complete — and the
    # summary says so rather than quietly counting one check fewer.
    assert prod_watch.run(1.0, []) == 0
    assert prod_watch.BUILD_CHECK in prod_watch.reported
    assert prod_watch.BUILD_CHECK not in {n for n, _, _ in prod_watch.results}
    out = capsys.readouterr().out
    assert "1 reported without assertion" in out
    assert "all accounted for" in out


def test_a_declared_check_that_never_runs_fails_the_run(capsys, monkeypatch):
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    code = prod_watch.run(1.0, [TIP])
    out = capsys.readouterr().out
    assert code == 3, "a vanished check must fail the run, not shrink the count"
    assert "1 declared check(s) never ran" in out and PHANTOM in out


def test_an_incomplete_run_never_reports_itself_as_complete(capsys,
                                                            monkeypatch):
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    prod_watch.run(1.0, [TIP])
    out = capsys.readouterr().out
    # The exact line the guard exists for. Every check that ran did pass, so
    # the old summary printed and read as a verdict on production.
    assert "checks passing" not in out
    assert "not a verdict on production" in out


def test_an_incomplete_run_is_not_reported_as_an_outage(monkeypatch):
    # Exit 3, not 1: nothing here says production is down, and filing this as
    # an outage sends whoever reads it at 03:00 to the wrong dashboard.
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    assert prod_watch.run(1.0, [TIP]) == 3


def test_an_incomplete_run_outranks_a_stale_build(monkeypatch):
    # A build verdict from a run that skipped checks is not worth acting on.
    # The stale alarm still fires from the JSON, which carries build.asserted.
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    assert prod_watch.run(1.0, ["0" * 40]) == 3
    assert prod_watch.build_info["asserted"] is True
    assert prod_watch.build_info["ok"] is False


def test_an_outage_outranks_an_incomplete_run(monkeypatch):
    # A real outage is the more urgent of the two, and the third alarm reads
    # the JSON rather than the exit code, so it still fires alongside.
    monkeypatch.setattr(prod_watch, "get", _fake_get(grade="B"))
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    assert prod_watch.run(1.0, [TIP]) == 1
    assert prod_watch.completeness["complete"] is False


def test_a_run_decides_only_for_itself():
    # The guard reads "what this run decided". A name left in the registers by
    # an earlier run would answer for a check this one skipped — the guard
    # fooled by the same stale module state the build verdict is reset for.
    prod_watch.results.append(("a check from an earlier run", True, ""))
    prod_watch.reported.append("a name from an earlier run")
    assert prod_watch.run(1.0, [TIP]) == 0
    assert "a check from an earlier run" not in _decided()
    assert "a name from an earlier run" not in _decided()


def test_a_source_it_cannot_read_fails_the_run(capsys, monkeypatch):
    def boom(source=None):
        raise OSError("no such file")
    monkeypatch.setattr(prod_watch, "_declared_checks", boom)
    code = prod_watch.run(1.0, [TIP])
    out = capsys.readouterr().out
    # Not a traceback: that exits 1 and files the outage issue about a script
    # that never reached production.
    assert code == 3
    assert "cannot say what it should have decided" in out


# --- what the scheduled job reads -------------------------------------------

def _payload(monkeypatch, tmp_path, argv):
    out = tmp_path / "status.json"
    monkeypatch.setattr(sys, "argv",
                        ["prod_watch.py", "--json-out", str(out)] + argv)
    code = prod_watch.main()
    return code, json.loads(out.read_text())


def test_the_payload_names_the_checks_that_vanished(monkeypatch, tmp_path):
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    code, payload = _payload(monkeypatch, tmp_path, ["--expect-sha", TIP])
    assert code == 3
    assert payload["complete"] is False
    # The alarm has to name what is wrong, not only that something is.
    assert payload["missing"] == [PHANTOM]
    assert payload["unreadable"] == []


def test_an_incomplete_run_cannot_close_the_outage_alarm(monkeypatch,
                                                         tmp_path):
    # `hard_ok` is what closes the outage issue, and closing needs a positive
    # observation. A run that skipped a check has not observed production.
    monkeypatch.setattr(prod_watch, "_declared_checks", _declares_a_phantom)
    _, payload = _payload(monkeypatch, tmp_path, ["--expect-sha", TIP])
    assert payload["hard_ok"] is False and payload["ok"] is False


def test_a_complete_run_still_closes_it(monkeypatch, tmp_path):
    code, payload = _payload(monkeypatch, tmp_path, ["--expect-sha", TIP])
    assert code == 0
    assert payload["complete"] is True and payload["hard_ok"] is True
    assert payload["missing"] == [] and payload["reported"] == []


def test_a_stale_build_alone_still_closes_the_outage_alarm(monkeypatch,
                                                           tmp_path):
    # The property the two-alarm split exists for, re-pinned because `hard_ok`
    # gained a second term: a healthy deployment on a stale build must not pin
    # the outage issue open.
    code, payload = _payload(monkeypatch, tmp_path, ["--expect-sha", "0" * 40])
    assert code == 2
    assert payload["complete"] is True and payload["hard_ok"] is True
