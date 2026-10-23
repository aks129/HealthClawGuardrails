"""CareAgents release checks: confirm a deploy landed, and notice drift.

    python scripts/careagents_release.py wait --sha <12-hex> [--timeout 900]
    python scripts/careagents_release.py drift [--grace 7200]

`wait` is the last step of .github/workflows/careagents-deploy.yml. It polls
/healthz until the web service reports the build that was just uploaded AND
`run_workers: true` (the worker is draining). `railway up --detach` returns as
soon as the archive is accepted, so the upload succeeding says nothing about
the deploy succeeding; this is the finish line the runbook defines.

`drift` runs hourly from .github/workflows/careagents-drift.yml. Production was
once 22 commits behind `main` because CareAgents deploys by hand. This compares
the deployed build with the newest `main` commit that touches CareAgents paths
and says whether production has been behind it for longer than the grace.

Exit codes for `drift`, read by the workflow's issue sync:
    0  current: the newest CareAgents commit is in the deployed build
    1  behind for longer than the grace: raise or keep the alarm
    2  behind, but within the grace (a deploy may be rolling out): no change
    3  could not check (healthz unreachable, git failed): no change

Standard library only, so the workflow needs no dependency install.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HEALTHZ = "https://careagents.cloud/healthz"

# Mirrors deploy/careagents/Dockerfile's build context, which is what the
# deploy workflow triggers on. A commit outside these paths cannot change what
# production runs, so it cannot make production stale.
CAREAGENTS_PATHS = ("careagents", "deploy/careagents", "pyproject.toml", "uv.lock")

CURRENT, BEHIND, WITHIN_GRACE, UNKNOWN = 0, 1, 2, 3

_SHA = re.compile(r"\A[0-9a-f]{7,40}\Z")
_MARKER = re.compile(r"\A[0-9a-f]{7,40}(-dirty)?\Z")


def clean_build(value: object) -> str:
    """The /healthz `build` value, or "unknown" if it is not a deploy marker.

    It comes from the network and ends up in a public issue body, inside a
    code fence and an HTML comment. Anything that is not a sha (optionally
    `-dirty`) is replaced before it is printed anywhere.
    """
    text = value if isinstance(value, str) else ""
    return text if _MARKER.match(text) else "unknown"


def fetch_healthz(url: str = HEALTHZ, timeout: float = 10.0) -> dict:
    """GET /healthz and return its JSON. A 503 still carries the body."""
    request = urllib.request.Request(url, headers={"User-Agent": "careagents-release"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:  # 503 = degraded, body says why
        return json.loads(err.read().decode("utf-8"))


def same_build(deployed: str, sha: str) -> bool:
    """True when the deployed marker names this commit.

    The marker is a short sha (12 hex) and `sha` may be short or full, so this
    is a prefix match in whichever direction is shorter. `-dirty` and
    `unknown` match nothing, on purpose: neither is a commit we shipped.
    """
    if not (_SHA.match(deployed or "") and _SHA.match(sha or "")):
        return False
    shorter, longer = sorted((deployed, sha), key=len)
    return longer.startswith(shorter)


def landed(health: dict, sha: str) -> bool:
    """The deploy is done: the right build, and a worker is draining."""
    return same_build(str(health.get("build", "")), sha) and health.get("run_workers") is True


def wait(sha: str, timeout: float, interval: float, fetch=fetch_healthz,
         clock=time.monotonic, sleep=time.sleep) -> int:
    deadline = clock() + timeout
    last: object = None
    while True:
        try:
            health = fetch()
        except Exception as err:  # noqa: BLE001 - mid-rollout blips are expected
            health = {"error": type(err).__name__}
        if isinstance(health, dict) and "build" in health:
            health = {**health, "build": clean_build(health["build"])}
        if health != last:
            print(f"healthz: {json.dumps(health, sort_keys=True)}", flush=True)
            last = health
        if landed(health, sha):
            print(f"deployed: build {health['build']} with run_workers true")
            return 0
        if clock() >= deadline:
            print(f"timed out after {int(timeout)}s waiting for build {sha} "
                  "with run_workers true", file=sys.stderr)
            return 1
        sleep(interval)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def latest_careagents_commit(ref: str = "origin/main") -> tuple[str, int]:
    """(full sha, commit unix time) of the newest commit on `ref` touching
    CareAgents paths. Raises RuntimeError when git cannot answer."""
    out = _git("log", "-1", "--format=%H %ct", ref, "--", *CAREAGENTS_PATHS)
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 2:
        raise RuntimeError(f"git log failed: {out.stderr.strip() or out.stdout.strip()}")
    return parts[0], int(parts[1])


def is_ancestor(ancestor: str, descendant: str) -> bool | None:
    """True/False from `git merge-base --is-ancestor`; None when git cannot
    resolve one of the commits (exit codes other than 0 and 1)."""
    result = _git("merge-base", "--is-ancestor", ancestor, descendant).returncode
    return {0: True, 1: False}.get(result)


def drift_verdict(deployed: str, latest: str, latest_time: int, now: float,
                  grace: float, contains, on_main) -> tuple[int, str]:
    """Decide drift. `contains(latest, deployed)` answers whether the deployed
    build includes `latest`; `on_main(deployed)` whether the build is a commit
    on `main`. Each returns True/False, or None when git cannot resolve it.

    A build that includes the newest CareAgents commit is current even when it
    is not that commit: a manual deploy from a later `main` tip ships the same
    CareAgents code. It must also BE on `main`: a build from an unmerged branch
    descends from main too, and is the unreviewed code this alarm exists to
    catch. A build that is `unknown`, `-dirty`, or not on `main` counts as
    behind (#258).
    """
    if same_build(deployed, latest):
        return CURRENT, f"production runs {deployed}, the newest CareAgents commit"
    if _SHA.match(deployed or ""):
        included = contains(latest, deployed)
        if included and on_main(deployed):
            return CURRENT, (f"production runs {deployed}, which includes the "
                             f"newest CareAgents commit {latest[:12]}")
        why = "predates it" if included is False else "is not a commit on main"
    else:
        why = "is not a commit we shipped"
    age = max(0, int(now - latest_time))
    detail = (f"production runs {deployed or 'nothing'} ({why}); the newest "
              f"CareAgents commit on main is {latest[:12]}, committed "
              f"{age // 60} minutes ago")
    return (BEHIND if age > grace else WITHIN_GRACE), detail


def drift(grace: float, ref: str, fetch=fetch_healthz, now=time.time) -> int:
    try:
        health = fetch()
        deployed = clean_build(health.get("build"))
    except Exception as err:  # noqa: BLE001 - reported, never a verdict
        print(f"could not read {HEALTHZ}: {type(err).__name__}: {err}")
        return UNKNOWN
    try:
        latest, latest_time = latest_careagents_commit(ref)
    except RuntimeError as err:
        print(f"could not read main: {err}")
        return UNKNOWN
    code, detail = drift_verdict(deployed, latest, latest_time, now(), grace,
                                 is_ancestor, lambda sha: is_ancestor(sha, ref))
    label = {CURRENT: "current", BEHIND: "BEHIND", WITHIN_GRACE: "behind, within grace"}[code]
    print(f"{label}: {detail}")
    print(f"deployed={deployed}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("wait", help="poll /healthz until a deploy has landed")
    w.add_argument("--sha", required=True)
    w.add_argument("--timeout", type=float, default=900)
    w.add_argument("--interval", type=float, default=15)
    d = sub.add_parser("drift", help="is production behind main?")
    d.add_argument("--grace", type=float, default=7200)
    d.add_argument("--ref", default="origin/main")
    args = parser.parse_args(argv)
    if args.command == "wait":
        return wait(args.sha, args.timeout, args.interval)
    return drift(args.grace, args.ref)


if __name__ == "__main__":
    sys.exit(main())
