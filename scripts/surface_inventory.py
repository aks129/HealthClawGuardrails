#!/usr/bin/env python3
"""Deployed-surface inventory — what is out there, and is anything watching it?

    uv run --with requests python scripts/surface_inventory.py
    uv run --with requests python scripts/surface_inventory.py --json-out out.json

Why this exists
---------------
#624 was an abandoned CareAgents instance still serving on an old VPS: months
behind `main`, holding an accounts store, holding a working credential to the
engine, and watched by nothing. It was found because somebody happened to ask
whether a deploy script was still used. Nothing in this repository could have
answered "what else is like that?" — so this does.

It enumerates every host this repository names as a *deployed surface*, probes
each one read-only, and sorts the answers into three groups:

  * **live and watched**   — answering, and `scripts/prod_watch.py` requests it
  * **live and unwatched** — answering, and nothing requests it (the #624 shape)
  * **referenced but dead** — named here, not answering (a stale reference)

Two rules it follows, both learned the hard way
----------------------------------------------
**Watched is measured, not assumed.** The watched set is not a copy of
prod_watch's constants. This imports that module, replaces its `get`/`post`
with recorders, runs it offline, and takes the URLs it actually requested. A
constant named `CAREAGENTS` proves nothing about which host a check named
"careagents:" measures — and in fact it measures the platform hostname, never
the domain users type. Reading names would have reproduced that error.

**DNS on a developer machine is not evidence.** This LAN answers port 53 from
a stale cache (operator notes, not published here). Every name here is resolved
over DoH *and* through the system resolver, and a disagreement is reported
rather than silently picked. It matters most for the one host where a wrong
answer flips the finding: if `careagents.cloud` still pointed at the retired
VPS, a probe of the name would be a probe of the abandoned box.

What it deliberately does NOT do
--------------------------------
No credential is ever sent, no method other than GET is used, no endpoint is
retried, and one request is made per endpoint. Several of these hosts belong
to other people. A non-answer is recorded as a non-answer.

It therefore cannot see anything behind authentication, cannot see a surface
this repository does not name, and cannot see a port nobody wrote down. Those
gaps are stated in the evidence document rather than papered over: a monitor
that quietly checks less than it appears to is worse than one with a narrow,
stated scope.

Redaction
---------
The retired VPS is addressed by IP in `deploy/careagents/deploy.sh`. That
address is read at runtime and **never printed** — every string this script
emits passes through `_scrub()`, which replaces it with a placeholder. The
report says whether a name resolves to it, which is the finding, without
restating it.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
UA = "healthclaw-surface-inventory/1.0 (+https://github.com/aks129/HealthClawGuardrails)"

G, R, Y, B, D, X = ("\033[92m", "\033[91m", "\033[93m", "\033[94m",
                    "\033[2m", "\033[0m")

# --- groups ------------------------------------------------------------------
OURS = "ours"          # we deploy it, or we own the name
UPSTREAM = "upstream"  # someone else's service this code or these docs call

LIVE_WATCHED = "live and watched"
LIVE_UNWATCHED = "live and unwatched"
DEAD = "referenced but dead"

#: Every surface this repository names as deployed, with the reason it is in
#: scope. `probe` is the single endpoint this script GETs — one per surface.
#:
#: An entry earns its place by being a thing that answers on the network. Code
#: identifier URIs (hl7.org, loinc.org, snomed.info, unitsofmeasure.org),
#: package registries and CDNs, badge and link-out hosts, and every
#: example/invalid/localhost/docker name are excluded on purpose; the exclusion
#: list is in the evidence document so the omissions are visible rather than
#: silent.
SURFACES: tuple[dict, ...] = (
    # --- ours ---------------------------------------------------------------
    {"name": "healthclaw engine (stateful)", "group": OURS,
     "probe": "https://app.healthclaw.io/r6/fhir/health",
     "why": "the guardrail engine; deployment.py STATEFUL_HOST"},
    {"name": "healthclaw public site / demo", "group": OURS,
     "probe": "https://healthclaw.io/",
     "why": "Vercel read-only copy; app.py PUBLIC_SITE_URL default"},
    {"name": "healthclaw MCP subdomain", "group": OURS,
     "probe": "https://mcp.healthclaw.io/mcp",
     "why": "proposed hosted MCP name, docs/specs/2026-08-16-mcp-authorization.md"},
    {"name": "smart health links viewer", "group": OURS,
     "probe": "https://shl.healthclaw.io/",
     "why": "viewer/manage links published by skills/share-health-qr"},
    {"name": "healthclaw legacy railway name", "group": OURS,
     "probe": "https://healthclaw.up.railway.app/r6/fhir/health",
     "why": "MCP url in skills/personal-health-records and the quickstart PDF"},
    {"name": "agent-skills discovery document", "group": OURS,
     "probe": "https://healthclaw.io/.well-known/agent-skills/index.json",
     "why": "RFC 8615 skill catalogue app.py serves to any spec client"},
    {"name": "careagents consumer domain", "group": OURS,
     "probe": "https://careagents.cloud/healthz",
     "why": "CARE_ORIGIN / CARE_RP_ID — the host a person's browser sees"},
    {"name": "careagents platform host", "group": OURS,
     "probe": "https://careagents-production.up.railway.app/healthz",
     "why": "the CareAgents deployment prod_watch measures"},
    {"name": "mcp server (token-locked)", "group": OURS,
     "probe": "https://mcp-server-production-5112.up.railway.app/health",
     "why": "server.json remote; production tool surface"},
    {"name": "mcp server (public demo)", "group": OURS,
     "probe": "https://mcp-demo-production-ee2c.up.railway.app/health",
     "why": "server.json remote, gemini-extension.json, every quickstart"},
    # --- upstreams and partner surfaces -------------------------------------
    {"name": "HAPI FHIR public R4", "group": UPSTREAM,
     "probe": "https://hapi.fhir.org/baseR4/metadata?_summary=true",
     "why": "FHIR_UPSTREAM_URL example throughout README/INTEGRATION"},
    {"name": "SMART Health IT R4", "group": UPSTREAM,
     "probe": "https://r4.smarthealthit.org/metadata?_summary=true",
     "why": "documented upstream, r6/fhir_proxy.py"},
    {"name": "Firely public server", "group": UPSTREAM,
     "probe": "https://server.fire.ly/R4/metadata?_summary=true",
     "why": "the `generic` connector's proven-live server (set-2 evidence)"},
    {"name": "Medplum hosted API", "group": UPSTREAM,
     "probe": "https://api.medplum.com/fhir/R4/metadata?_summary=true",
     "why": "_MEDPLUM_HOSTED_TOKEN_ENDPOINT, r6/fhir_proxy.py:50"},
    {"name": "tx.fhir.org terminology", "group": UPSTREAM,
     "probe": "https://tx.fhir.org/r4/metadata?_summary=true",
     "why": "TX_FHIR_ORG, r6/curatr.py:29"},
    {"name": "Fasten Connect API", "group": UPSTREAM,
     "probe": "https://api.connect.fastenhealth.com/v1",
     "why": "FASTEN_API_BASE default, r6/fasten/api.py:19"},
    {"name": "MEDENT FHIR", "group": UPSTREAM,
     "probe": "https://fhir.medent.com/fhir/R4/metadata?_summary=true",
     "why": "_MEDENT_FHIR_BASE, scripts/export_medent_fhir.py:28"},
    {"name": "HealthEx MCP", "group": UPSTREAM,
     "probe": "https://api.healthex.io/mcp",
     "why": "HEALTHEX_MCP_URL default, scripts/export_healthex_legacy.py"},
    {"name": "Health Bank One MCP", "group": UPSTREAM,
     "probe": "https://mcp.app.healthbankone.com/mcp",
     "why": "HBO_MCP_URL default, openclaw/bot.py:109; .mcp.json"},
    {"name": "Health Bank One OAuth", "group": UPSTREAM,
     "probe": "https://oauth.app.healthbankone.com/",
     "why": "scripts/healthbankone_oauth.py"},
    {"name": "PromptOpinion marketplace", "group": UPSTREAM,
     "probe": "https://app.promptopinion.ai/marketplace",
     "why": "listing linked from templates/index.html, faq.html, wiki.html"},
    {"name": "SHARP-on-MCP spec site", "group": UPSTREAM,
     "probe": "https://sharponmcp.com/",
     "why": "the contract the MCP server advertises conformance to"},
    {"name": "ClawHub skill listing", "group": UPSTREAM,
     "probe": "https://clawhub.ai/aks129/skills/fhir-r6-guardrails",
     "why": "README distribution channel for 14 skills"},
    {"name": "agentskills.io schema host", "group": UPSTREAM,
     "probe": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
     "why": "_SKILLS_DISCOVERY_SCHEMA, app.py:339 — served in our own JSON"},
)

# The retired VPS. Addressed by IP in the deploy script, so the address is read
# from there at runtime and scrubbed out of everything printed. Probed by
# presenting the hostname to that address, which is exactly how #624 reached it.
VPS_DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "careagents" / "deploy.sh"
VPS_SNI_HOST = "careagents.cloud"
VPS_PLACEHOLDER = "<vps-address-redacted>"

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def vps_address() -> str | None:
    """The default host in the CareAgents VPS deploy script, if it is still there.

    Returned only so the probe can dial it and the DNS comparison can test
    against it. It must not reach any output stream — `_scrub` enforces that.
    """
    if not VPS_DEPLOY_SCRIPT.is_file():
        return None
    text = VPS_DEPLOY_SCRIPT.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'HOST="\$\{1:-[^@"]*@([^"}]+)\}"', text)
    if not match:
        return None
    candidate = match.group(1).strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        # A hostname rather than an address is not a secret, but it is also not
        # what this branch is for; treat only literal addresses as the target.
        return None
    return candidate


_SECRET_ADDRESS = vps_address()


def _scrub(text: str) -> str:
    """Remove the retired box's address from anything on its way out.

    Deliberately blunt: the address, and any address at all in a probe body we
    do not control. The cost of over-redacting a report is a reader running one
    more command; the cost of under-redacting is publishing an operator's
    infrastructure into a public repository, which this repo has done before.
    """
    if not text:
        return text
    if _SECRET_ADDRESS:
        text = text.replace(_SECRET_ADDRESS, VPS_PLACEHOLDER)
    return text


def out(line: str = "") -> None:
    print(_scrub(line))


# --- probing -----------------------------------------------------------------

def probe(url: str, timeout: float) -> dict:
    """One GET. No redirect following, no retry, no credential.

    `allow_redirects=False` because a 301 to a parking page is a different
    finding from a 200, and following it would erase the difference.
    """
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False,
                         headers={"User-Agent": UA, "Accept": "*/*"})
    except requests.RequestException as exc:
        return {"reached": False, "error": type(exc).__name__,
                "status": None, "server": None, "location": None, "body": ""}
    return {
        "reached": True,
        "error": None,
        "status": r.status_code,
        "server": r.headers.get("Server") or r.headers.get("server"),
        "location": r.headers.get("Location"),
        "body": (r.text or "")[:600],
    }


def answering(result: dict) -> bool:
    """Is something serving here?

    A 401/403/404 from a server is a server. Only a transport failure, or a
    platform's own "this deployment does not exist" page, counts as dead —
    the second because a Vercel 404 for an unclaimed name is served by Vercel,
    not by us, and calling that "live" would hide exactly the stale reference
    this exercise is looking for.
    """
    if not result.get("reached"):
        return False
    body = result.get("body") or ""
    if "DEPLOYMENT_NOT_FOUND" in body or "Application not found" in body:
        return False
    if "Application failed to respond" in body:
        return False
    return True


# --- what does the monitor actually request? ---------------------------------

def watched_urls() -> list[str]:
    """The URLs `scripts/prod_watch.py` requests, observed rather than read.

    Imports the monitor, swaps its two HTTP helpers for recorders, and runs it
    with no network. The stub returns a string, so every `getattr(r,
    "status_code", None)` in that module yields None and the run completes and
    reports failures — which is fine, the verdicts are discarded. Only the URLs
    matter.

    This is the whole reason the inventory can be trusted about "watched": the
    module's constants are named for products, and a check named "careagents:"
    turns out to request a hostname no user ever types.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import prod_watch  # noqa: E402  (deliberate: the stub must precede the run)

    seen: list[str] = []

    def recorder(url, timeout, **kw):
        seen.append(url)
        return "surface-inventory-stub"

    original_get, original_post = prod_watch.get, prod_watch.post
    try:
        prod_watch.get = recorder
        prod_watch.post = recorder
        # Never mind the exit code; a run with no network fails every check.
        # Its report is swallowed rather than printed: eleven FAIL lines at the
        # top of an inventory read as the inventory failing, and they are an
        # artefact of stubbing the network, not a finding about production.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            prod_watch.run(1.0, [])
    finally:
        prod_watch.get, prod_watch.post = original_get, original_post
    return seen


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --- DNS, over a resolver that is not this network's -------------------------

def doh(name: str, rrtype: str) -> list[str]:
    try:
        r = requests.get("https://dns.google/resolve", timeout=10,
                         params={"name": name, "type": rrtype},
                         headers={"User-Agent": UA})
        payload = r.json()
    except (requests.RequestException, ValueError):
        return []
    return [a.get("data", "") for a in (payload.get("Answer") or [])
            if a.get("type") in (1, 5)]


def nameservers(name: str) -> list[str]:
    """Who is authoritative — the cheapest honest label for 'which platform'."""
    labels = name.split(".")
    for start in range(len(labels) - 1):
        zone = ".".join(labels[start:])
        try:
            r = requests.get("https://dns.google/resolve", timeout=10,
                             params={"name": zone, "type": "NS"},
                             headers={"User-Agent": UA})
            payload = r.json()
        except (requests.RequestException, ValueError):
            return []
        answers = [a.get("data", "").rstrip(".")
                   for a in (payload.get("Answer") or []) if a.get("type") == 2]
        if answers:
            return sorted(answers)
    return []


def system_addresses(name: str) -> list[str]:
    try:
        info = socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return sorted({i[4][0] for i in info})


def dns_facts(name: str) -> dict:
    """Both resolvers, compared, with no address in the result.

    Addresses are compared and then dropped. What a reader needs is (a) do the
    two resolvers agree, (b) does this name point at the retired box, and (c)
    who runs the zone. An address list adds none of that and is the one class
    of string this report must not carry.
    """
    doh_a = sorted({a for a in doh(name, "A")
                    if _IPV4.fullmatch(a or "")})
    sys_a = sorted(set(system_addresses(name)))
    return {
        "resolves": bool(doh_a),
        "resolvers_agree": (set(doh_a) == set(sys_a)) if (doh_a and sys_a)
                           else None,
        "points_at_retired_vps": (
            None if not (doh_a and _SECRET_ADDRESS)
            else _SECRET_ADDRESS in doh_a),
        "nameservers": nameservers(name),
    }


# --- build evidence ----------------------------------------------------------

def build_evidence(url: str, result: dict) -> str:
    """Anything the surface says about WHICH build it is.

    "Answering" and "current" are different findings and #624 was the second:
    the box answered perfectly while running code from months earlier. A
    payload with no build marker at all is itself evidence — it predates the
    stamping this repo added in #258.
    """
    if not result.get("reached"):
        return ""
    body = result.get("body") or ""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        bits = []
        for key in ("build", "built_at", "version", "software", "fhirVersion",
                    "status", "grade", "accounts", "run_workers", "provider",
                    "database"):
            if key in payload:
                bits.append(f"{key}={payload[key]!r}")
        if bits:
            return "; ".join(bits)
    server = result.get("server")
    return f"Server: {server}" if server else "no build marker in the response"


# --- reference scan ----------------------------------------------------------

_SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__", ".venv",
              ".ruff_cache", ".pytest_cache", "build", ".claude"}
_SCAN_SUFFIXES = {".py", ".ts", ".js", ".md", ".json", ".yml", ".yaml",
                  ".html", ".toml", ".sh", ".service", ".txt", ".example"}


def references(host: str, limit: int = 4) -> list[str]:
    """Where the repository names this host, as file:line.

    Computed at run time rather than written down, so the inventory cannot
    drift from the tree the way a hand-maintained list does.
    """
    hits: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        # Relative parts, not absolute. An absolute comparison silently
        # matched every file when the repo itself sits under a skipped name
        # (a `.claude/worktrees/...` checkout), so this returned nothing at
        # all and the "computed at run time" claim was empty.
        if _SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, 1):
            if host in line:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{index}")
                break
        if len(hits) >= limit:
            break
    return hits


# --- the retired VPS ---------------------------------------------------------

def probe_retired_vps(timeout: float) -> dict | None:
    """One GET to the address in the deploy script, presenting the hostname.

    Uses curl's `--resolve` rather than the system resolver, because the point
    is to reach that specific box regardless of where the name currently
    points. Read-only, single request, no credential.
    """
    if not _SECRET_ADDRESS:
        return None
    cmd = ["curl", "-sS", "--max-time", str(int(timeout)),
           "--resolve", f"{VPS_SNI_HOST}:443:{_SECRET_ADDRESS}",
           "-o", "-", "-w", "\n__STATUS__%{http_code}",
           "-A", UA, f"https://{VPS_SNI_HOST}/healthz"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"reached": False, "error": type(exc).__name__, "status": None,
                "server": None, "location": None, "body": ""}
    raw = proc.stdout or ""
    status = None
    body = raw
    if "__STATUS__" in raw:
        body, _, code = raw.rpartition("__STATUS__")
        status = int(code.strip()) if code.strip().isdigit() else None
    if not status:
        return {"reached": False,
                "error": (proc.stderr or "curl failed").strip()[:120],
                "status": None, "server": None, "location": None, "body": ""}
    return {"reached": True, "error": None, "status": status, "server": None,
            "location": None, "body": body.strip()[:600]}


# --- main --------------------------------------------------------------------

def classify(result: dict, watched: bool) -> str:
    if not answering(result):
        return DEAD
    return LIVE_WATCHED if watched else LIVE_UNWATCHED


def run(timeout: float) -> dict:
    watched_list = watched_urls()
    watched_hosts = {host_of(u) for u in watched_list}

    out(f"{B}What scripts/prod_watch.py actually requests{X} "
        f"{D}(observed, not read from its constants){X}")
    for url in watched_list:
        out(f"  {url}")
    out(f"  {D}→ {len(watched_hosts)} host(s): "
        f"{', '.join(sorted(watched_hosts))}{X}")
    out()

    rows: list[dict] = []
    for surface in SURFACES:
        url = surface["probe"]
        host = host_of(url)
        result = probe(url, timeout)
        is_watched = host in watched_hosts
        row = {
            "name": surface["name"],
            "group": surface["group"],
            "host": host,
            "probe": url,
            "why": surface["why"],
            "status": result["status"],
            "error": result["error"],
            "location": _scrub(result["location"] or ""),
            "answering": answering(result),
            "watched": is_watched,
            "verdict": classify(result, is_watched),
            "build": _scrub(build_evidence(url, result)),
            "references": references(host),
        }
        # DNS for everything we own, and for anything that did not answer:
        # "the name does not exist" and "the name exists and refused" are
        # different stale references with different owners.
        if surface["group"] == OURS or not row["answering"]:
            row["dns"] = dns_facts(host)
        rows.append(row)

    # The retired VPS, addressed rather than named.
    vps = probe_retired_vps(timeout)
    if vps is not None:
        rows.append({
            "name": "careagents VPS (address from deploy/careagents/deploy.sh)",
            "group": OURS,
            "host": VPS_PLACEHOLDER,
            "probe": f"https://{VPS_SNI_HOST}/healthz via --resolve "
                     f"{VPS_PLACEHOLDER}",
            "why": "the #624 box; deploy/careagents/deploy.sh still defaults to it",
            "status": vps["status"],
            "error": _scrub(vps["error"] or ""),
            "location": "",
            "answering": answering(vps),
            "watched": False,
            "verdict": classify(vps, False),
            "build": _scrub(build_evidence("", vps)),
            "references": ["deploy/careagents/deploy.sh"],
            "dns": None,
        })

    for verdict, colour in ((LIVE_UNWATCHED, R), (LIVE_WATCHED, G), (DEAD, Y)):
        group = [r for r in rows if r["verdict"] == verdict]
        out(f"{colour}{verdict.upper()} — {len(group)}{X}")
        for row in group:
            code = row["status"] if row["status"] is not None else row["error"]
            out(f"  [{row['group']}] {row['name']}")
            out(f"      {row['probe']} -> {code}")
            if row["build"]:
                out(f"      {D}{row['build']}{X}")
            if row.get("dns"):
                dns = row["dns"]
                out(f"      {D}dns: resolves={dns['resolves']} "
                    f"resolvers_agree={dns['resolvers_agree']} "
                    f"retired_vps={dns['points_at_retired_vps']} "
                    f"ns={','.join(dns['nameservers']) or '—'}{X}")
            if row["references"]:
                out(f"      {D}referenced: "
                    f"{', '.join(row['references'])}{X}")
        out()

    counts = {v: len([r for r in rows if r["verdict"] == v])
              for v in (LIVE_WATCHED, LIVE_UNWATCHED, DEAD)}
    out(f"{B}{len(rows)} surface(s) probed{X}: "
        + ", ".join(f"{n} {v}" for v, n in counts.items()))
    return {"watched_urls": watched_list,
            "watched_hosts": sorted(watched_hosts),
            "surfaces": rows,
            "counts": counts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--json-out", metavar="PATH",
                    help="write the same findings as JSON")
    args = ap.parse_args()
    payload = run(args.timeout)
    if args.json_out:
        Path(args.json_out).write_text(
            _scrub(json.dumps(payload, indent=2)) + "\n", encoding="utf-8")
    # Always 0: this reports, it does not gate. An inventory that fails a build
    # because a third party's demo server is down would be turned off in a week.
    return 0


if __name__ == "__main__":
    sys.exit(main())
