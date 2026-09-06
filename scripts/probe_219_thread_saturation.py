#!/usr/bin/env python3
"""Measure what concurrent chat turns actually do to CareAgents' ``/healthz``.

Run-once measurement for issue #219, which claims:

    One worker, 8 threads. Each chat turn holds a thread for its whole
    duration -- up to 6 tool rounds x 25s HealthClaw timeout, plus a 90s LLM
    timeout. 8 concurrent turns block everything including ``/healthz``,
    Railway marks the container unhealthy, restarts it, and every chat
    history and rate-limit state is wiped.

This probe measures the first half (does ``/healthz`` starve?) and reads the
second half out of the code. It does NOT touch production: everything runs
against a local gunicorn plus a fake HealthClaw on loopback.

What is faked, and why
----------------------
* **HealthClaw** is a local ``ThreadingHTTPServer`` (:class:`FakeHealthClaw`).
  ``HEALTHCLAW_BASE`` points at it, so the *real* ``HealthClawClient`` and its
  *real* 25s timeout are exercised -- nothing is monkeypatched inside
  gunicorn. The fake answers token-mint, message-claim, run-create and
  worker-health instantly, and reports a run as ``running`` for a controlled
  number of seconds before completing it. That controlled duration is the
  independent variable: it is how long a chat turn holds a gunicorn thread.
* **The LLM is not faked, because the web tier never calls it.** Since #257
  (`careagents/wsgi.py`) inference runs in ``python -m careagents.worker``.
  The web request enqueues a run and streams durable events back. No worker
  process is started here; the fake supplies the run's lifecycle directly.

Everything else is the shipping code: ``careagents.wsgi:app`` under the same
gunicorn invocation as ``deploy/careagents/Dockerfile``.

Usage
-----
    uv run python scripts/probe_219_thread_saturation.py
    uv run python scripts/probe_219_thread_saturation.py --quick
    uv run python scripts/probe_219_thread_saturation.py --only saturation

Exits 0 when the measurement completed (whatever it found); non-zero only if
the rig itself could not run.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Generated per run, never written down. The rig needs a session signing key
# and an internal-secret value that its own fake HealthClaw will accept; both
# are throwaway and both processes receive them through the environment. They
# are computed rather than hard-coded so this committed file contains no
# credential-shaped literal for a scanner -- or a reader -- to mistake for one.
SESSION_SECRET = secrets.token_urlsafe(32)
MINT_SECRET = secrets.token_urlsafe(16)
STEP_UP_STUB = secrets.token_urlsafe(16)

# Docker HEALTHCHECK --timeout=5s (deploy/careagents/Dockerfile) and the
# healthcheckTimeout used in this repo's railway.toml. Both are probed, so the
# table says which budget a slow /healthz actually breaks.
PROBE_TIMEOUTS = (5.0, 30.0)


# --------------------------------------------------------------------------
# Fake HealthClaw
# --------------------------------------------------------------------------

class FakeState:
    """Knobs the scenarios turn, shared with the request handler."""

    def __init__(self) -> None:
        self.hold_seconds = 20.0        # how long a run stays "running"
        self.worker_health_delay = 0.0  # stall /runs/workers/health this long
        self.events_hang = 0.0          # stall /runs/<id>/events this long
        self.first_seen: dict[str, float] = {}
        self.counts: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def reset_counts(self) -> None:
        with self.lock:
            self.counts = defaultdict(int)
            self.first_seen = {}

    def bump(self, key: str) -> None:
        with self.lock:
            self.counts[key] += 1

    def run_elapsed(self, run_id: str) -> float:
        with self.lock:
            started = self.first_seen.setdefault(run_id, time.monotonic())
        return time.monotonic() - started


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # keep-alive, like a real HealthClaw
    state: FakeState = None            # set on the server class

    def log_message(self, *_args) -> None:  # noqa: D102 - silence the fake
        pass

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = urlparse(self.path).path
        st = self.state

        if path.endswith("/internal/step-up-token"):
            st.bump("mint")
            return self._reply(200, {"token": STEP_UP_STUB})

        if path == "/command-center/api/conversations":
            st.bump("message")
            return self._reply(
                201, {"id": f"msg-{st.counts['message']}",
                      "role": body.get("role", "user")})

        if path == "/command-center/api/runs":
            st.bump("run_create")
            run_id = f"run-{st.counts['run_create']}"
            return self._reply(201, {
                "id": run_id, "tenant_id": body.get("tenant_id"),
                "agent_id": "careagents", "status": "queued"})

        return self._reply(404, {"error": "probe fake: unhandled POST"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        st = self.state

        if path == "/command-center/api/runs/workers/health":
            st.bump("worker_health")
            if st.worker_health_delay:
                time.sleep(st.worker_health_delay)
            return self._reply(200, {"available": True, "workers": 1})

        if path.startswith("/command-center/api/runs/") and \
                path.endswith("/events"):
            st.bump("run_events")
            if st.events_hang:
                # The hang the read timeout is supposed to bound. _stream_run
                # has no try/except, so HealthClawError ends the generator.
                time.sleep(st.events_hang)
            run_id = path.split("/")[-2]
            after = int((query.get("after") or ["0"])[0])
            if st.run_elapsed(run_id) < st.hold_seconds:
                return self._reply(200, {"events": [], "status": "running"})
            events = []
            if after < 1:
                events.append({"id": 1, "type": "agent.text",
                               "payload": {"text": "probe answer"}})
            return self._reply(200, {"events": events, "status": "completed"})

        if path.startswith("/command-center/api/runs/"):
            st.bump("run_lookup")
            run_id = path.rstrip("/").split("/")[-1]
            return self._reply(200, {
                "id": run_id, "tenant_id": self.headers.get("X-Tenant-Id"),
                "agent_id": "careagents", "status": "running"})

        return self._reply(404, {"error": "probe fake: unhandled GET"})


def start_fake_healthclaw() -> tuple[ThreadingHTTPServer, FakeState, int]:
    state = FakeState()
    handler = type("ProbeHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, server.server_address[1]


# --------------------------------------------------------------------------
# Seeding + session cookies
# --------------------------------------------------------------------------

def base_env(db_path: Path, fake_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "CARE_ENV": "development",
        "CARE_SESSION_SECRET": SESSION_SECRET,
        "CARE_DATABASE_URL": f"sqlite:///{db_path}",
        "HEALTHCLAW_BASE": f"http://127.0.0.1:{fake_port}",
        "HEALTHCLAW_MINT_SECRET": MINT_SECRET,
        # The burst limiter is 20 turns / 600s per account and the daily cap is
        # 200. Both are raised here ONLY so a rate-limited turn (which returns
        # 429 instantly and holds no thread) cannot masquerade as a held
        # thread. Every scenario asserts its turns were accepted.
        "CARE_CHAT_TURNS": "10000",
        "CARE_CHAT_TURNS_PER_DAY": "100000",
        "PYTHONUNBUFFERED": "1",
        # macOS-only rig artifact: gunicorn's pre-fork master crashes its
        # children with "+[NSCharacterSet initialize] may have been in
        # progress ... Crashing instead". The container runs Linux, where this
        # variable is inert. It changes nothing about Python's threading.
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES",
        # Import careagents from the repo without running gunicorn *in* it --
        # gunicorn 25 drops a `gunicorn.ctl` control socket in its cwd.
        "PYTHONPATH": str(REPO_ROOT),
    })
    for stale in ("PORT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(stale, None)
    return env


def seed_accounts(env: dict[str, str], count: int,
                  prefix: str = "probe") -> list[dict]:
    """One account + connection + agent per virtual user, plus its cookie.

    Distinct accounts on purpose: N concurrent turns from ONE account is a
    different scenario (the per-account limiter), not the one #219 describes.
    """
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        from careagents.accounts import AccountService
        from careagents.app import create_app
        from careagents.config import Config
        from careagents.models import Account, now

        cfg = Config()
        svc = AccountService(cfg)
        app = create_app(cfg)
        serializer = app.session_interface.get_signing_serializer(app)

        users: list[dict] = []
        for i in range(count):
            with svc.session() as s:
                acct = Account(email=f"{prefix}-{i}@example.invalid",
                               email_verified_at=now())
                s.add(acct)
                s.flush()
                account_id = acct.id
            conn_id = svc.add_connection(
                account_id, "sample", f"probe-tenant-{i}", "Probe records")
            agent_id = svc.create_agent(account_id, "Probe", "calm", conn_id)
            cookie = serializer.dumps({"account_id": account_id,
                                       "_permanent": True})
            users.append({"account_id": account_id, "agent_id": agent_id,
                          "cookie": cookie})
        return users
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --------------------------------------------------------------------------
# Gunicorn, run the way the container runs it
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gunicorn:
    """`deploy/careagents/Dockerfile`'s web command, minus the container."""

    def __init__(self, env: dict[str, str], workers: int, threads: int,
                 wsgi: str = "careagents.wsgi:app") -> None:
        self.port = free_port()
        self.workers, self.threads, self.wsgi = workers, threads, wsgi
        self.env = dict(env)
        self.env["PORT"] = str(self.port)
        self.cwd = Path(tempfile.mkdtemp(prefix="probe219-cwd-"))
        self.proc: subprocess.Popen | None = None
        self.log = tempfile.NamedTemporaryFile(
            prefix="probe219-gunicorn-", suffix=".log", delete=False)

    @property
    def label(self) -> str:
        return (f"{self.workers}w x {self.threads}t "
                f"({self.workers * self.threads} threads)")

    def __enter__(self) -> "Gunicorn":
        cmd = [sys.executable, "-m", "gunicorn", self.wsgi,
               "--bind", f"127.0.0.1:{self.port}",
               "--workers", str(self.workers),
               "--threads", str(self.threads),
               "--timeout", "180",
               "--access-logfile", "-", "--error-logfile", "-"]
        self.proc = subprocess.Popen(cmd, cwd=str(self.cwd), env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"gunicorn exited {self.proc.returncode}; log: "
                    f"{self.log.name}\n{Path(self.log.name).read_text()[-2000:]}")
            probe = health_probe(self.port, timeout=2.0)
            if probe["status"] == 200:
                return self
            time.sleep(0.25)
        raise RuntimeError(f"gunicorn never became ready; log: {self.log.name}")

    def __exit__(self, *_exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


# --------------------------------------------------------------------------
# Load generation
# --------------------------------------------------------------------------

def health_probe(port: int, timeout: float) -> dict:
    """One /healthz request on its own connection, like a container probe."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        resp.read()
        return {"status": resp.status,
                "seconds": time.monotonic() - started, "error": None}
    except Exception as exc:  # noqa: BLE001 - a probe records its failures
        return {"status": None, "seconds": time.monotonic() - started,
                "error": type(exc).__name__}
    finally:
        conn.close()


class Holder(threading.Thread):
    """One virtual user: POST /api/chat and consume the SSE to the end."""

    def __init__(self, port: int, user: dict, stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self.port, self.user, self.stop = port, user, stop
        self.http_status: int | None = None
        self.accepted_at: float | None = None
        self.finished_at: float | None = None
        self.events: list[str] = []
        self.error: str | None = None
        self.started_at = 0.0

    def run(self) -> None:
        self.started_at = time.monotonic()
        body = json.dumps({"agent_id": self.user["agent_id"],
                           "message": "probe turn"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=200)
        try:
            conn.request("POST", "/api/chat", body=body, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Cookie": f"session={self.user['cookie']}"})
            resp = conn.getresponse()
            self.http_status = resp.status
            if resp.status != 200:
                self.error = resp.read()[:200].decode("utf-8", "replace")
                return
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type", "?")
                self.events.append(kind)
                if kind == "accepted" and self.accepted_at is None:
                    self.accepted_at = time.monotonic()
                if kind in ("done", "reconnect"):
                    break
                if self.stop.is_set():
                    break
        except Exception as exc:  # noqa: BLE001 - a probe records its failures
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.finished_at = time.monotonic()
            conn.close()


def run_scenario(gun: Gunicorn, state: FakeState, users: list[dict],
                 n: int, hold: float, settle: float = 2.0,
                 stagger: float = 0.0) -> dict:
    """N concurrent turns; probe /healthz while they hold threads."""
    state.hold_seconds = hold
    state.reset_counts()
    stop = threading.Event()
    holders = [Holder(gun.port, users[i], stop) for i in range(n)]
    t0 = time.monotonic()
    for h in holders:
        h.start()
        if stagger:
            time.sleep(stagger)
    time.sleep(settle)  # let the holds establish before probing

    # Three sweeps in parallel. The 5s and 30s probes answer "does the
    # configured budget hold?"; the patient one has enough budget to record
    # what the latency actually WAS, so a timeout is never the only datum.
    patience = hold + 60.0
    results: dict[str, list[dict]] = {"p5": [], "p30": [], "patient": []}

    def sweep(key: str, timeout: float, count: int) -> None:
        for _ in range(count):
            results[key].append(health_probe(gun.port, timeout))
            time.sleep(0.2)

    threads = [threading.Thread(target=sweep, args=("p5", 5.0, 4), daemon=True),
               threading.Thread(target=sweep, args=("p30", 30.0, 1),
                                daemon=True),
               threading.Thread(target=sweep, args=("patient", patience, 1),
                                daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    probe_window = time.monotonic() - t0

    stop.set()
    for h in holders:
        h.join(timeout=hold + 60)

    accepted = [h for h in holders if h.accepted_at is not None]
    rejected = [h for h in holders
                if h.http_status not in (200, None) or
                (h.http_status is None and h.error)]
    with state.lock:
        polls = state.counts.get("run_events", 0)

    def summarise(rows: list[dict]) -> dict:
        oks = [r for r in rows if r["status"] == 200]
        return {
            "n": len(rows),
            "ok": len(oks),
            "fail": len(rows) - len(oks),
            "max_s": max((r["seconds"] for r in rows), default=0.0),
            "each_s": [round(r["seconds"], 2) for r in rows],
            "errors": sorted({r["error"] for r in rows if r["error"]}),
        }

    return {
        "concurrency": n,
        "hold": hold,
        "accepted": len(accepted),
        "accepted_within_settle": len(
            [h for h in accepted if h.accepted_at - t0 <= settle]),
        "rejected": len(rejected),
        "reject_detail": sorted({(h.error or "")[:80] for h in rejected}),
        "probe_window_s": probe_window,
        "hold_seconds_observed": [
            round((h.finished_at - h.started_at), 2) for h in holders
            if h.finished_at],
        "run_event_polls": polls,
        "poll_rate_per_s": round(polls / probe_window, 1) if probe_window else 0,
        "p5": summarise(results["p5"]),
        "p30": summarise(results["p30"]),
        "patient": summarise(results["patient"]),
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_table(title: str, rows: list[dict]) -> None:
    print()
    print(title)
    print("-" * len(title))
    head = (f"{'N':>3} {'hold':>6} {'turns started':>13} "
            f"{'healthz 5s budget':>18} {'healthz 30s budget':>18} "
            f"{'true wait':>10} {'HC req/s':>9}")
    print(head)
    print("-" * len(head))
    for r in rows:
        started = f"{r['accepted_within_settle']}/{r['concurrency']}"
        p5 = f"{r['p5']['ok']}/{r['p5']['n']} ok, max {r['p5']['max_s']:.2f}s"
        p30 = (f"{r['p30']['ok']}/{r['p30']['n']} ok, "
               f"max {r['p30']['max_s']:.2f}s")
        true = f"{r['patient']['max_s']:.2f}s"
        print(f"{r['concurrency']:>3} {r['hold']:>5.0f}s {started:>13} "
              f"{p5:>18} {p30:>18} {true:>10} "
              f"{r['poll_rate_per_s']:>9}")
    print("  'turns started' = turns that got their `accepted` event within "
          "the 2s settle;")
    print("  'true wait'     = /healthz latency measured with a budget large "
          "enough to record it.")


def saturation_matrix(env: dict, state: FakeState, users: list[dict],
                      configs: list[tuple[int, int]], levels: list[int],
                      hold: float, wsgi: str = "careagents.wsgi:app",
                      stagger: float = 0.0) -> dict:
    out: dict[str, list[dict]] = {}
    for workers, threads in configs:
        with Gunicorn(env, workers, threads, wsgi=wsgi) as gun:
            rows = []
            for n in levels:
                row = run_scenario(gun, state, users, n, hold,
                                   stagger=stagger)
                rows.append(row)
                print(f"  [{gun.label}] N={n:<3} started="
                      f"{row['accepted_within_settle']}/{n:<3} "
                      f"healthz(5s) {row['p5']['ok']}/{row['p5']['n']} "
                      f"healthz(30s) {row['p30']['ok']}/{row['p30']['n']} "
                      f"true wait {row['patient']['max_s']:.2f}s")
            out[gun.label] = rows
            print_table(f"/healthz under N concurrent chat turns — {gun.label}",
                        rows)
    return out


def turn_hold_measurement(env: dict, state: FakeState,
                          users: list[dict]) -> list[dict]:
    """Item 2: what one turn actually holds a web thread for."""
    rows = []
    with Gunicorn(env, 2, 4) as gun:
        for hold in (2.0, 10.0, 30.0):
            state.hold_seconds = hold
            state.reset_counts()
            stop = threading.Event()
            h = Holder(gun.port, users[0], stop)
            h.start()
            h.join(timeout=hold + 60)
            with state.lock:
                polls = state.counts.get("run_events", 0)
            rows.append({
                "run_seconds": hold,
                "thread_hold_s": round(h.finished_at - h.started_at, 2),
                "accept_latency_s": round(h.accepted_at - h.started_at, 3)
                if h.accepted_at else None,
                "events": h.events,
                "run_event_polls": polls,
            })
    print()
    print("Per-turn web-thread hold (one turn, 2w x 4t)")
    print("--------------------------------------------")
    print(f"{'run duration':>13}  {'thread held':>12}  "
          f"{'accepted after':>15}  {'poll reqs':>10}  events")
    for r in rows:
        print(f"{r['run_seconds']:>12.0f}s  {r['thread_hold_s']:>11.2f}s  "
              f"{r['accept_latency_s']:>14.3f}s  "
              f"{r['run_event_polls']:>10}  {','.join(r['events'])}")
    return rows


def distribution_variance(env: dict, state: FakeState, users: list[dict],
                          n: int, hold: float, repeats: int,
                          stagger: float = 0.0) -> list[dict]:
    """Why 2 workers starve BELOW their thread count.

    Gunicorn pre-fork workers accept from a shared socket, so N turns do not
    spread evenly. With 2x4, six turns can land 5-and-1 and saturate one
    worker while the other idles. /healthz is served by whichever worker
    accepts it, so starvation below the total thread count is a coin flip --
    which a single sample cannot show.
    """
    rows = []
    with Gunicorn(env, 2, 4) as gun:
        for i in range(repeats):
            row = run_scenario(gun, state, users, n, hold,
                               stagger=stagger)
            rows.append(row)
            print(f"  run {i + 1}/{repeats}: /healthz true wait "
                  f"{row['patient']['max_s']:.2f}s  "
                  f"(5s budget {row['p5']['ok']}/{row['p5']['n']} ok)")
    stalled = [r for r in rows if r["patient"]["max_s"] > 1.0]
    print(f"\n  N={n} on 2w x 4t, {repeats} runs: /healthz stalled in "
          f"{len(stalled)}/{repeats}. "
          f"Waits: {[round(r['patient']['max_s'], 2) for r in rows]}")
    return rows


def restart_and_state(env: dict, state: FakeState) -> dict:
    """Item 4: which state a restart actually loses.

    Drives the real endpoints, restarts gunicorn against the same database,
    and reports what survived. Three stores are in play: the in-process burst
    limiter (`app.py` `turns`), the durable daily counter (`UsageDay`), and
    the login-code attempt counter (`EmailToken.attempts`).
    """
    env = dict(env)
    env["CARE_CHAT_TURNS"] = "3"          # small window, so it is easy to trip
    env["CARE_CHAT_TURNS_PER_DAY"] = "1000"
    state.hold_seconds = 0.5
    state.reset_counts()

    user = seed_accounts(env, 1, prefix=f"restart-{int(time.time())}")[0]
    result: dict = {}

    def one_turn(port: int) -> tuple[int, str]:
        body = json.dumps({"agent_id": user["agent_id"],
                           "message": "restart probe"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request("POST", "/api/chat", body=body, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Cookie": f"session={user['cookie']}"})
            resp = conn.getresponse()
            payload = resp.read()[:200].decode("utf-8", "replace")
            return resp.status, payload
        finally:
            conn.close()

    with Gunicorn(env, 1, 4) as gun:
        before = [one_turn(gun.port) for _ in range(4)]
    result["before_restart"] = [
        {"status": s, "body": b[:80]} for s, b in before]

    with state.lock:
        result["messages_sent_to_healthclaw"] = state.counts.get("message", 0)

    # Same database, brand new processes: this IS the restart.
    with Gunicorn(env, 1, 4) as gun:
        after = one_turn(gun.port)
    result["after_restart"] = {"status": after[0], "body": after[1][:80]}

    saved = dict(os.environ)
    os.environ.update(env)
    try:
        from careagents.accounts import AccountService
        from careagents.config import Config
        from careagents.models import EmailToken, UsageDay

        svc = AccountService(Config())
        with svc.session() as s:
            rows = s.query(UsageDay).filter_by(
                account_id=user["account_id"]).all()
            result["usage_day_turns_after_restart"] = [
                int(r.turns or 0) for r in rows]
        # Login-code attempts: two wrong guesses, then read the column back
        # through a fresh engine -- the same thing a restart does.
        email = f"restart-probe-{int(time.time())}@example.invalid"
        svc.start_email_code(email)       # dev: logged, not sent (no API key)
        for _ in range(2):
            try:
                svc.verify_email_code(email, "00000000")
            except Exception:  # noqa: BLE001 - a wrong code is the point
                pass
        svc2 = AccountService(Config())   # new engine == new process
        with svc2.session() as s:
            tok = s.query(EmailToken).filter_by(email=email).first()
            result["login_attempts_after_new_engine"] = (
                int(tok.attempts or 0) if tok else None)
    finally:
        os.environ.clear()
        os.environ.update(saved)

    print("  burst limiter (CARE_CHAT_TURNS=3), 4 turns before restart: "
          f"{[s for s, _ in before]}")
    print(f"  first turn AFTER restart: {result['after_restart']['status']} "
          f"{result['after_restart']['body']}")
    print("  durable daily counter (UsageDay.turns) across the restart: "
          f"{result['usage_day_turns_after_restart']}")
    print("  login-code attempts read back through a new engine: "
          f"{result['login_attempts_after_new_engine']}")
    print("  chat turns persisted to HealthClaw, not CareAgents memory: "
          f"{result['messages_sent_to_healthclaw']} POSTs to "
          "/command-center/api/conversations")
    return result


def hanging_dependency(env: dict, state: FakeState, users: list[dict],
                       client_timeout_label: str,
                       wsgi: str = "careagents.wsgi:app") -> dict:
    """Item 5b: /healthz when HealthClaw hangs, at zero chat load.

    /healthz calls hc.agent_worker_health(), so its latency floor is a network
    call to HealthClaw with the client's read timeout -- independent of any
    thread saturation.
    """
    with Gunicorn(env, 2, 4, wsgi=wsgi) as gun:
        state.worker_health_delay = 40.0   # longer than either read timeout
        try:
            rows = [health_probe(gun.port, 60.0) for _ in range(2)]
        finally:
            state.worker_health_delay = 0.0
    result = {"client_timeout": client_timeout_label,
              "seconds": [round(r["seconds"], 2) for r in rows],
              "status": [r["status"] for r in rows],
              "error": [r["error"] for r in rows]}
    print(f"  HealthClaw hangs, no chat load, client read timeout "
          f"{client_timeout_label}: /healthz took "
          f"{result['seconds']} -> {result['status']}")
    return result


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="fewer concurrency levels and a shorter hold")
    ap.add_argument("--hold", type=float, default=20.0,
                    help="seconds a faked run stays 'running' (default 20)")
    ap.add_argument("--only",
                    choices=["saturation", "hold", "worstcase", "variance",
                             "restart", "mitigations"],
                    help="run one section instead of all of them")
    args = ap.parse_args()

    levels = [0, 4, 8, 16] if args.quick else [0, 4, 6, 8, 10, 16]
    hold = 8.0 if args.quick else args.hold

    tmp = Path(tempfile.mkdtemp(prefix="probe219-"))
    server, state, fake_port = start_fake_healthclaw()
    env = base_env(tmp / "careagents.db", fake_port)

    print("=" * 78)
    print("probe #219 — /healthz under concurrent chat turns")
    print("=" * 78)
    print(f"repo HEAD      : "
          f"{subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()}")
    print(f"python         : {sys.version.split()[0]}")
    print(f"fake HealthClaw: http://127.0.0.1:{fake_port}")
    print(f"hold per turn  : {hold}s     concurrency levels: {levels}")
    print(f"scratch        : {tmp}")

    users = seed_accounts(env, max(levels) or 1)
    print(f"seeded         : {len(users)} accounts (1 per virtual user)")

    report: dict = {"levels": levels, "hold": hold}
    try:
        if args.only in (None, "saturation"):
            print("\n### Item 1 — does /healthz starve? ###")
            report["saturation"] = saturation_matrix(
                env, state, users,
                configs=[(2, 4), (1, 8)], levels=levels, hold=hold)

        if args.only in (None, "hold"):
            print("\n### Item 2 — real per-turn thread hold ###")
            report["turn_hold"] = turn_hold_measurement(env, state, users)

        if args.only in (None, "worstcase"):
            # The tables above use a short hold to keep the matrix fast. The
            # number that matters is the one at the code's OWN worst case: a
            # run that lives to `CARE_RUN_DEADLINE_SECONDS` (120s default).
            worst = 20.0 if args.quick else 120.0
            print(f"\n### Worst case — 8 turns each holding {worst:.0f}s "
                  f"(the run deadline) ###")
            report["worst_case"] = saturation_matrix(
                env, state, users, configs=[(2, 4)], levels=[8],
                hold=worst)

        if args.only in (None, "variance"):
            print("\n### Why 2w x 4t starves below 8 — N=6, repeated ###")
            repeats = 3 if args.quick else 6
            print("  arrivals in the same millisecond (burst):")
            report["variance_burst"] = distribution_variance(
                env, state, users, n=6, hold=hold, repeats=repeats)
            print("\n  arrivals spread 0.3s apart (closer to real traffic):")
            report["variance_staggered"] = distribution_variance(
                env, state, users, n=6, hold=hold, repeats=repeats,
                stagger=0.3)

        if args.only in (None, "restart"):
            print("\n### Item 4 — what a restart actually loses ###")
            report["restart"] = restart_and_state(env, state)

        if args.only in (None, "mitigations"):
            print("\n### Item 5 — measured effect of each mitigation ###")
            print("\n(a) bound the turn: same N, shorter run")
            report["mitigation_bound_turn"] = saturation_matrix(
                env, state, users, configs=[(2, 4)],
                levels=[8, 16], hold=max(2.0, hold / 4))

            print("\n(b) drop the HealthClaw read timeout 25s -> 10s")
            shim_env = dict(env)
            shim = tmp / "probe_wsgi_10s.py"
            shim.write_text(
                '"""Probe-only shim: 10s HealthClaw read timeout.\n\n'
                'Rebinds the client default BEFORE create_app, so nothing in\n'
                'careagents/ is edited to measure the mitigation.\n"""\n'
                "import careagents.healthclaw as _hc\n"
                "_orig = _hc.HealthClawClient.__init__\n"
                "def _patched(self, base, mint_secret, timeout=10.0):\n"
                "    _orig(self, base, mint_secret, timeout)\n"
                "_hc.HealthClawClient.__init__ = _patched\n"
                "from careagents.app import create_app\n"
                "app = create_app()\n")
            shim_env["PYTHONPATH"] = (
                f"{tmp}{os.pathsep}{shim_env.get('PYTHONPATH', '')}")
            shim_wsgi = "probe_wsgi_10s:app"

            print("  b1. /healthz's own call, no chat load:")
            report["mitigation_timeout_25"] = hanging_dependency(
                env, state, users, "25s (shipping default)")
            report["mitigation_timeout_10"] = hanging_dependency(
                shim_env, state, users, "10s (proposed)", wsgi=shim_wsgi)

            print("\n  b2. under load — HealthClaw hangs on run events, so "
                  "the read timeout IS the thread hold (N=8):")
            state.events_hang = 40.0
            try:
                report["mitigation_hang_25"] = saturation_matrix(
                    env, state, users, configs=[(2, 4)], levels=[8],
                    hold=hold)
                report["mitigation_hang_10"] = saturation_matrix(
                    shim_env, state, users, configs=[(2, 4)], levels=[8],
                    hold=hold, wsgi=shim_wsgi)
            finally:
                state.events_hang = 0.0

            print("\n(c) raise threads: 2w x 8t (16 threads)")
            report["mitigation_threads"] = saturation_matrix(
                env, state, users, configs=[(2, 8)],
                levels=[8, 16], hold=hold)
    finally:
        server.shutdown()

    out = tmp / "probe-219-report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nmachine-readable report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
