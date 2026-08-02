"""prod-watch's build-provenance check (#258).

Every other check in scripts/prod_watch.py is satisfied just as well by a
months-old build — which is how both CareAgents deployments ran code older
than PR #241 while the monitor reported 9/9 green. This pins the check that
closes that gap, and the exit-code split that keeps a stale build from being
reported as an outage.

No network: `prod_watch.get` is replaced wholesale.
"""

from __future__ import annotations

import json
import sys
import tempfile
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


_FRESH_BUILD_INFO = {"deployed": None, "built_at": None, "built": None,
                     "asserted": False, "ok": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # `build_info` is a module global that `run()` does not reset, so without
    # this an asserted run leaks `asserted=True` into every later test in the
    # process — see the strict-xfail below, which pins that as a defect rather
    # than papering over it here.
    prod_watch.results.clear()
    prod_watch.build_info.update(_FRESH_BUILD_INFO)
    monkeypatch.setattr(prod_watch, "get", _fake_get())
    yield
    prod_watch.results.clear()
    prod_watch.build_info.update(_FRESH_BUILD_INFO)


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


# --- the machine-readable verdict the workflow's two alarms are driven by ----
#
# .github/workflows/prod-watch.yml stopped inferring both alarms from the exit
# code and now reads `build.asserted` / `build.ok` out of --json-out. Nothing
# pinned that: every field below could be renamed, inverted, or dropped and the
# suite stayed green while the stale-build alarm silently never fired again.
# A monitor whose alarm wiring is untested is the shape of #258 itself.

def _payload(argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prod_watch.py", *argv])
    code = prod_watch.main()
    capsys.readouterr()
    return code


@pytest.mark.parametrize("argv,asserted,ok,exit_code", [
    ([], False, None, 0),                          # informational
    (["--expect-sha", TIP], True, True, 0),        # observed pass
    (["--expect-sha", "0" * 40], True, False, 2),  # observed stale
])
def test_the_json_payload_carries_the_verdict_the_alarms_read(
        argv, asserted, ok, exit_code, monkeypatch, capsys, tmp_path):
    out = tmp_path / "status.json"
    code = _payload([*argv, "--json-out", str(out)], monkeypatch, capsys)
    assert code == exit_code
    status = json.loads(out.read_text())
    # Field names, not just values: the workflow reads these by name and a
    # rename would disable the alarm without failing anything.
    assert status["build"]["asserted"] is asserted
    assert status["build"]["ok"] is ok
    assert status["ok"] is (exit_code == 0)
    # Three states, not two. `asserted is False` must be distinguishable from
    # `ok is False`, or informational mode closes a live stale-build alarm.
    assert not (asserted is False and status["build"]["ok"] is False)


def test_the_reported_build_is_the_one_the_deployment_actually_named(
        monkeypatch, capsys, tmp_path):
    out = tmp_path / "status.json"
    _payload(["--json-out", str(out)], monkeypatch, capsys)
    build = json.loads(out.read_text())["build"]
    assert build["deployed"] == "4f2a91cbeef1"
    assert build["built_at"] == 1754056800
    assert build["built"] == "2025-08-01T14:00Z"


def test_json_and_json_out_cannot_disagree(monkeypatch, capsys, tmp_path):
    # Two readers of the same run must not be told different things: one reads
    # stdout and the alarm reads the file.
    out = tmp_path / "status.json"
    monkeypatch.setattr(sys, "argv", ["prod_watch.py", "--json",
                                      "--json-out", str(out),
                                      "--expect-sha", "0" * 40])
    assert prod_watch.main() == 2
    printed = capsys.readouterr().out
    assert json.loads(printed[printed.index("{"):]) == \
        json.loads(out.read_text())


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT (pre-existing, and the one F5 leaned on): --json is documented as "
    "'machine-readable' and --json-out as the flag that leaves 'stdout "
    "human-readable', but --json prints the JSON *after* the ANSI-coloured "
    "human lines on the same stream, so the documented machine-readable mode "
    "cannot be parsed. Repro: "
    "python scripts/prod_watch.py --json | python -m json.tool"))
def test_the_documented_machine_readable_mode_is_machine_readable(monkeypatch,
                                                                  capsys):
    monkeypatch.setattr(sys, "argv", ["prod_watch.py", "--json"])
    prod_watch.main()
    json.loads(capsys.readouterr().out)


def test_a_refused_run_writes_no_status_file_at_all(monkeypatch, capsys,
                                                    tmp_path):
    # The F2 refusal happens before run(), so no file is written. Pinned
    # deliberately: the dangerous regression is writing a *default* payload
    # here, which would hand the alarm step `asserted: false` and let it treat
    # a refused run as a run that simply pinned nothing.
    out = tmp_path / "status.json"
    assert _payload(["--expect-sha", "", "--json-out", str(out)],
                    monkeypatch, capsys) == 1
    assert not out.exists(), "a refused run must not leave a verdict behind"


def test_an_earlier_assertion_does_not_leak_into_a_later_informational_run():
    prod_watch.run(1.0, [TIP])
    prod_watch.results.clear()
    prod_watch.run(1.0, [])
    assert prod_watch.build_info["asserted"] is False, (
        "informational mode asserts nothing and must say so")
    assert prod_watch.build_info["ok"] is None


# --- the wiring between the script and the alarms ---------------------------

WORKFLOW = (Path(__file__).parent.parent / ".github" / "workflows"
            / "prod-watch.yml").read_text()


def test_the_scheduled_run_still_writes_the_file_its_alarms_read():
    # Drop `--json-out` from the workflow and the stale-build alarm goes silent
    # forever: status.json is absent, `status?.build?.asserted` is undefined,
    # and the alarm neither fires nor closes. Nothing else would notice.
    assert "--json-out status.json" in WORKFLOW
    assert "readFileSync('status.json'" in WORKFLOW


def _payload_for(code: int) -> dict:
    """The JSON payload main() would emit for a given run outcome."""
    import argparse
    import json as _json
    ns = argparse.Namespace(timeout=1.0, json=False, json_out=None,
                            expect_sha=["a" * 40])
    real_run, real_parse = prod_watch.run, argparse.ArgumentParser.parse_args
    written = {}
    try:
        prod_watch.run = lambda *a, **k: code
        argparse.ArgumentParser.parse_args = lambda self, *a, **k: ns
        ns.json_out = str(tmp := Path(tempfile.mkdtemp()) / "s.json")
        prod_watch.main()
        written = _json.loads(Path(tmp).read_text())
    finally:
        prod_watch.run, argparse.ArgumentParser.parse_args = real_run, real_parse
    return written


def test_the_outage_alarm_is_never_closed_by_an_exit_code_alone():
    # Asserted positively, not as "the old wrong predicate is absent". The
    # negative form passed for `status !== null` and for `status?.ok !== false`
    # — the second of which is TRUE when status is null, so it closes the alarm
    # on a missing verdict file and puts the whole defect straight back. What
    # matters is that closing requires a positive observation, so pin the
    # observation.
    # `hard_ok`, not `ok`: closing on `ok` was N9 — a healthy deployment on a
    # stale build exits 2, so `ok` is false with nothing this alarm speaks for
    # broken, and the outage issue could never close again. Both properties
    # matter, so pin both: a positive observation, and the right one.
    assert "const allPassed = status?.hard_ok === true;" in WORKFLOW
    assert "hardFailing, allPassed," in WORKFLOW


def test_the_payload_separates_a_hard_failure_from_any_failure():
    # The distinction the outage alarm rests on. If `hard_ok` ever collapses
    # back into `ok`, N9 returns silently: the alarm keeps closing on the wrong
    # question and nothing else in the suite notices.
    healthy_stale = _payload_for(code=2)
    assert healthy_stale["ok"] is False and healthy_stale["hard_ok"] is True
    outage = _payload_for(code=1)
    assert outage["ok"] is False and outage["hard_ok"] is False
    green = _payload_for(code=0)
    assert green["ok"] is True and green["hard_ok"] is True


def test_a_stale_build_still_alarms_when_the_verdict_file_is_unusable():
    # The exit code is an INDEPENDENT trigger, deliberately. Every JSON-only
    # predicate goes permanently silent under schema drift — rename `asserted`
    # and the alarm never fires again with nothing red anywhere — and silence
    # is the one failure mode this alarm cannot tolerate. Narrowing it to
    # `exit === '2' && status === null` does not help: a drifted file parses
    # fine, so the null guard never fires.
    assert "const staleFiring = exit === '2'" in WORKFLOW
    assert "|| (status?.build?.asserted === true" in WORKFLOW


@pytest.mark.parametrize("ts", [0, -1, -1754056800, "-1", "", None, "abc",
                                1e30, 10 ** 20])
def test_no_timestamp_a_build_cannot_have_had_is_ever_rendered(ts):
    # N8: `if not ts` let a negative through, so an unstamped-then-mangled
    # marker printed "built 1969-12-31T23:59Z" — F4's defect one value over.
    # _build passes int() through unbounded, so the monitor is the last gate.
    assert prod_watch._stamp(ts) == ""


def test_a_real_timestamp_still_renders():
    # The other half: the guard must not swallow the value it exists to show.
    assert prod_watch._stamp(1754056800) == "2025-08-01T14:00Z"
    assert prod_watch._stamp("1754056800") == "2025-08-01T14:00Z"


def test_a_healthy_deployment_can_always_close_its_outage_alarm(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(prod_watch, "get", _fake_get(build="0bad0bad0bad"))
    out = tmp_path / "status.json"
    assert _payload(["--expect-sha", TIP, "--json-out", str(out)],
                    monkeypatch, capsys) == 2
    status = json.loads(out.read_text())
    hard = [c["name"] for c in status["checks"]
            if not c["ok"] and c["name"] != prod_watch.BUILD_CHECK]
    assert hard == [], "nothing but the build check failed"
    assert status["hard_ok"] is True, (
        "a run with no hard failure must say so, or the outage alarm it "
        "closes on can never close while the build is stale")
