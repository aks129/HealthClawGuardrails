#!/Users/coopeydoop/.healthclaw/venv/bin/python3
"""
scripts/bot_commands.py

Shared command helper invoked by each OpenClaw agent (Sally, Mary, Dom,
Shervin, Ronny, Joe, Kristy) when the user sends a slash command in
Telegram. Each agent's AGENTS.md tells the LLM which commands to
handle and to exec this script for the mechanics.

Deployed to the Mac mini at ~/.healthclaw/commands.py by
scripts/bot_commands_install.sh. Reads secrets from ~/.healthclaw/env
(preferred) or ~/.kristy/env (fallback for co-install).

Design goals:
  - Zero side effects on bad input (prints an error line, exits non-zero).
  - Structured stdout (one fact per line) so the LLM can paraphrase it.
  - No external Python deps beyond `requests` (already installed for kristy).
  - Same STEP_UP_SECRET as Railway → tokens are accepted by the Flask API.

Usage:
  ~/.healthclaw/commands.py <command> [--agent <id>] [--tenant <id>]

Infra commands (every agent):
  dashboard       — mint a 24h signed dashboard URL
  health          — probe Railway Flask + OpenClaw Gateway + Redis
  tasks           — list pending AgentTasks for the tenant
  token           — emit a step-up token (5-min TTL)  [dev/debug]
  help            — print command list

FHIR read commands (every agent, answers depend on their specialty):
  conditions      — active Conditions with codes + onset dates
  labs            — recent lab Observations (category=laboratory)
  vitals          — recent vital-signs Observations (BP, HR, weight, etc.)
  meds            — active MedicationRequests
  allergies       — AllergyIntolerance list
  immunizations   — Immunization history
  summary         — one-page clinical summary (counts by resource type)
  fhir <type>     — generic FHIR search for any resource type

Data pipeline:
  import <path>   — POST a FHIR bundle (.json) to /Bundle/$ingest-context
  import-help     — show step-by-step instructions for getting data in

Kristy-only:
  week            — run the Kristy watcher (pulls iCals + emits conflicts)
  conflicts       — list family-conflict:* AgentTasks
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import warnings
from pathlib import Path

# Silence the urllib3 "LibreSSL" warning — adds 2 lines of noise to every
# agent-invoked run which the LLM then tries to paraphrase.
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import requests


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

_ENV_CANDIDATES = (
    Path.home() / ".healthclaw" / "env",
    Path.home() / ".kristy" / "env",  # co-install fallback
)


def _load_env() -> None:
    for path in _ENV_CANDIDATES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def _api_base() -> str:
    return os.environ.get(
        "COMMAND_CENTER_API",
        "https://app.healthclaw.io/command-center/api",
    ).rstrip("/")


def _dashboard_base() -> str:
    return os.environ.get(
        "DASHBOARD_BASE_URL",
        "https://app.healthclaw.io",
    ).rstrip("/")


def _tenant_default() -> str:
    return os.environ.get("DEFAULT_TENANT", "ev-personal")


# ---------------------------------------------------------------------------
# Secrets + tokens
# ---------------------------------------------------------------------------

def _stepup_secret() -> str:
    s = os.environ.get("STEP_UP_SECRET", "").strip()
    if not s:
        print("error: STEP_UP_SECRET not set (~/.healthclaw/env)", file=sys.stderr)
        sys.exit(2)
    return s


def mint_step_up_token(tenant: str, agent: str = "bot") -> str:
    """Same format as r6.stepup.generate_step_up_token: base64url(json).hmac_hex"""
    secret = _stepup_secret()
    payload = {
        "exp": int(time.time()) + 300,
        "tid": tenant,
        "sub": agent,
        "nonce": secrets.token_hex(16),
    }
    p = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
    return f"{p}.{sig}"


def mint_dashboard_token(tenant: str, agent: str = "bot") -> str:
    """
    24-hour signed URL access token (itsdangerous format) — accepted by
    Flask's access.verify_access_token. Replicates the server-side
    signing scheme so we can generate links offline.
    """
    # itsdangerous URLSafeTimedSerializer with salt "command-center-access-v1"
    secret = _stepup_secret()  # server's SESSION_SECRET fallback is STEP_UP_SECRET
    import itsdangerous
    s = itsdangerous.URLSafeTimedSerializer(
        secret, salt="command-center-access-v1"
    )
    payload = {"tenant_id": tenant}
    if agent:
        payload["agent_id"] = agent
    return s.dumps(payload)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_dashboard(args) -> int:
    tenant = args.tenant or _tenant_default()
    token = mint_dashboard_token(tenant, args.agent or "bot")
    url = f"{_dashboard_base()}/command-center?tenant={tenant}&t={token}"
    print(url)
    print("valid: 24h")
    return 0


def cmd_health(args) -> int:
    base = _dashboard_base()
    rows = []
    # Flask
    try:
        r = requests.get(f"{base}/r6/fhir/health", timeout=8)
        rows.append(f"flask: HTTP {r.status_code} ({r.elapsed.total_seconds()*1000:.0f}ms)")
    except Exception as exc:
        rows.append(f"flask: unreachable — {exc}")
    # Command center API — need a session, use step-up
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/system",
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            gw = d.get("openclaw_gateway", {})
            rows.append(
                f"openclaw gateway: "
                f"{'reachable' if gw.get('reachable') else 'unreachable'}"
                f" ({gw.get('status_code','?')}, {gw.get('latency_ms','?')}ms)"
            )
            mcp = d.get("mcp_server", {})
            rows.append(f"mcp server: {'up' if mcp.get('up') else 'down'}")
            redis = d.get("redis", {})
            rows.append(
                "redis: "
                + ("up" if redis.get("up") else f"down (configured={redis.get('configured')})")
            )
        else:
            rows.append(f"system api: HTTP {r.status_code}")
    except Exception as exc:
        rows.append(f"system api: error — {exc}")
    for row in rows:
        print(row)
    return 0


def cmd_tasks(args) -> int:
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/tasks",
            params={"tenant": tenant, "limit": 20},
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"error fetching tasks: {exc}", file=sys.stderr)
        return 1
    tasks = r.json()
    if not tasks:
        print("No pending tasks.")
        return 0
    print(f"Pending tasks ({len(tasks)}):")
    for t in tasks:
        print(f"- [{t.get('priority','?'):8s}] {t.get('title','?')}")
        if t.get("agent_emoji") or t.get("agent_name"):
            print(f"    for: {t.get('agent_emoji','')} {t.get('agent_name','?')}")
        if t.get("source"):
            print(f"    source: {t['source']}")
    return 0


def cmd_conflicts(args) -> int:
    """List current family-conflict:* pending tasks (Kristy-specific filter)."""
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    try:
        r = requests.get(
            f"{_api_base()}/tasks",
            params={"tenant": tenant, "limit": 50},
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rows = [
        t for t in r.json()
        if (t.get("resource_ref") or "").startswith("family-conflict:")
    ]
    if not rows:
        print("No family schedule conflicts pending.")
        return 0
    print(f"Family conflicts pending ({len(rows)}):")
    for t in rows:
        print(f"- [{t.get('priority','?'):8s}] {t.get('title','?')}")
        desc = (t.get("description") or "").split("\n", 1)[0]
        if desc:
            print(f"    {desc}")
    return 0


def cmd_week(args) -> int:
    """Run the Kristy watcher (Kristy persona only)."""
    watcher = Path.home() / ".kristy" / "watcher.py"
    if not watcher.exists():
        print(f"error: watcher not installed at {watcher}", file=sys.stderr)
        return 1
    os.execv("/usr/bin/python3", ["python3", str(watcher)])
    return 0  # unreachable


def cmd_token(args) -> int:
    """Emit a fresh step-up token — useful for curl debugging."""
    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")
    print(token)
    print(f"tenant: {tenant}")
    print(f"ttl: 300s")
    return 0


def cmd_help(args) -> int:
    print(
        "HealthClaw bot commands:\n"
        "  Core:\n"
        "    /dashboard     fresh 24h signed command-center link\n"
        "    /health        stack health (Flask, MCP, gateway, Redis)\n"
        "    /tasks         pending AgentTasks for your tenant\n"
        "    /help          this list\n"
        "  FHIR reads:\n"
        "    /conditions    active Conditions with codes + onset\n"
        "    /labs          recent lab results\n"
        "    /vitals        recent vitals (BP, HR, weight, etc.)\n"
        "    /meds          active medications\n"
        "    /allergies     allergies + intolerances\n"
        "    /immunizations vaccine history\n"
        "    /summary       one-page count-by-type summary\n"
        "    /fhir <type>   generic FHIR search\n"
        "  Data pipeline:\n"
        "    /export        pull HealthEx → redact PHI → write bundle\n"
        "    /import <path> ingest a bundle JSON file\n"
        "    /import-help   end-to-end import instructions\n"
        "  Kristy:\n"
        "    /week          schedule scan\n"
        "    /conflicts     family schedule conflicts\n"
    )
    return 0


# ---------------------------------------------------------------------------
# FHIR reads — GET /r6/fhir/<ResourceType>?...
# ---------------------------------------------------------------------------

def _fhir_base() -> str:
    """Railway Flask's FHIR REST facade."""
    return os.environ.get(
        "FHIR_BASE_URL",
        _dashboard_base() + "/r6/fhir",
    ).rstrip("/")


def _fhir_get(resource_type: str, params: dict, tenant: str) -> dict:
    """Generic FHIR search, returns parsed Bundle or raises."""
    r = requests.get(
        f"{_fhir_base()}/{resource_type}",
        params=params,
        headers={"X-Tenant-Id": tenant, "Accept": "application/fhir+json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _code_display(coding_list: list, fallback: str = "—") -> str:
    """From a FHIR coding list, pick a readable label."""
    if not coding_list:
        return fallback
    for c in coding_list:
        if c.get("display"):
            return f"{c['display']} ({c.get('system','?').rsplit('/',1)[-1]}:{c.get('code','?')})"
    first = coding_list[0]
    return f"{first.get('system','?').rsplit('/',1)[-1]}:{first.get('code','?')}"


def cmd_conditions(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get("Condition", {"_count": 50, "_sort": "-_lastUpdated"}, tenant)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No Conditions on file for tenant '{tenant}'.")
        return 0
    # Group by clinical status
    active, resolved = [], []
    for c in entries:
        status_code = (c.get("clinicalStatus", {}).get("coding") or [{}])[0].get("code", "")
        label = _code_display(c.get("code", {}).get("coding"),
                              c.get("code", {}).get("text", "unnamed"))
        onset = c.get("onsetDateTime") or c.get("recordedDate") or "?"
        row = f"- {label} (onset {onset})"
        (active if status_code in ("active", "recurrence", "relapse") else resolved).append(row)
    if active:
        print(f"Active conditions ({len(active)}):")
        for r in active: print(r)
    if resolved:
        print(f"\nResolved/inactive conditions ({len(resolved)}):")
        for r in resolved: print(r)
    return 0


def cmd_labs(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get(
            "Observation",
            {"category": "laboratory", "_count": 20, "_sort": "-_lastUpdated"},
            tenant,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No lab Observations on file for tenant '{tenant}'.")
        return 0
    print(f"Recent labs ({len(entries)}):")
    for obs in entries:
        code = _code_display(obs.get("code", {}).get("coding"),
                             obs.get("code", {}).get("text", "lab"))
        when = obs.get("effectiveDateTime") or obs.get("issued") or "?"
        value = _format_obs_value(obs)
        interp = (obs.get("interpretation", [{}])[0].get("coding") or [{}])[0].get("code", "")
        flag = f" [{interp}]" if interp and interp not in ("N", "NR") else ""
        print(f"- {when} · {code}: {value}{flag}")
    return 0


def cmd_vitals(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get(
            "Observation",
            {"category": "vital-signs", "_count": 30, "_sort": "-_lastUpdated"},
            tenant,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No vital-sign Observations on file for tenant '{tenant}'.")
        return 0
    print(f"Recent vitals ({len(entries)}):")
    for obs in entries:
        code = _code_display(obs.get("code", {}).get("coding"),
                             obs.get("code", {}).get("text", "vital"))
        when = (obs.get("effectiveDateTime") or obs.get("issued") or "?")[:10]
        value = _format_obs_value(obs)
        print(f"- {when} · {code}: {value}")
    return 0


def _format_obs_value(obs: dict) -> str:
    """Render Observation.value[x] as a human-readable string."""
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        return f"{q.get('value','?')} {q.get('unit','')}".strip()
    if "valueCodeableConcept" in obs:
        return _code_display(obs["valueCodeableConcept"].get("coding"),
                             obs["valueCodeableConcept"].get("text", "?"))
    if "valueString" in obs:
        return str(obs["valueString"])
    if "component" in obs:
        # Multi-component (e.g., BP = systolic + diastolic)
        parts = []
        for c in obs["component"]:
            code = (c.get("code", {}).get("coding") or [{}])[0].get("display", "")
            q = c.get("valueQuantity") or {}
            parts.append(f"{code} {q.get('value','?')}{q.get('unit','')}".strip())
        return ", ".join(parts)
    return "(no value)"


def cmd_meds(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get("MedicationRequest", {"_count": 50, "_sort": "-_lastUpdated"}, tenant)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No MedicationRequests on file for tenant '{tenant}'.")
        return 0
    active = [m for m in entries if m.get("status") == "active"]
    inactive = [m for m in entries if m.get("status") != "active"]
    if active:
        print(f"Active medications ({len(active)}):")
        for m in active:
            med = (
                _code_display(m.get("medicationCodeableConcept", {}).get("coding"),
                              m.get("medicationCodeableConcept", {}).get("text", "medication"))
                if m.get("medicationCodeableConcept") else m.get("medicationReference", {}).get("display", "?")
            )
            print(f"- {med}  · intent: {m.get('intent','?')}")
    if inactive:
        print(f"\nInactive medications ({len(inactive)}):")
        for m in inactive[:10]:
            med = (_code_display(m.get("medicationCodeableConcept", {}).get("coding"),
                                 m.get("medicationCodeableConcept", {}).get("text", "medication"))
                   if m.get("medicationCodeableConcept") else "?")
            print(f"- {med}  · status: {m.get('status','?')}")
    return 0


def cmd_allergies(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get("AllergyIntolerance", {"_count": 50, "_sort": "-_lastUpdated"}, tenant)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No AllergyIntolerance records on file for tenant '{tenant}'.")
        return 0
    print(f"Allergies & intolerances ({len(entries)}):")
    for a in entries:
        code = _code_display(a.get("code", {}).get("coding"),
                             a.get("code", {}).get("text", "allergen"))
        status = (a.get("clinicalStatus", {}).get("coding") or [{}])[0].get("code", "?")
        crit = a.get("criticality") or "?"
        print(f"- {code}  · clinical:{status}, criticality:{crit}")
    return 0


def cmd_immunizations(args) -> int:
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get("Immunization", {"_count": 100, "_sort": "-_lastUpdated"}, tenant)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    if not entries:
        print(f"No Immunizations on file for tenant '{tenant}'.")
        return 0
    print(f"Immunizations ({len(entries)}):")
    for imm in entries[:30]:
        vaccine = _code_display(imm.get("vaccineCode", {}).get("coding"),
                                imm.get("vaccineCode", {}).get("text", "vaccine"))
        when = (imm.get("occurrenceDateTime") or "?")[:10]
        print(f"- {when} · {vaccine}  · status: {imm.get('status','?')}")
    return 0


def cmd_summary(args) -> int:
    """Print record counts per resource type — handy 'what do I have on file' overview."""
    tenant = args.tenant or _tenant_default()
    kinds = [
        ("Patient", "patients"),
        ("Condition", "conditions"),
        ("Observation", "observations"),
        ("MedicationRequest", "medications"),
        ("AllergyIntolerance", "allergies"),
        ("Immunization", "immunizations"),
        ("Procedure", "procedures"),
        ("DiagnosticReport", "diagnostic reports"),
        ("CarePlan", "care plans"),
    ]
    print(f"Clinical summary for tenant '{tenant}':")
    any_data = False
    for rtype, label in kinds:
        try:
            b = _fhir_get(rtype, {"_summary": "count"}, tenant)
            n = b.get("total", 0)
        except Exception:
            n = "?"
        if isinstance(n, int) and n > 0:
            any_data = True
        print(f"  {label:24s} {n}")
    if not any_data:
        print("\nNo clinical data yet. Run /import-help to see how to bring your records in.")
    return 0


def cmd_fhir(args) -> int:
    """Generic FHIR search: /fhir <type> [--code X] — flexible escape hatch."""
    if not args.resource_type:
        print("usage: fhir <ResourceType> [--code CODE] [--patient ID] [--count N]",
              file=sys.stderr)
        return 1
    params = {"_count": args.count or 20, "_sort": "-_lastUpdated"}
    if args.code:    params["code"] = args.code
    if args.patient: params["patient"] = args.patient
    tenant = args.tenant or _tenant_default()
    try:
        bundle = _fhir_get(args.resource_type, params, tenant)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    print(f"{len(entries)} × {args.resource_type} (of {bundle.get('total','?')} total):")
    for r in entries[:10]:
        summary = r.get("id", "?")
        if r.get("code", {}).get("text"):
            summary += f" · {r['code']['text']}"
        elif r.get("meta", {}).get("tag"):
            summary += f" · tag={r['meta']['tag'][0].get('code','?')}"
        print(f"- {summary}")
    return 0


# ---------------------------------------------------------------------------
# Data import — POST a bundle to /Bundle/$ingest-context
# ---------------------------------------------------------------------------

def cmd_import(args) -> int:
    if not args.path:
        print("usage: import <bundle.json>", file=sys.stderr)
        return 1
    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    try:
        bundle = json.loads(path.read_text())
    except Exception as exc:
        print(f"bundle parse error: {exc}", file=sys.stderr)
        return 1

    tenant = args.tenant or _tenant_default()
    token = mint_step_up_token(tenant, args.agent or "bot")

    try:
        r = requests.post(
            f"{_fhir_base()}/Bundle/$ingest-context",
            json=bundle,
            headers={
                "X-Tenant-Id": tenant,
                "X-Step-Up-Token": token,
                "X-Human-Confirmed": "true",
                "Content-Type": "application/fhir+json",
            },
            timeout=60,
        )
    except Exception as exc:
        print(f"POST failed: {exc}", file=sys.stderr)
        return 1

    if r.status_code >= 400:
        print(f"HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 1

    out = r.json() if r.headers.get("content-type", "").startswith("application/") else {}
    entries_count = len(bundle.get("entry", []))
    print(f"Imported {entries_count} entries into tenant '{tenant}'.")
    if isinstance(out, dict):
        if out.get("context_id"):
            print(f"context_id: {out['context_id']}")
        if out.get("items_ingested") is not None:
            print(f"items_ingested: {out['items_ingested']}")
    return 0


def cmd_import_help(args) -> int:
    print(
        "Getting your health data into HealthClaw — two paths:\n"
        "\n"
        "A. /export end-to-end (recommended — runs on the Mac mini):\n"
        "   1. Store your HealthEx OAuth token in the Mac mini Keychain:\n"
        "      security add-generic-password -s healthex -a me -w '<token>'\n"
        "   2. DM any bot: /export\n"
        "      → pulls from HealthEx MCP, redacts PHI, writes a bundle\n"
        "        to ~/.healthclaw/exports/healthex-<date>.json\n"
        "   3. DM: /import <path printed by /export>\n"
        "\n"
        "B. Manual bundle (any FHIR R4 transaction bundle):\n"
        "   1. Put the JSON file on the Mac mini at ~/bundle.json\n"
        "   2. Tell any bot: /import ~/bundle.json\n"
        "\n"
        "After import: /summary confirms counts; /conditions /labs etc. work."
    )
    return 0


# ---------------------------------------------------------------------------
# /export — wrap export_healthex_mcp.py so bots can run the pipeline
# ---------------------------------------------------------------------------

def cmd_export(args) -> int:
    """Run scripts/export_healthex_mcp.py against HealthEx + redact in-process.

    Token resolution order:
      1. $HEALTHEX_AUTH_TOKEN (already exported)
      2. macOS Keychain item with service 'healthex'
    """
    import subprocess
    tenant = args.tenant or _tenant_default()

    # Resolve token
    token = os.environ.get("HEALTHEX_AUTH_TOKEN", "").strip()
    if not token:
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "healthex", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                token = r.stdout.strip()
        except Exception:
            pass
    if not token:
        print(
            "error: HEALTHEX_AUTH_TOKEN not set and not in Keychain.\n"
            "fix: security add-generic-password -s healthex -a me -w '<token>'",
            file=sys.stderr,
        )
        return 1

    # Script path (on the Mac mini this is ~/.healthclaw/export_healthex_mcp.py)
    exports_dir = Path.home() / ".healthclaw" / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    from datetime import date
    out = exports_dir / f"healthex-{date.today().isoformat()}.json"

    script = Path.home() / ".healthclaw" / "export_healthex_mcp.py"
    if not script.exists():
        # Fall back to the repo path (for laptop dev)
        script = Path(__file__).parent / "export_healthex_mcp.py"
    if not script.exists():
        print(f"error: export script not found at {script}", file=sys.stderr)
        return 1

    # Use the same venv python that runs commands.py (has mcp + httpx + icalendar)
    venv_python = Path.home() / ".healthclaw" / "venv" / "bin" / "python3"
    python = str(venv_python) if venv_python.exists() else sys.executable

    env = {**os.environ, "HEALTHEX_AUTH_TOKEN": token}
    cmd = [python, str(script), "--tenant-id", tenant, "--output", str(out)]

    print(f"Running HealthEx export → {out}")
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("error: export timed out after 5 minutes", file=sys.stderr)
        return 1

    # Last line of stderr has the summary; stream it
    for line in (r.stderr or "").splitlines()[-10:]:
        print(line)
    if r.returncode != 0:
        print(f"error: export exited {r.returncode}", file=sys.stderr)
        return r.returncode

    # Summarize the output
    try:
        data = json.loads(out.read_text())
        rs = data.get("_meta", {}).get("redaction_stats", {})
        redacted_total = sum(rs.values())
        print(f"\nOutput: {out}")
        print(f"  size: {out.stat().st_size:,} bytes")
        print(f"  tools ok: {len(data.get('records', {}))}")
        print(f"  tools failed: {len(data.get('errors', {}))}")
        print(f"  fields redacted: {redacted_total}")
        if redacted_total == 0:
            print("  WARNING: zero fields redacted — confirm redaction hook is wired")
        print(f"\nNext: tell any bot `/import {out}` to ingest this bundle.")
    except Exception as exc:
        print(f"  (couldn't summarize: {exc})")
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_COMMANDS = {
    "dashboard":     cmd_dashboard,
    "health":        cmd_health,
    "tasks":         cmd_tasks,
    "conflicts":     cmd_conflicts,
    "week":          cmd_week,
    "token":         cmd_token,
    "help":          cmd_help,
    # FHIR reads
    "conditions":    cmd_conditions,
    "labs":          cmd_labs,
    "vitals":        cmd_vitals,
    "meds":          cmd_meds,
    "allergies":     cmd_allergies,
    "immunizations": cmd_immunizations,
    "summary":       cmd_summary,
    "fhir":          cmd_fhir,
    # Data pipeline
    "export":        cmd_export,
    "import":        cmd_import,
    "import-help":   cmd_import_help,
}


def main() -> int:
    _load_env()

    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("command", choices=list(_COMMANDS.keys()))
    p.add_argument("arg", nargs="?", default=None,
                   help="positional arg — resource type for 'fhir', file path for 'import'")
    p.add_argument("--agent", default=None, help="agent id (sally, mary, dom, ...)")
    p.add_argument("--tenant", default=None, help="tenant id (default: $DEFAULT_TENANT)")
    p.add_argument("--code", default=None, help="FHIR search param (fhir command)")
    p.add_argument("--patient", default=None, help="FHIR search param (fhir command)")
    p.add_argument("--count", type=int, default=None, help="FHIR _count (fhir command)")
    args = p.parse_args()
    # Alias for commands that expect a specifically named positional
    args.resource_type = args.arg
    args.path = args.arg
    return _COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
