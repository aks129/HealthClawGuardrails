#!/usr/bin/env python3
"""What Authorization header the proxy actually puts on the wire, per kind.

Why this exists: §6 of `docs/evidence/2026-08-16-set2-connectors.md` measured
the single highest-severity finding of that pass — R5, "the `hapi` connector
silently drops the credentials its own summary tells operators to set" — by
pointing the proxy at a local HTTP server that records the Authorization header
it receives, with identical credentials set, changing only the kind. That
script (`auth_probe.py`) lived in an uncommitted scratch directory and is gone,
so the finding could not be re-run by anyone. This is it, rewritten from the
transcript and committed (#602).

The transcript does not say WHICH request the proxy was made to send. This
script sends the health check, because `/r6/fhir/health` reaches the upstream
through `FHIRUpstreamProxy.healthy()` on the same client that carries
`basic_auth`, needs no step-up token and creates nothing. That is a
reconstruction decision, recorded here and in the rerun evidence rather than
left implicit.

It boots the real app twice, against a recording server on loopback. No public
server is touched and nothing is created anywhere.

Usage:
  uv run python scripts/connector-auth-probe.py                 # the comparison
  uv run python scripts/connector-auth-probe.py --no-credential # negative control
  uv run python scripts/connector-auth-probe.py --repo /path/to/another/checkout

`--repo` exists so this measurement can be taken against an older tree. A
difference between two runs of the same script is evidence; a difference
between this run and a transcript nobody can re-execute is an assumption.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The credentials §6 used. The client id is asserted on, so a Basic header the
# proxy invented from somewhere else would not pass.
CLIENT_ID = "set2-probe-client"
CLIENT_SECRET = "set2-probe-secret"

# What 2026-08-16 recorded, for the comparison this script exists to make.
BASELINE = {"hapi": None, "generic": "Basic"}

KINDS = ("hapi", "generic")


class Recorder(BaseHTTPRequestHandler):
    """A FHIR server that answers /metadata and remembers who asked."""

    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        Recorder.seen.append(
            {"path": self.path, "authorization": self.headers.get("Authorization")}
        )
        body = json.dumps(
            {
                "resourceType": "CapabilityStatement",
                "fhirVersion": "4.0.1",
                "software": {"name": "auth-probe recorder"},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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


def boot_app(kind, upstream, port, db_path, with_credential, repo):
    """Start the real app in upstream mode. Returns the process."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("FHIR_UPSTREAM_", "MEDPLUM_")):
            del env[key]
    env.update(
        {
            "FHIR_UPSTREAM_KIND": kind,
            "FHIR_UPSTREAM_URL": upstream,
            "APP_ENV": "development",
            "STEP_UP_SECRET": "auth-probe-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "PORT": str(port),
            "DISABLE_COMMAND_CENTER": "1",
        }
    )
    if with_credential:
        env["FHIR_UPSTREAM_CLIENT_ID"] = CLIENT_ID
        env["FHIR_UPSTREAM_CLIENT_SECRET"] = CLIENT_SECRET
    return subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(repo),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_health(port: int, proc, timeout: float = 45.0) -> dict | None:
    """Poll /r6/fhir/health until it answers. Returns the parsed body."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/r6/fhir/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:  # 503 degraded is still an answer
            return json.loads(exc.read())
        except Exception:
            time.sleep(0.4)
    return None


def describe(auth: str | None) -> str:
    if auth is None:
        return "None (NO credential sent)"
    scheme = auth.split(" ", 1)[0]
    return f"{scheme} <redacted>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo",
        default=str(REPO),
        help="checkout to boot (default: this one)",
    )
    ap.add_argument(
        "--no-credential",
        action="store_true",
        help="negative control: run with no credentials set at all",
    )
    args = ap.parse_args()
    with_credential = not args.no_credential
    repo = Path(args.repo).resolve()
    if not (repo / "main.py").is_file():
        print(f"no main.py under {short(repo)} — nothing to boot")
        return 2

    server = HTTPServer(("127.0.0.1", 0), Recorder)
    upstream = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("connector-auth-probe.py")
    print(f"date      {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"repo      {short(repo)}")
    print(f"recorder  {upstream}")
    print(f"request   GET /r6/fhir/health -> proxy GET {upstream}/metadata")
    print("")
    print("Identical env for both cases:")
    if with_credential:
        print(f"  FHIR_UPSTREAM_CLIENT_ID     = {CLIENT_ID}")
        print("  FHIR_UPSTREAM_CLIENT_SECRET = <set>")
    else:
        print("  FHIR_UPSTREAM_CLIENT_ID     = <unset>   (negative control)")
        print("  FHIR_UPSTREAM_CLIENT_SECRET = <unset>   (negative control)")
    print("Question: what Authorization header reaches the upstream?")
    print("")

    failed = False
    observed: dict[str, str | None] = {}
    identity_bad: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp:
        for kind in KINDS:
            Recorder.seen.clear()
            port = free_port()
            db_path = os.path.join(tmp, f"{kind}.db")
            proc = boot_app(kind, upstream, port, db_path, with_credential, repo)
            try:
                health = wait_for_health(port, proc)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()

            if health is None:
                print(f"  kind={kind:<8} -> FAIL the app never answered /r6/fhir/health")
                failed = True
                continue

            metadata = [r for r in Recorder.seen if r["path"].startswith("/metadata")]
            if not metadata:
                # The trap this guard exists for: "Authorization: None" and
                # "the proxy never called the upstream at all" print the same
                # way, and only one of them is a measurement.
                print(
                    f"  kind={kind:<8} -> FAIL the recorder saw no /metadata request. "
                    f"Nothing was measured for this kind. "
                    f"(health mode={health.get('mode')!r}, "
                    f"upstream={health.get('checks', {}).get('upstream')!r})"
                )
                failed = True
                continue

            auth = metadata[-1]["authorization"]
            observed[kind] = auth
            print(f"  kind={kind:<8} -> Authorization: {describe(auth)}")

            if auth is not None and auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
                except Exception:
                    decoded = ""
                sent_id = decoded.split(":", 1)[0]
                if sent_id != CLIENT_ID:
                    print(
                        f"           FAIL the Basic header carries {sent_id!r}, "
                        f"not the configured {CLIENT_ID!r}"
                    )
                    identity_bad.add(kind)
                    failed = True

    server.shutdown()

    if not with_credential:
        print("")
        print("Negative control. With no credentials set, both kinds must send")
        print("nothing: AUTH_BASIC degrades to anonymous rather than inventing a")
        print("header. A 'Basic' here would mean this script reads a credential")
        print("from somewhere other than the environment it sets.")
        for kind in KINDS:
            if observed.get(kind) is not None:
                print(f"  FAIL kind={kind} sent {describe(observed[kind])} with no credential configured")
                failed = True
        if not failed:
            print("  PASS neither kind sent an Authorization header")
        return 1 if failed else 0

    print("")
    print("Against 2026-08-16 (§6 of docs/evidence/2026-08-16-set2-connectors.md):")
    for kind in KINDS:
        if kind not in observed:
            print(f"  kind={kind:<8} NOT MEASURED this run — no comparison is possible")
            failed = True
            continue
        auth = observed[kind]
        now = None if auth is None else auth.split(" ", 1)[0]
        was = BASELINE[kind]
        if now == was:
            print(f"  kind={kind:<8} same as 2026-08-16 ({was or 'no credential sent'})")
        else:
            print(
                f"  kind={kind:<8} DIFFERS: 2026-08-16 sent "
                f"{was or 'no credential'}, today sends {now or 'no credential'}"
            )

    # The verdict is only available when both kinds actually produced a header
    # this run. Without this the "no header seen" and "the upstream was never
    # reached" cases print the same R5 verdict, and a mutation that pointed the
    # proxy at a dead port reported "R5 STILL OPEN" having measured nothing —
    # the control that looks like one thing and quietly does two, again.
    print("")
    if set(observed) != set(KINDS) or identity_bad:
        print("  NO VERDICT ON R5. Not every kind produced a header this run,")
        print("  so nothing here says whether hapi sends its credential.")
        failed = True
    elif observed.get("hapi") is None:
        print("  R5 STILL OPEN: hapi accepts the credentials its summary asks for")
        print("  and does not send them.")
        failed = True
    elif observed.get("generic") is None:
        print("  FAIL generic sent no credential either. The comparison §6 makes")
        print("  needs generic as its control; without it the hapi row means nothing.")
        failed = True
    else:
        print("  R5 CLOSED on the wire: both kinds send the configured credential.")

    print("")
    print("no assertion failed" if not failed else "at least one assertion failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
