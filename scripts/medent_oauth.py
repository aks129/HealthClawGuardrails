#!/usr/bin/env python3
"""SMART on FHIR Patient Standalone Launch helper for MEDENT.

MEDENT is the most common small-practice EHR on the US East Coast. It exposes
a standard FHIR R4 API protected by OAuth 2.0 + SMART App Launch. This script
drives the complete authorization dance and caches tokens for
export_medent_fhir.py to reuse.

MEDENT endpoints (v23.5+):
  Dynamic Registration: https://www.medentfhir.com/fhir/R4/dynamicregistration/
  Practice list:        https://www.medentfhir.com/fhir/resources/practices.php
  Authorization:        https://www.medentfhir.com/fhir/R4/{PRACTICE_ID}/authorize
  Token (initial):      https://www.medentfhir.com/fhir/R4/token/index.php?medent_practice_id={PRACTICE_ID}
  Token (refresh):      https://www.medentfhir.com/fhir/R4/{PRACTICE_ID}/token
  FHIR base:            https://www.medentfhir.com/fhir/R4/{PRACTICE_ID}/

Auth flow:
  1. register  — DCR once to get client_id; saves to ~/.healthclaw/medent_client.json
  2. practices — list practices available for your client_id
  3. authorize — Patient Standalone Launch; opens browser → patient portal login
                 → consent → code exchanged → tokens cached

Token lifetime: access_token = 15 min; refresh_token = 24 h.
After auth, export_medent_fhir.py auto-refreshes as needed.

Subcommands:
  register   -- Dynamic Client Registration (one-time)
  practices  -- list FHIR-enabled MEDENT practices
  authorize  -- complete the SMART OAuth dance; cache tokens
  status     -- show cached token state + expiry
  refresh    -- force a token refresh using cached refresh_token

Environment variables:
  MEDENT_CLIENT_ID       From registration (auto-loaded from medent_client.json)
  MEDENT_CLIENT_SECRET   From registration if not public client
  MEDENT_PRACTICE_ID     Your practice's MEDENT ID (find via 'practices')
  MEDENT_REDIRECT_URI    Default: http://localhost:8743/medent/callback
  MEDENT_SCOPES          Default: patient/*.read openid profile offline_access
  MEDENT_TOKEN_CACHE     Default: ~/.healthclaw/medent_tokens.json
  MEDENT_CLIENT_CACHE    Default: ~/.healthclaw/medent_client.json

Usage:
  python scripts/medent_oauth.py register --contacts eugene.vestel@gmail.com
  python scripts/medent_oauth.py practices
  python scripts/medent_oauth.py authorize --practice-id 12345 --tenant-id ev-personal
  python scripts/medent_oauth.py status
  python scripts/medent_oauth.py refresh
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

_MEDENT_BASE = "https://www.medentfhir.com/fhir/R4"
_DCR_URL = f"{_MEDENT_BASE}/dynamicregistration/"
_PRACTICES_URL = "https://www.medentfhir.com/fhir/resources/practices.php"
_DEFAULT_SCOPES = "patient/*.read openid profile offline_access"
_DEFAULT_REDIRECT_URI = "http://localhost:8743/medent/callback"
_CALLBACK_TIMEOUT = 180  # seconds — patient portal login can be slow

TOKEN_CACHE = Path(os.environ.get(
    "MEDENT_TOKEN_CACHE",
    str(Path.home() / ".healthclaw" / "medent_tokens.json")))
CLIENT_CACHE = Path(os.environ.get(
    "MEDENT_CLIENT_CACHE",
    str(Path.home() / ".healthclaw" / "medent_client.json")))


# ── PKCE ──────────────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier_bytes = secrets.token_bytes(48)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def _load_client() -> dict:
    """Load DCR registration from cache or env."""
    client_id = os.environ.get("MEDENT_CLIENT_ID", "").strip()
    if client_id:
        return {"client_id": client_id,
                "client_secret": os.environ.get("MEDENT_CLIENT_SECRET", "").strip()}
    return _load(CLIENT_CACHE)


# ── Local callback server ─────────────────────────────────────────────────────

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict | None = None
    _done = threading.Event()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        error = (params.get("error") or [None])[0]

        if error:
            body = f"<h2>Authorization error: {error}</h2><p>You can close this tab.</p>"
            _CallbackHandler.result = {
                "error": error,
                "error_description": (params.get("error_description") or [""])[0],
            }
        elif code:
            body = "<h2>Authorization successful.</h2><p>You can close this tab and return to the terminal.</p>"
            _CallbackHandler.result = {"code": code, "state": state}
        else:
            body = "<h2>Unexpected callback — no code received.</h2>"
            _CallbackHandler.result = {"error": "no_code"}

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        _CallbackHandler._done.set()

    def log_message(self, fmt, *args):
        pass


def _wait_for_callback(port: int, timeout: int) -> dict:
    _CallbackHandler.result = None
    _CallbackHandler._done.clear()
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)

    def _serve():
        while not _CallbackHandler._done.is_set():
            server.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    if not _CallbackHandler._done.wait(timeout=timeout):
        server.server_close()
        raise TimeoutError(f"no callback received within {timeout}s")
    server.server_close()
    return _CallbackHandler.result or {}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url: str, **kwargs) -> dict:
    import httpx
    resp = httpx.post(url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_register(args: argparse.Namespace) -> int:
    """Dynamic Client Registration — run once to get a client_id."""
    import httpx

    redirect_uri = os.environ.get("MEDENT_REDIRECT_URI", _DEFAULT_REDIRECT_URI).strip()

    payload = {
        "client_name": args.client_name,
        "client_uri": "https://healthclaw.io",
        "logo_uri": "https://healthclaw.io/static/img/healthclaw-logo.png",
        "tos_uri": "https://healthclaw.io/terms",
        "policy_uri": "https://healthclaw.io/privacy",
        "contacts": args.contacts,
        "redirect_uris": [redirect_uri],
        # Required for Patient Launch — must be a reachable URL (not 400)
        "initiate_login_uri": "https://healthclaw.io",
        "scope": _DEFAULT_SCOPES,
        "response_types": ["code"],
        "grant_types": ["authorization_code"],
        "token_endpoint_auth_method": "none",
    }

    print(f"Registering '{args.client_name}' at {_DCR_URL} ...")
    print(f"  redirect_uri: {redirect_uri}")
    print(f"  contacts: {args.contacts}")
    print()

    try:
        resp = httpx.post(_DCR_URL, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"error: registration failed — {exc}", file=sys.stderr)
        return 1

    client_id = body.get("client_id")
    if not client_id:
        print(f"error: no client_id in response:\n{json.dumps(body, indent=2)}", file=sys.stderr)
        return 1

    _save(CLIENT_CACHE, body)
    print(f"client_id: {client_id}")
    print(f"registration saved to {CLIENT_CACHE}")
    print()
    print("Next steps:")
    print("  python scripts/medent_oauth.py practices")
    print("  python scripts/medent_oauth.py authorize --practice-id <id> --tenant-id ev-personal")
    return 0


def cmd_practices(args: argparse.Namespace) -> int:
    """List FHIR-enabled MEDENT practices available for this client_id."""
    import httpx

    client = _load_client()
    client_id = client.get("client_id", "").strip()
    if not client_id:
        print(
            "error: no client_id found.\n"
            "  Run: python scripts/medent_oauth.py register --contacts you@email.com",
            file=sys.stderr,
        )
        return 1

    print(f"Fetching practice list for client_id={client_id} ...")
    try:
        resp = httpx.post(_PRACTICES_URL, data={"clientid": client_id}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    practices = body if isinstance(body, list) else body.get("practices", [body])
    if not practices:
        print("no practices returned — your registration may still be under review")
        return 1

    print(f"\nFHIR-enabled practices ({len(practices)}):")
    for p in practices:
        name = p.get("name", "?")
        base = p.get("baseUrl", p.get("base_url", "?"))
        pid = p.get("practice_id") or p.get("id") or base.rstrip("/").rsplit("/", 1)[-1]
        print(f"  {pid:10s}  {name}")
        print(f"             {base}")

    print()
    print("Use the practice_id / last segment of baseUrl with --practice-id.")
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    """Patient Standalone Launch — opens browser, waits for callback, caches tokens."""
    client = _load_client()
    client_id = client.get("client_id", "").strip()
    if not client_id:
        print(
            "error: no client_id — run 'register' first.",
            file=sys.stderr,
        )
        return 1

    practice_id = (args.practice_id or os.environ.get("MEDENT_PRACTICE_ID", "")).strip()
    if not practice_id:
        print(
            "error: --practice-id required (find yours with 'practices').",
            file=sys.stderr,
        )
        return 1

    redirect_uri = os.environ.get("MEDENT_REDIRECT_URI", _DEFAULT_REDIRECT_URI).strip()
    scopes = os.environ.get("MEDENT_SCOPES", _DEFAULT_SCOPES).strip()
    if args.scopes:
        scopes = args.scopes

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    port = parsed_redirect.port or 80

    code_verifier, code_challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_url = (
        f"{_MEDENT_BASE}/{practice_id}/authorize?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "aud": f"{_MEDENT_BASE}/{practice_id}",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
    )

    print("Opening MEDENT patient portal authorization in browser:")
    print(f"  {auth_url}")
    print()
    print("Log in with your MEDENT patient portal credentials when the page opens.")
    print(f"Waiting for callback on port {port} (timeout: {_CALLBACK_TIMEOUT}s)...")
    print()

    if not args.no_browser:
        webbrowser.open(auth_url)

    try:
        callback = _wait_for_callback(port, _CALLBACK_TIMEOUT)
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("  Open the URL above manually if your browser didn't launch.", file=sys.stderr)
        return 1

    if "error" in callback:
        print(f"error: {callback['error']}: {callback.get('error_description', '')}", file=sys.stderr)
        return 1

    if callback.get("state") != state:
        print("error: state mismatch — possible CSRF attempt", file=sys.stderr)
        return 1

    code = callback["code"]
    print("authorization code received, exchanging for tokens...")

    token_url = f"{_MEDENT_BASE}/token/index.php?medent_practice_id={practice_id}"
    try:
        body = _post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
    except Exception as exc:
        print(f"error: token exchange failed — {exc}", file=sys.stderr)
        return 1

    expires_in = int(body.get("expires_in", 900))
    patient_mrn = body.get("patient", "")
    cached = {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": datetime.now(timezone.utc).timestamp() + expires_in - 60,
        "scope": body.get("scope", scopes),
        "token_type": body.get("token_type", "Bearer"),
        "patient_mrn": patient_mrn,
        "practice_id": practice_id,
        "tenant_id": args.tenant_id,
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(TOKEN_CACHE, cached)

    expiry = datetime.fromtimestamp(cached["expires_at"], tz=timezone.utc)
    print(f"tokens cached at {TOKEN_CACHE}")
    print(f"  access_token: ...{cached['access_token'][-8:]}")
    print(f"  expires:      {expiry.isoformat(timespec='seconds')}")
    print(f"  refresh_token: {'yes' if cached.get('refresh_token') else 'no'}")
    print(f"  patient_mrn:  {patient_mrn or '(not returned)'}")
    print(f"  practice_id:  {practice_id}")
    print()
    print("next step:")
    print(f"  python scripts/export_medent_fhir.py --tenant-id {args.tenant_id}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cached = _load(TOKEN_CACHE)
    if not cached:
        print(f"no cached tokens at {TOKEN_CACHE}")
        client = _load_client()
        if client.get("client_id"):
            print(f"  (client_id registered: {client['client_id']})")
            print("  run 'authorize --practice-id <id>' to connect")
        else:
            print("  run 'register' first, then 'authorize'")
        return 1

    now = datetime.now(timezone.utc).timestamp()
    expires_at = cached.get("expires_at", 0)
    expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    remaining = max(0, expires_at - now)
    status = "VALID" if remaining > 0 else "EXPIRED"
    print(f"status: {status}")
    print(f"  expires:     {expiry.isoformat(timespec='seconds')} ({remaining:.0f}s remaining)")
    print(f"  practice_id: {cached.get('practice_id', '(not set)')}")
    print(f"  patient_mrn: {cached.get('patient_mrn', '(not set)')}")
    print(f"  tenant_id:   {cached.get('tenant_id', '(not set)')}")
    print(f"  refresh:     {'yes' if cached.get('refresh_token') else 'no'}")
    print(f"  obtained_at: {cached.get('obtained_at', '(unknown)')}")
    print(f"  cache:       {TOKEN_CACHE}")
    client = _load_client()
    if client.get("client_id"):
        print(f"  client_id:   {client['client_id']}")
    return 0


def cmd_refresh(_args: argparse.Namespace) -> int:
    cached = _load(TOKEN_CACHE)
    if not cached:
        print(f"no cached tokens at {TOKEN_CACHE}", file=sys.stderr)
        return 1
    refresh_token = cached.get("refresh_token")
    if not refresh_token:
        print("error: no refresh_token — run 'authorize' again", file=sys.stderr)
        return 1

    practice_id = cached.get("practice_id", "").strip()
    if not practice_id:
        print("error: practice_id missing from cache — run 'authorize' again", file=sys.stderr)
        return 1

    client = _load_client()
    client_id = client.get("client_id", os.environ.get("MEDENT_CLIENT_ID", "")).strip()

    refresh_url = f"{_MEDENT_BASE}/{practice_id}/token"
    print(f"refreshing tokens at {refresh_url} ...")
    try:
        body = _post(
            refresh_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )
    except Exception as exc:
        print(f"error: refresh failed — {exc}", file=sys.stderr)
        return 1

    expires_in = int(body.get("expires_in", 900))
    cached.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "expires_at": datetime.now(timezone.utc).timestamp() + expires_in - 60,
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save(TOKEN_CACHE, cached)

    expiry = datetime.fromtimestamp(cached["expires_at"], tz=timezone.utc)
    print(f"refreshed — new expiry: {expiry.isoformat(timespec='seconds')}")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_reg = sub.add_parser("register", help="One-time Dynamic Client Registration")
    p_reg.add_argument("--client-name", default="HealthClaw",
                       help="App name — must be unique on MEDENT (default: HealthClaw)")
    p_reg.add_argument("--contacts", default="eugene.vestel@gmail.com",
                       help="Contact email for registration")

    sub.add_parser("practices", help="List FHIR-enabled MEDENT practices")

    p_auth = sub.add_parser("authorize", help="Patient Standalone Launch OAuth flow")
    p_auth.add_argument("--practice-id", default=None,
                        help="MEDENT practice ID (find via 'practices')")
    p_auth.add_argument("--tenant-id", default="ev-personal",
                        help="HealthClaw tenant to associate tokens with")
    p_auth.add_argument("--scopes", default=None,
                        help="Override MEDENT_SCOPES")
    p_auth.add_argument("--no-browser", action="store_true",
                        help="Print URL without opening a browser")

    sub.add_parser("status", help="Show cached token state")
    sub.add_parser("refresh", help="Force a token refresh")

    args = parser.parse_args()
    if args.command == "register":   return cmd_register(args)
    if args.command == "practices":  return cmd_practices(args)
    if args.command == "authorize":  return cmd_authorize(args)
    if args.command == "status":     return cmd_status(args)
    if args.command == "refresh":    return cmd_refresh(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
