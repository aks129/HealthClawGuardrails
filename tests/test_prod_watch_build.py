"""prod-watch's build-provenance check (#258).

Every other check in scripts/prod_watch.py is satisfied just as well by a
months-old build — which is how both CareAgents deployments ran code older
than PR #241 while the monitor reported 9/9 green. This pins the check that
closes that gap, and the exit-code split that keeps a stale build from being
reported as an outage.

No network. `prod_watch.get` AND `prod_watch.post` are replaced wholesale in
the autouse fixture, and `requests.get`/`requests.post` are tripwired under
them, so a test here that reaches the network fails loudly instead of passing
on production's say-so. Until #433 this docstring made the same claim while
only `get` was faked: the public demo's keyless `initialize` POST went to the
live MCP server on every run, with a one-second timeout, and one CI run went
red on it. Nothing in this file is marked as reaching the network, because
nothing in it does.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest
import requests

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


def _fake_get(build="4f2a91cbeef1", built_at=1754056800, grade="A",
              demo_patients=None, landing='<a href="/auth">start</a>',
              home=None):
    def get(url, timeout, **kw):
        if url.endswith("/r6/fhir/health"):
            return _Resp(200)
        if "$conformance" in url:
            return _Resp(200, {"grade": grade})
        if "Patient" in url:
            # None means "the tenant is in the state it is supposed to be in".
            # A number means "this many patients that are not the demo set",
            # which is what duplication actually looked like.
            ids = (list(prod_watch.DEMO_PATIENTS) if demo_patients is None
                   else [f"p{i}" for i in range(demo_patients)])
            return _Resp(200, {"entry": [
                {"resource": {"resourceType": "Patient", "id": i}}
                for i in ids]})
        if "Condition" in url:
            return _Resp(200, {"entry": [{"resource": {
                "resourceType": "Condition", "code": {"text": "Asthma"}}}]})
        if url.endswith("/healthz"):
            return _Resp(200, {"status": "ok", "provider": "openai",
                               "accounts": True, "build": build,
                               "built_at": built_at})
        if url.endswith("/auth"):
            return _Resp(200, text='<input maxlength="8">')
        if url.endswith("/home"):
            # Production's answer without a session is a redirect to /auth.
            # `home` is the page a hypothetical public /home would render.
            return _Resp(302) if home is None else _Resp(200, text=home)
        if url.endswith("/mcp"):
            return _Resp(401)
        if url.endswith("/health"):
            return _Resp(200)
        return _Resp(200, text=landing)
    return get


def _fake_post(url, timeout, **kw):
    # The public demo's keyless `initialize` — the one POST in the script.
    return _Resp(200, {"jsonrpc": "2.0", "id": 1, "result": {}})


def _no_network(url, *a, **kw):
    # Not a RequestException on purpose: prod_watch.get/post swallow those
    # into a status string, which is exactly how the real POST hid (#433).
    raise AssertionError(f"a test in this file reached the network: {url}")


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
    # Same reason, for the stream --json redirects (#270): main() assigns it
    # unconditionally, but a test that calls run() directly after one would
    # otherwise find its human output on stderr.
    monkeypatch.setattr(prod_watch, "_human_to_stderr", False)
    monkeypatch.setattr(prod_watch, "get", _fake_get())
    # Both verbs, not one. The script has exactly one POST, and leaving it
    # real made CI green depend on a production service (#433).
    monkeypatch.setattr(prod_watch, "post", _fake_post)
    # And the layer under them, so a fake a later test forgets to install
    # cannot fall through to a real request and pass anyway.
    monkeypatch.setattr(requests, "get", _no_network)
    monkeypatch.setattr(requests, "post", _no_network)
    yield
    prod_watch.results.clear()
    prod_watch.build_info.update(_FRESH_BUILD_INFO)


def _named(name):
    return [(n, ok, d) for n, ok, d in prod_watch.results if n == name]


def test_a_current_build_passes():
    assert prod_watch.run(1.0, [TIP]) == 0
    assert _named(prod_watch.BUILD_CHECK)[0][1] is True


def test_the_demo_handshake_never_leaves_the_process():
    """#433: this file said "No network" while its one POST was real.

    `test_a_current_build_passes` made a live HTTPS POST to the public demo
    MCP server on every run, with a one-second timeout, so CI green depended
    on a production deployment and a slow answer became an unexplained red.
    The fixture now fakes `post` and tripwires `requests.post` beneath it.
    MUTATION: drop the `post` fake from `_isolate` -> the tripwire fires here.
    """
    assert prod_watch.run(1.0, [TIP]) == 0
    (_, ok, detail), = _named(
        "mcp (public demo): serves an unauthenticated handshake")
    assert ok is True, detail
    assert "keyless initialize -> 200" in detail


# --- the host users reach, and the surface nobody could see (#537, #289) ----

def _requested(monkeypatch, **fake_kw):
    """Run against the fake and return every URL the run asked for."""
    real = _fake_get(**fake_kw)
    seen = []

    def get(url, timeout, **kw):
        seen.append(url)
        return real(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", get)
    prod_watch.run(1.0, [TIP])
    return seen


def test_the_monitor_watches_the_origin_people_visit(monkeypatch):
    """#537/#289: every CareAgents check ran against the Railway hostname,
    which nobody visits. A DNS or routing divergence between it and
    careagents.cloud was invisible by construction.

    MUTATION: point CAREAGENTS back at the Railway host -> red.
    """
    assert prod_watch.CAREAGENTS == "https://careagents.cloud"
    seen = _requested(monkeypatch)
    for path in ("/healthz", "/", "/auth", "/home"):
        assert f"https://careagents.cloud{path}" in seen, (path, seen)
    # The build marker comes from the origin, not the Railway host: #289's
    # point is that the alarm was pinned to the wrong address.
    assert seen.index("https://careagents.cloud/healthz") < seen.index(
        f"{prod_watch.CAREAGENTS_RAILWAY}/healthz")


def test_the_railway_host_keeps_its_readiness_check(monkeypatch):
    """/healthz on the Railway hostname is what Railway's own health check
    hits, and the one path that keeps answering directly once everything
    else there redirects to the origin. It stays watched, labelled as what
    it is, so the two hosts can be seen to diverge rather than assumed equal.
    """
    seen = _requested(monkeypatch)
    assert f"{prod_watch.CAREAGENTS_RAILWAY}/healthz" in seen
    railway = [u for u in seen if u.startswith(prod_watch.CAREAGENTS_RAILWAY)]
    assert railway == [f"{prod_watch.CAREAGENTS_RAILWAY}/healthz"], (
        "only /healthz is asked of the Railway host — everything else there "
        "is about to redirect, and the user-facing checks belong to the origin")
    (_, ok, detail), = _named("careagents (railway host): ready (db reachable)")
    assert ok is True
    assert "build=4f2a91cbeef1" in detail, (
        "the Railway host's build is shown beside the origin's so a split "
        "deployment is visible in the report")


START = '<a href="/auth">start</a>'  # what "landing renders" looks for
LIVE_TILE = ('<div class="surface on"><b>Web</b><span>always on</span></div>'
             '<div class="surface" id="tg-surface"><b>Telegram</b>'
             '<span id="tg-state">connect →</span></div>'
             '<div class="surface soon"><b>iMessage</b>'
             '<span>coming soon</span></div>')
SOON_TILE = ('<div class="surface soon"><b>Telegram</b>'
             '<span>coming soon</span></div>')


def test_a_landing_that_does_not_mention_telegram_passes():
    """What production's landing page looks like today."""
    prod_watch.run(1.0, [TIP])
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is True, detail
    # The check names what it read and what it could not, because a check
    # that quietly covers less than its name suggests is the shape of #537.
    assert "/" in detail
    assert "/home not scanned (302 without a session)" in detail


@pytest.mark.parametrize("landing,how", [
    ('<a href="https://t.me/care_bot?start=x">Open in Telegram</a>', "t.me"),
    ('<a class="btn" href="/tg">Open in Telegram</a>', "Open in Telegram"),
    ('<p>Chat with your agent on Telegram.</p>', "Chat with your agent"),
    (LIVE_TILE, "Telegram connect"),
])
def test_a_live_telegram_link_or_button_on_the_landing_is_an_outage(
        monkeypatch, landing, how):
    """#537: the surface had been dead since June while the board was green.

    Advertising a surface that cannot pair is the defect; the monitor's job
    is to say so where a person will read it. MUTATION: drop the Telegram
    check from run() -> red.
    """
    monkeypatch.setattr(prod_watch, "get", _fake_get(landing=START + landing))
    rc = prod_watch.run(1.0, [TIP])
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is False
    assert how in detail, detail
    # The remedy, on the line: red here is the truth about #536, not noise.
    assert "#536" in detail and "coming soon" in detail
    assert rc == 1, "a surface advertised as live but dead is an outage"
    assert [n for n, ok, _ in prod_watch.results if not ok] == [
        prod_watch.TELEGRAM_CHECK], "nothing else about this page is wrong"


@pytest.mark.parametrize("page", [
    SOON_TILE,
    # Neighbours must not vouch for or against each other: a live iMessage
    # tile beside a coming-soon Telegram tile is fine either way round.
    SOON_TILE + '<div class="surface" id="im-surface"><b>iMessage</b>'
                '<span id="im-state">connect →</span></div>',
    '<div class="surface" id="im-surface"><b>iMessage</b>'
    '<span id="im-state">connect →</span></div>' + SOON_TILE,
    '<p>Telegram and iMessage are coming soon.</p>',
    # Prose that names the surface without inviting anyone onto it.
    '<p>Every surface — web, Telegram, iMessage — goes through the guardrail'
    ' layer.</p>',
    # Script and style bodies are not copy anyone reads.
    '<script>window.CARE = {telegramBot: "care_bot"};</script>',
])
def test_a_telegram_tile_marked_coming_soon_passes(monkeypatch, page):
    monkeypatch.setattr(prod_watch, "get", _fake_get(landing=START + page))
    assert prod_watch.run(1.0, [TIP]) == 0
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is True, detail


HOME_TEMPLATE = (Path(__file__).parent.parent / "careagents" / "templates"
                 / "home.html").read_text()
LANDING_TEMPLATE = (Path(__file__).parent.parent / "careagents" / "templates"
                    / "landing.html").read_text()


def test_the_detector_reads_the_tile_the_real_home_page_renders():
    """Tied to the markup it guards, not to a hand-written copy of it.

    The first cut of this detector passed a one-line tile and missed the
    real one: the template breaks the line between `<b>Telegram</b>` and
    `<span>connect →</span>`, and a source newline was being read as the end
    of a piece of copy. The Jinja in the template is inert here; the tile is
    literal markup.

    Asserted as agreement with the template's own state rather than as a
    prediction of it, so the change that marks the tile "coming soon" (the
    fix for #536's dead end, in flight separately) turns this into the proof
    that the monitor will read that tile as no promise — which is what lets
    the check go green after it deploys.
    """
    how = prod_watch._telegram_advertised(HOME_TEMPLATE)
    if '<span id="tg-state">connect →</span>' in HOME_TEMPLATE:
        assert how.startswith("a live call to action"), how
        assert "Telegram connect" in how, how
    else:
        assert how == "", (
            "the Telegram tile changed and the monitor still reads it as a "
            f"live call to action: {how}")
    # The landing page makes no such promise — which is why the check is
    # green on production while #536 is open: the tile is behind sign-in,
    # where an unauthenticated monitor cannot see it.
    assert prod_watch._telegram_advertised(LANDING_TEMPLATE) == ""


def test_the_home_page_is_scanned_only_when_it_answers_without_a_session(
        monkeypatch):
    """The tile #536 describes lives on /home, behind sign-in. This monitor
    runs unauthenticated, so it can only ever see /home if that page starts
    answering without a session — and then it must look, because that is
    the moment the tile becomes a public promise.
    """
    monkeypatch.setattr(prod_watch, "get", _fake_get(home=LIVE_TILE))
    assert prod_watch.run(1.0, [TIP]) == 1
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is False
    assert detail.startswith("/home shows"), detail

    prod_watch.results.clear()
    monkeypatch.setattr(prod_watch, "get", _fake_get(home=SOON_TILE))
    assert prod_watch.run(1.0, [TIP]) == 0
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is True
    assert "/, /home" in detail and "not scanned" not in detail


def test_the_home_page_is_fetched_without_following_its_redirect(monkeypatch):
    # Following /home -> /auth would scan the sign-in page under /home's name
    # and report the home page as read when it was not.
    seen = {}
    real = _fake_get()

    def get(url, timeout, **kw):
        seen[url] = kw
        return real(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", get)
    prod_watch.run(1.0, [TIP])
    assert seen[f"{prod_watch.CAREAGENTS}/home"].get("allow_redirects") is False


def test_an_unreadable_landing_is_not_reported_as_telegram_free(monkeypatch):
    """Nothing read, nothing verified. A pass here would be the exact
    green-for-the-wrong-reason this check was added to end."""
    real = _fake_get()

    def get(url, timeout, **kw):
        if url == f"{prod_watch.CAREAGENTS}/":
            return "ConnectionError"
        return real(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", get)
    assert prod_watch.run(1.0, [TIP]) == 1
    (_, ok, detail), = _named(prod_watch.TELEGRAM_CHECK)
    assert ok is False
    assert "nothing was scanned" in detail and "ConnectionError" in detail


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


def test_the_documented_machine_readable_mode_is_machine_readable(monkeypatch,
                                                                  capsys):
    """#270: --json printed the payload after the ANSI-coloured human lines on
    the same stream, so the mode documented as 'machine-readable' died on
    `prod_watch.py --json | python -m json.tool`.

    Closed by sending the human report to stderr under --json. stdout is then
    the payload and nothing else — parsed here as a WHOLE stream, not from the
    first `{`, because slicing to a brace is what let the defect hide.
    """
    monkeypatch.setattr(sys, "argv", ["prod_watch.py", "--json"])
    prod_watch.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["checks"], "the payload is the real one, not an empty stub"
    # Option 2 of #270, not option 1: the human report moves, it does not
    # disappear. Someone watching a terminal still sees every check.
    assert prod_watch.BUILD_CHECK in captured.err
    assert "mcp (locked): alive" in captured.err


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
                                1e30, 10 ** 20,
                                # N10: bool is an int, so True walked straight
                                # through the `<= 0` guard and rendered 1970.
                                True, False])
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


def test_a_real_hard_failure_is_reported_as_one(monkeypatch, capsys, tmp_path):
    # The other half of hard_ok, driven through run() rather than through a
    # stub of it: _payload_for proves the plumbing from a code, this proves the
    # code. Without it, `hard_ok = code != 1` is only ever checked against a
    # `code` the test itself chose.
    monkeypatch.setattr(prod_watch, "get", _fake_get(grade="B"))
    out = tmp_path / "status.json"
    assert _payload(["--expect-sha", TIP, "--json-out", str(out)],
                    monkeypatch, capsys) == 1
    status = json.loads(out.read_text())
    hard = [c["name"] for c in status["checks"]
            if not c["ok"] and c["name"] != prod_watch.BUILD_CHECK]
    assert hard == ["healthclaw: guardrail grade A"]
    assert status["hard_ok"] is False and status["ok"] is False


def test_the_scheduled_run_still_pins_the_build_it_alarms_about():
    # Drop `--expect-sha` from the workflow and the stale alarm is dead in both
    # directions, permanently and silently: informational mode never fires it,
    # `asserted` is false so nothing can close it, and prod_watch never returns
    # 2 so the independent trigger never fires either. Everything stays green.
    # The same class of hole as dropping `--json-out`, which is already pinned.
    assert 'EXPECT="--expect-sha $TIP"' in WORKFLOW
    assert 'EXPECT="$EXPECT --expect-sha $sha"' in WORKFLOW


def test_the_workflow_only_reads_fields_the_script_actually_writes(
        monkeypatch, capsys, tmp_path):
    # A contract test across the two files, because a rename on either side is
    # invisible: `status?.missing?.field` is undefined, every predicate reading
    # it goes quietly false, and the alarm stops working with nothing red. This
    # compares the workflow's actual field accesses against a real payload.
    out = tmp_path / "status.json"
    _payload(["--expect-sha", TIP, "--json-out", str(out)], monkeypatch, capsys)
    status = json.loads(out.read_text())
    top = set(re.findall(r"status\?\.([a-z_]+)", WORKFLOW))
    build = set(re.findall(r"status\?\.build\?\.([a-z_]+)", WORKFLOW))
    assert top - {"build"} <= set(status), (
        f"workflow reads {sorted(top - {'build'} - set(status))} which the "
        "payload does not contain")
    assert build <= set(status["build"]), (
        f"workflow reads build.{sorted(build - set(status['build']))} which "
        "the payload does not contain")
    assert top and build, "the regexes must actually be matching something"


def test_an_unreachable_endpoint_is_not_reported_as_a_stale_build(monkeypatch,
                                                                  capsys):
    """#272: an unreachable /healthz made get() return an exception name, left
    `body` empty, and let `deployed` default to 'unknown' — indistinguishable
    from a genuinely unmarked build. The build check then asserted ok=False and
    the stale alarm told whoever read it at 03:00 to 'redeploy per RELEASING.md
    §4' while CareAgents was DOWN: a verdict about a field the run never read.

    Closed by gating the assertion on /healthz having actually been read, and
    report()-ing the build otherwise — the same not-asserted path a run with no
    --expect-sha already takes.
    """
    def unreachable(url, timeout, **kw):
        if url.endswith("/healthz"):
            return "ConnectionError"
        return _fake_get()(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", unreachable)
    # 1, not 2: this is an outage, and the outage alarm is the one whose remedy
    # is right. Exit 2 would have routed it to the stale-build alarm.
    assert prod_watch.run(1.0, [TIP]) == 1
    assert prod_watch.build_info["asserted"] is False, (
        "a build that was never read has not been asserted about")
    assert _named(prod_watch.BUILD_CHECK) == [], "must not count as a check"
    assert prod_watch.build_info["deployed"] is None, (
        "'unknown' here is this script's own default, not the deployment's")
    # The misdirection itself: no redeploy prescribed for an outage.
    out = capsys.readouterr().out
    assert "RELEASING.md" not in out and "auto-deploy" not in out


# --- the demo tenant's shape (#457, catalogue §10) ---------------------------

def test_the_expected_demo_patients_pass():
    """The state the demo is supposed to be in."""
    prod_watch.run(timeout=1, expect_sha=[])
    name = "healthclaw: the demo tenant holds exactly its demo patients"
    assert _named(name) and _named(name)[0][1] is True, _named(name)


def test_a_duplicated_demo_tenant_is_a_hard_failure(monkeypatch):
    """19 Patients is what production actually held on 2026-08-10.

    Every other check in this file passes just as well against that tenant,
    which is why a physician advisor found it on camera instead of a machine
    finding it a month earlier.

    MUTATION: drop the demo-tenant check from run() -> red.
    """
    monkeypatch.setattr(prod_watch, "get", _fake_get(demo_patients=19))
    rc = prod_watch.run(timeout=1, expect_sha=[])

    name = "healthclaw: the demo tenant holds exactly its demo patients"
    entry = _named(name)
    assert entry, "the demo-tenant check did not run"
    assert entry[0][1] is False
    assert "19" in entry[0][2]
    assert rc == 1, "a duplicated demo tenant must be a hard failure, not a warning"


def test_an_empty_demo_tenant_is_a_failure_not_a_pass(monkeypatch):
    """Zero is a real failure, and a different one from "cannot read".

    MUTATION: use `if found:` instead of `is not None` -> red, because an
    empty tenant would then be reported as an unreadable one.
    """
    monkeypatch.setattr(prod_watch, "get", _fake_get(demo_patients=0))
    prod_watch.run(timeout=1, expect_sha=[])

    entry = _named("healthclaw: the demo tenant holds exactly its demo patients")
    assert entry and entry[0][1] is False
    assert "0 Patient(s)" in entry[0][2], (
        f"an empty tenant must report as empty, not as unreadable: {entry}")


def test_a_missing_demo_persona_is_a_failure(monkeypatch):
    """The failure a COUNT cannot see, and the one that costs a recording.

    Three of four patients is not "nearly right" — it is a demo that opens on
    a patient who is not there. The old check counted, so it read this as
    healthy right up until the count happened to be one, and read the correct
    four-patient tenant as broken for four days after the personas were
    seeded.

    MUTATION: compare len(found) to len(DEMO_PATIENTS) instead of the sets ->
    red, because a substitution keeps the count.
    """
    kept = list(prod_watch.DEMO_PATIENTS)[:-1]
    real = _fake_get()

    def get(url, timeout, **kw):
        if "Patient" in url:
            return _Resp(200, {"entry": [
                {"resource": {"resourceType": "Patient", "id": i}}
                for i in kept]})
        return real(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", get)
    rc = prod_watch.run(timeout=1, expect_sha=[])

    name = "healthclaw: the demo tenant holds exactly its demo patients"
    entry = _named(name)
    assert entry and entry[0][1] is False
    assert "missing" in entry[0][2] and prod_watch.DEMO_PATIENTS[-1] in entry[0][2], (
        f"the report must name the persona that vanished: {entry}")
    assert rc == 1


def test_a_substituted_patient_is_caught_though_the_count_is_right(monkeypatch):
    """Four patients, one of them a stranger. A count says fine."""
    swapped = list(prod_watch.DEMO_PATIENTS)[:-1] + ["demo-someone-else"]
    real = _fake_get()

    def get(url, timeout, **kw):
        if "Patient" in url:
            return _Resp(200, {"entry": [
                {"resource": {"resourceType": "Patient", "id": i}}
                for i in swapped]})
        return real(url, timeout, **kw)

    monkeypatch.setattr(prod_watch, "get", get)
    rc = prod_watch.run(timeout=1, expect_sha=[])

    entry = _named("healthclaw: the demo tenant holds exactly its demo patients")
    assert entry and entry[0][1] is False
    assert "unexpected demo-someone-else" in entry[0][2], entry
    assert rc == 1


# --- repeat suppression: an alarm that repeats itself gets muted -------------
#
# #427 collected 35 identical comments at six-hour intervals and the
# maintainer muted the thread, which is the rational response and also the end
# of the alarm: the next real failure would have arrived in a muted thread.
# These pin the mechanism that stops it. The semantic walk-through lives in
# tests/tools/prod_watch_alarm_sim.js, which drives the real script body; CI
# cannot run node, so what CI can hold is that the moving parts are still here.

def test_the_firing_branch_refreshes_the_issue_body_every_run():
    """An edit sends no notification, so the issue can stay current for free.

    MUTATION: delete the issues.update call from the firing branch -> red.
    A stale body is worse than none: it shows a failure that may have been
    superseded, next to a comment thread that stopped when nothing changed.
    """
    firing = WORKFLOW.split("if (firing) {")[1].split("} else if (resolved)")[0]
    assert "issues.update" in firing, (
        "the firing branch no longer refreshes the issue body, so the issue "
        "shows whatever was true the last time something changed")
    assert "body: text" in firing


def test_a_comment_is_posted_only_when_the_fingerprint_changes():
    """MUTATION: drop the `previous !== fingerprint` guard -> red.

    Without it the workflow is back to one notification every six hours for
    as long as anything is wrong.
    """
    firing = WORKFLOW.split("if (firing) {")[1].split("} else if (resolved)")[0]
    assert "previous !== fingerprint" in firing, (
        "createComment is no longer guarded by a change in what is failing")
    guard = firing.index("previous !== fingerprint")
    comment = firing.index("issues.createComment")
    assert guard < comment, (
        "the comment must be inside the change guard, not beside it")


def test_an_unprovable_state_still_speaks():
    """Silence on a state we cannot verify is this workflow's own failure mode.

    With no status.json there is no way to show the failure is unchanged, so
    the fingerprint is seeded with the run id and can never match the stored
    one. MUTATION: give the unknown case a constant -> the alarm goes quiet
    for every run after the first, which is the failure #258 was about.
    """
    assert "unverified-run-${context.runId}" in WORKFLOW, (
        "an unverifiable run must produce a fingerprint that cannot match")


def test_the_fingerprint_is_the_failing_set_not_its_details():
    """A count moving 4 -> 5 while the same check fails is not news.

    Fingerprinting on details would re-notify on every run whose numbers
    wobble, which is the noise this replaced.
    """
    assert "c.ok !== true).map(c => c.name)" in WORKFLOW, (
        "the outage fingerprint should be built from failing check NAMES")
    assert "checks:${failingNames}" in WORKFLOW
