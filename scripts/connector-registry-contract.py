#!/usr/bin/env python3
"""The connector registry's contract, asserted against a booted app.

Why this exists: §5 of `docs/evidence/2026-08-16-set2-connectors.md` asserted
three properties of `r6/upstream_connectors.py` — not as unit tests, but by
booting the real Flask app with a real environment and asking
`/r6/fhir/health` what it resolved. The two scripts behind it
(`registry-contract.sh` for cases 1 and 2, `halfconfig.sh` for case 3, which
the pack files as register entry R1) lived in an uncommitted scratch directory
and are gone. This is both of them, rewritten from the transcripts and
committed as one script, because they boot the same app and ask the same
endpoint (#602).

The three cases:

  1  FHIR_UPSTREAM_URL takes precedence over MEDPLUM_BASE_URL
  2  an unknown FHIR_UPSTREAM_KIND is refused, not guessed at
  3  a half-configured upstream must not report a healthy upstream while
     writes land in the local store  (R1)

Case 1 contacts a public FHIR server (read only, `/metadata` via the proxy's
own health check) so that the `software` field is the UPSTREAM naming itself
rather than our configuration restating itself. Cases 2 and 3 are entirely
local: case 3's upstream host is deliberately unroutable, because the point of
that case is that no client is ever built for it.

Usage:
  uv run python scripts/connector-registry-contract.py
  uv run python scripts/connector-registry-contract.py --repo /path/to/checkout
  uv run python scripts/connector-registry-contract.py --case 2

`--repo` exists so this can be run against an older tree: a difference between
two runs of the same script is evidence, where a difference between this run
and a transcript nobody can re-execute is an assumption.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CASE1_UPSTREAM = "https://server.fire.ly/R4"
CASE1_MEDPLUM_DECOY = "https://hapi.fhir.org/baseR4"
# Case 3's upstream is never contacted: get_proxy() refuses to build an OAuth2
# client with no secret, so nothing resolves this name. An unroutable host
# makes that visible — if a client were ever built, the health check would say
# 'unreachable' rather than 'misconfigured'/'not_configured'.
CASE3_MEDPLUM = "https://medplum.example.invalid/fhir"

# The exact sentence resolve_upstream_config raises on. Asserting on the
# message and not merely on "the app did not start" matters: a port collision
# also stops the app starting, and would otherwise read as this guard holding.
CASE2_MESSAGE = "is not one of"
CASE2_KIND = "totally-made-up"

TENANT = "desktop-demo"
STEP_UP_SECRET = "registry-contract-secret"


class Result:
    def __init__(self):
        self.failed = False
        self.ran: set[int] = set()

    def ok(self, msg):
        print(f"  PASS {msg}")

    def bad(self, msg):
        print(f"  FAIL {msg}")
        self.failed = True

    def note(self, msg):
        print(f"  NOTE {msg}")


def short(path: Path) -> str:
    """A path fit for a transcript: relative, never the operator's home.

    An absolute path here is the operator's OS username, and this repository
    has committed one into a public evidence pack before. Printing a relative
    path makes that impossible rather than something a reviewer has to catch.
    """
    try:
        return os.path.relpath(path)
    except ValueError:
        return path.name


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(repo: Path, port: int, db_path: str) -> dict:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("FHIR_UPSTREAM_", "MEDPLUM_")):
            del env[key]
    env.update(
        {
            "APP_ENV": "development",
            "STEP_UP_SECRET": STEP_UP_SECRET,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "PORT": str(port),
            "DISABLE_COMMAND_CENTER": "1",
        }
    )
    return env


def init_db(repo: Path, env: dict) -> str:
    """Create the schema. Without it the first write is an AuditWriteError."""
    proc = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "main", "init-db"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )
    return "" if proc.returncode == 0 else (proc.stderr or proc.stdout)


def boot(repo: Path, env: dict, capture: bool = False):
    return subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(repo),
        env=env,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        text=True,
    )


def http(url: str, method: str = "GET", body: bytes | None = None, headers=None):
    """Returns (status, parsed body or raw text). Never raises on 4xx/5xx."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except Exception as exc:
        return None, str(exc)
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw.decode(errors="replace")


def wait_for_health(port: int, proc, timeout: float = 60.0):
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/r6/fhir/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, None
        status, body = http(url)
        if status is not None:
            return status, body
        time.sleep(0.4)
    return None, None


def stop(proc):
    proc.terminate()
    try:
        return proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


# ---------------------------------------------------------------------------


def case1(repo: Path, r: Result, tmp: str):
    print("\nCase 1 — FHIR_UPSTREAM_URL takes precedence over MEDPLUM_BASE_URL")
    print(f"  FHIR_UPSTREAM_URL = {CASE1_UPSTREAM}")
    print(f"  MEDPLUM_BASE_URL  = {CASE1_MEDPLUM_DECOY}   (must be IGNORED)")
    print("  MEDPLUM_CLIENT_ID / _SECRET = must-not-be-used")

    port = free_port()
    env = base_env(repo, port, os.path.join(tmp, "case1.db"))
    env.update(
        {
            "FHIR_UPSTREAM_URL": CASE1_UPSTREAM,
            "MEDPLUM_BASE_URL": CASE1_MEDPLUM_DECOY,
            "MEDPLUM_CLIENT_ID": "must-not-be-used",
            "MEDPLUM_CLIENT_SECRET": "must-not-be-used",
        }
    )
    proc = boot(repo, env)
    try:
        status, body = wait_for_health(port, proc)
    finally:
        stop(proc)

    if status is None:
        r.bad("the app never answered /r6/fhir/health — case 1 measured nothing")
        return
    r.ran.add(1)

    up = (body or {}).get("checks", {}).get("upstream")
    print(f"\n  resolved -> mode={(body or {}).get('mode')!r} upstream={up!r}")

    if not isinstance(up, dict):
        # 'not_configured' or 'misconfigured'. Without this the case could
        # "pass" against an app in local mode, which resolves nothing at all.
        r.bad(
            f"checks.upstream is {up!r}, not a proxy health payload. No upstream "
            "was resolved, so precedence was not exercised."
        )
        return
    if up.get("status") != "connected":
        r.bad(
            f"the proxy could not reach the upstream (status {up.get('status')!r}). "
            "The 'software' field below would not be the upstream naming itself."
        )
        return

    if up.get("upstream_url") == CASE1_UPSTREAM:
        r.ok("FHIR_UPSTREAM_URL won; MEDPLUM_BASE_URL was not used")
    elif up.get("upstream_url") == CASE1_MEDPLUM_DECOY:
        r.bad("MEDPLUM_BASE_URL won. Precedence is REVERSED from what §5 recorded.")
    else:
        r.bad(f"resolved to {up.get('upstream_url')!r}, neither configured URL")

    if up.get("kind") == "generic":
        r.ok("kind is 'generic' — the MEDPLUM_* names did not imply the kind")
    else:
        r.bad(f"kind is {up.get('kind')!r}, not 'generic'")

    software = up.get("software")
    if software and software != "unknown":
        r.ok(f"the upstream names itself {software!r} in its CapabilityStatement")
    else:
        r.bad(
            f"software is {software!r} — this run has no confirmation from the "
            "upstream about which server was reached"
        )


def case2(repo: Path, r: Result, tmp: str):
    print("\nCase 2 — an unknown kind is refused rather than guessed at")
    print(f"  FHIR_UPSTREAM_KIND = {CASE2_KIND}")

    port = free_port()
    env = base_env(repo, port, os.path.join(tmp, "case2.db"))
    env.update({"FHIR_UPSTREAM_KIND": CASE2_KIND, "FHIR_UPSTREAM_URL": CASE1_UPSTREAM})
    proc = boot(repo, env, capture=True)
    status, body = wait_for_health(port, proc)
    out, err = stop(proc)
    combined = f"{out or ''}{err or ''}"

    if status is None and proc.returncode not in (0, None):
        # Today's behaviour. The message is the assertion: the app failing to
        # start for some OTHER reason must not read as this guard holding.
        r.ran.add(2)
        if CASE2_MESSAGE in combined and CASE2_KIND in combined:
            line = next(
                (ln for ln in combined.splitlines() if CASE2_MESSAGE in ln), ""
            )
            print(f"  process exited {proc.returncode} before binding a port")
            print(f"  {line.strip()[:160]}")
            r.ok("the app REFUSES TO START on an unknown kind, naming the kind")
            r.note(
                "§5 recorded the app BOOTING and answering 500 per request "
                "(register entry R6). This run differs."
            )
        else:
            r.bad(
                f"the app exited {proc.returncode} without the registry's message. "
                "Something else stopped it; this case proves nothing."
            )
        return

    if status is None:
        r.bad("the app neither started nor exited — case 2 measured nothing")
        return

    r.ran.add(2)
    print(f"  /r6/fhir/health -> HTTP {status}")
    text = body if isinstance(body, str) else json.dumps(body)
    if status >= 500 and (CASE2_MESSAGE in text or CASE2_MESSAGE in combined):
        # §5's transcript shows the ValueError under a "Traceback (most recent
        # call last)", which is the app's own stderr — Flask's 500 body carries
        # no traceback with debug off. Both are checked so the case reads the
        # same evidence §5 read.
        where = "the response body" if CASE2_MESSAGE in text else "the app's log"
        line = next(
            (ln for ln in f"{text}\n{combined}".splitlines() if CASE2_MESSAGE in ln), ""
        )
        print(f"  {line.strip()[:160]}")
        r.ok(f"the app boots and raises per request, as §5 recorded (R6); in {where}")
    elif status >= 500:
        r.bad(
            "the app 500s but neither the body nor the log carries the registry's "
            "message. This case cannot say the 500 came from the unknown kind."
        )
    else:
        # The security property. Anything that answers 2xx here has resolved
        # the typo to SOMETHING, and 'generic' means anonymous requests at a
        # record system.
        r.bad(
            f"/r6/fhir/health answered HTTP {status} with an unknown kind set. "
            f"body={text[:200]!r}. An unknown kind must not resolve."
        )


def case3(repo: Path, r: Result, tmp: str):
    print("\nCase 3 — MEDPLUM_BASE_URL with no credentials  (register entry R1)")
    print(f"  MEDPLUM_BASE_URL  = {CASE3_MEDPLUM}")
    print("  MEDPLUM_CLIENT_ID = set")
    print("  MEDPLUM_CLIENT_SECRET = MISSING")

    port = free_port()
    db_path = os.path.join(tmp, "case3.db")
    env = base_env(repo, port, db_path)
    env.update(
        {
            "MEDPLUM_BASE_URL": CASE3_MEDPLUM,
            "MEDPLUM_CLIENT_ID": "half-configured",
        }
    )
    err = init_db(repo, env)
    if err:
        r.bad(f"init-db failed, so the write half of R1 cannot run: {err[-200:]}")
        return
    proc = boot(repo, env)
    try:
        status, body = wait_for_health(port, proc)
        if status is None:
            r.bad("the app never answered /r6/fhir/health — case 3 measured nothing")
            return
        r.ran.add(3)
        body = body or {}
        health_status = body.get("status")
        mode = body.get("mode")
        upstream_check = body.get("checks", {}).get("upstream")
        print(f"\n  GET /r6/fhir/health -> HTTP {status}")
        print(
            f"  status = {health_status!r} | mode = {mode!r} | "
            f"checks.upstream = {upstream_check!r}"
        )

        base = f"http://127.0.0.1:{port}"
        tok_status, tok_body = http(
            f"{base}/r6/fhir/internal/step-up-token",
            method="POST",
            body=json.dumps({"tenant_id": TENANT}).encode(),
            headers={"Content-Type": "application/json", "X-Tenant-Id": TENANT},
        )
        token = (tok_body or {}).get("token") if isinstance(tok_body, dict) else None
        if not token:
            r.bad(
                f"could not mint a step-up token (HTTP {tok_status}). The write "
                "half of R1 was NOT measured this run."
            )
            return

        patient = json.dumps(
            {
                "resourceType": "Patient",
                "name": [{"family": "Halfconfig", "given": ["Register"]}],
                "birthDate": "1980-03-11",
            }
        ).encode()
        cr_status, created = http(
            f"{base}/r6/fhir/Patient",
            method="POST",
            body=patient,
            headers={
                "Content-Type": "application/fhir+json",
                "X-Tenant-Id": TENANT,
                "X-Step-Up-Token": token,
            },
        )
        pid = created.get("id") if isinstance(created, dict) else None
        if not pid:
            r.bad(
                f"the create returned HTTP {cr_status} and no id "
                f"({str(created)[:160]!r}). The write half of R1 was NOT measured."
            )
            return
        print(f"  POST /r6/fhir/Patient -> HTTP {cr_status}, id {pid}")

        rd_status, read = http(
            f"{base}/r6/fhir/Patient/{pid}",
            headers={"X-Tenant-Id": TENANT, "X-Step-Up-Token": token},
        )
        source = read.get("_source") if isinstance(read, dict) else "<unreadable>"
        print(f"  GET  /r6/fhir/Patient/{pid} -> HTTP {rd_status}, _source = {source!r}")
    finally:
        stop(proc)

    rows = None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT COUNT(*) FROM r6_resources WHERE resource_type = 'Patient'"
        ).fetchone()[0]
        conn.close()
    except Exception as exc:
        r.bad(f"could not count local rows ({exc}); where the write landed is unknown")
        return
    print(f"    r6_resources Patient rows = {rows}")

    if rows < 1:
        r.bad(
            "the create returned an id and the local store holds no Patient. "
            "This case cannot say where the write went."
        )
        return

    r.note(
        "the write landed in the proxy's own SQLite, not in a Medplum — "
        "unchanged from 2026-08-16, and the correct fallback."
    )

    # The property, stated as what must NOT be true: an operator reading the
    # health page must not be told the upstream is fine while writes are
    # landing somewhere else.
    if mode == "upstream" and health_status == "healthy":
        r.bad(
            "R1 STILL OPEN: /r6/fhir/health reports mode 'upstream' and status "
            "'healthy' while the write landed locally. A container healthcheck "
            "and any orchestrator probe both report this deployment fine."
        )
    elif health_status == "healthy":
        r.bad(
            f"health reports 'healthy' (mode {mode!r}) while a named upstream "
            "was never built and the write landed locally."
        )
    else:
        r.ok(
            f"R1 CLOSED: health reports HTTP {status}, status {health_status!r}, "
            f"mode {mode!r}, checks.upstream {upstream_check!r} — a named upstream "
            "that could not be built is visible to a probe."
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO), help="checkout to boot")
    ap.add_argument(
        "--case", type=int, choices=(1, 2, 3), action="append", help="run one case"
    )
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / "main.py").is_file():
        print(f"no main.py under {short(repo)} — nothing to boot")
        return 2

    wanted = sorted(set(args.case)) if args.case else [1, 2, 3]

    print("connector-registry-contract.py")
    print(f"date  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"repo  {short(repo)}")
    print(f"cases {wanted}")
    print("baseline docs/evidence/2026-08-16-set2-connectors.md §5 and R1")

    r = Result()
    with tempfile.TemporaryDirectory() as tmp:
        for n in wanted:
            {1: case1, 2: case2, 3: case3}[n](repo, r, tmp)

    print("\nResult")
    missing = [n for n in wanted if n not in r.ran]
    if missing:
        # Without this the script can print no FAIL line and exit 0 having
        # never got an app up. A case that did not run is not a case that passed.
        print(f"  case(s) {missing} never produced a measurement")
        r.failed = True
    print("  no assertion failed" if not r.failed else "  at least one assertion failed")
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
