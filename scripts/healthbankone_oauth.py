#!/usr/bin/env python3
"""OAuth 2.x authorization-code + PKCE helper for Health Bank One.

Handles the consumer-consent dance, exchanges the authorization code for
tokens, and caches them securely for export_healthbankone_mcp.py to reuse.

Subcommands:

  authorize   -- open browser → wait for callback → exchange code → cache tokens
  status      -- show cached token state and expiry
  revoke      -- call HBO revoke endpoint (if set) and delete cached tokens
  refresh     -- force a token refresh using the cached refresh_token

Environment variables (all configurable, none required until HBO gives us URLs):

  HBO_CLIENT_ID               Required for authorize
  HBO_CLIENT_SECRET           Required for authorize (omit if public client)
  HBO_AUTHORIZATION_ENDPOINT  Required for authorize
  HBO_TOKEN_ENDPOINT          Required for authorize + refresh
  HBO_REVOCATION_ENDPOINT     Optional, used by revoke
  HBO_REDIRECT_URI            Default: http://localhost:8742/hbo/callback
  HBO_SCOPES                  Space-separated; default: openid offline_access
  HBO_TOKEN_CACHE             Path to token JSON; default: ~/.healthclaw/hbo_tokens.json

Usage:

  python scripts/healthbankone_oauth.py authorize --tenant-id ev-personal-hbo
  python scripts/healthbankone_oauth.py status
  python scripts/healthbankone_oauth.py revoke
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

TOKEN_CACHE = Path(os.environ.get(
    "HBO_TOKEN_CACHE", str(Path.home() / ".healthclaw" / "hbo_tokens.json")))

DEFAULT_REDIRECT_URI = "http://localhost:8742/hbo/callback"
DEFAULT_SCOPES = "openid offline_access"
CALLBACK_TIMEOUT = 120  # seconds to wait for browser callback


# ── PKCE helpers ───────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge_S256)."""
    verifier_bytes = secrets.token_bytes(48)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── Token cache ────────────────────────────────────────────────────────────────

def _load_cached() -> dict:
    if not TOKEN_CACHE.exists():
        return {}
    try:
        return json.loads(TOKEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cached(data: dict) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(data, indent=2))
    TOKEN_CACHE.chmod(0o600)


def _delete_cached() -> None:
    if TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()
        print(f"deleted {TOKEN_CACHE}")


# ── Local callback server ──────────────────────────────────────────────────────

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures one OAuth callback then shuts down."""

    result: dict | None = None  # code + state written here
    _done = threading.Event()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        error = (params.get("error") or [None])[0]

        if error:
            body = f"<h2>Authorization error: {error}</h2><p>You can close this tab.</p>"
            _CallbackHandler.result = {"error": error,
                                       "error_description": (params.get("error_description") or [""])[0]}
        elif code:
            body = "<h2>Authorization successful.</h2><p>You can close this tab.</p>"
            _CallbackHandler.result = {"code": code, "state": state}
        else:
            body = "<h2>Unexpected callback.</h2>"
            _CallbackHandler.result = {"error": "no_code"}

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        _CallbackHandler._done.set()

    def log_message(self, fmt, *args):  # suppress default access log
        pass


def _wait_for_callback(port: int, timeout: int) -> dict:
    """Start a one-shot HTTP server on the given port and block until callback."""
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


# ── Token exchange ─────────────────────────────────────────────────────────────

def _exchange_code(code: str, code_verifier: str, redirect_uri: str,
                   tenant_id: str) -> dict:
    import httpx

    token_endpoint = os.environ.get("HBO_TOKEN_ENDPOINT", "").strip()
    if not token_endpoint:
        raise ValueError("HBO_TOKEN_ENDPOINT is not set")

    client_id = os.environ.get("HBO_CLIENT_ID", "").strip()
    if not client_id:
        raise ValueError("HBO_CLIENT_ID is not set")

    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    client_secret = os.environ.get("HBO_CLIENT_SECRET", "").strip()
    if client_secret:
        data["client_secret"] = client_secret

    resp = httpx.post(token_endpoint, data=data, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    expires_in = int(body.get("expires_in", 3600))
    cached = {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": datetime.now(timezone.utc).timestamp() + expires_in - 60,
        "scope": body.get("scope", ""),
        "token_type": body.get("token_type", "Bearer"),
        "tenant_id": tenant_id,
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_cached(cached)
    return cached


# ── refresh ────────────────────────────────────────────────────────────────────

def _do_refresh(cached: dict) -> dict:
    import httpx

    token_endpoint = os.environ.get("HBO_TOKEN_ENDPOINT", "").strip()
    if not token_endpoint:
        raise ValueError("HBO_TOKEN_ENDPOINT is not set")

    refresh_token = cached.get("refresh_token")
    if not refresh_token:
        raise ValueError("no refresh_token in cache")

    client_id = os.environ.get("HBO_CLIENT_ID", "").strip()
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    secret = os.environ.get("HBO_CLIENT_SECRET", "").strip()
    if secret:
        data["client_secret"] = secret

    resp = httpx.post(token_endpoint, data=data, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    expires_in = int(body.get("expires_in", 3600))
    cached.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "expires_at": datetime.now(timezone.utc).timestamp() + expires_in - 60,
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save_cached(cached)
    return cached


# ── revoke ─────────────────────────────────────────────────────────────────────

def _do_revoke(cached: dict) -> None:
    revoke_endpoint = os.environ.get("HBO_REVOCATION_ENDPOINT", "").strip()
    token = cached.get("access_token") or cached.get("refresh_token")
    if revoke_endpoint and token:
        import httpx
        client_id = os.environ.get("HBO_CLIENT_ID", "").strip()
        data: dict[str, str] = {"token": token}
        if client_id:
            data["client_id"] = client_id
        secret = os.environ.get("HBO_CLIENT_SECRET", "").strip()
        if secret:
            data["client_secret"] = secret
        try:
            resp = httpx.post(revoke_endpoint, data=data, timeout=15)
            resp.raise_for_status()
            print("revocation acknowledged by server")
        except Exception as exc:
            print(f"warning: revocation request failed: {exc}", file=sys.stderr)
    else:
        print("HBO_REVOCATION_ENDPOINT not set — tokens deleted locally only")
    _delete_cached()


# ── commands ───────────────────────────────────────────────────────────────────

def cmd_authorize(args: argparse.Namespace) -> int:
    auth_endpoint = os.environ.get("HBO_AUTHORIZATION_ENDPOINT", "").strip()
    if not auth_endpoint:
        print("error: HBO_AUTHORIZATION_ENDPOINT is not set.\n"
              "  Set it to the URL from the Health Bank One developer portal.",
              file=sys.stderr)
        return 2

    client_id = os.environ.get("HBO_CLIENT_ID", "").strip()
    if not client_id:
        print("error: HBO_CLIENT_ID is not set.", file=sys.stderr)
        return 2

    redirect_uri = os.environ.get("HBO_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
    scopes = os.environ.get("HBO_SCOPES", DEFAULT_SCOPES).strip()
    if args.scopes:
        scopes = args.scopes

    code_verifier, code_challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    # Parse the redirect URI to get the callback port
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 80

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

    print(f"Opening authorization URL in browser:")
    print(f"  {auth_url}")
    print(f"\nWaiting for callback on {redirect_uri} ...")
    print(f"(timeout: {CALLBACK_TIMEOUT}s)\n")

    if not args.no_browser:
        webbrowser.open(auth_url)

    try:
        callback = _wait_for_callback(port, CALLBACK_TIMEOUT)
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("  Copy the authorization URL above and open it manually if needed.",
              file=sys.stderr)
        return 1

    if "error" in callback:
        print(f"error: {callback['error']}: {callback.get('error_description', '')}",
              file=sys.stderr)
        return 1

    if callback.get("state") != state:
        print("error: state mismatch — possible CSRF attempt", file=sys.stderr)
        return 1

    code = callback["code"]
    print("received authorization code, exchanging for tokens...")

    try:
        cached = _exchange_code(code, code_verifier, redirect_uri,
                                tenant_id=args.tenant_id)
    except Exception as exc:
        print(f"error: token exchange failed: {exc}", file=sys.stderr)
        return 1

    expiry = datetime.fromtimestamp(cached["expires_at"], tz=timezone.utc)
    print(f"tokens cached at {TOKEN_CACHE}")
    print(f"  access_token: ...{cached['access_token'][-8:]}")
    print(f"  expires: {expiry.isoformat(timespec='seconds')}")
    print(f"  refresh_token: {'yes' if cached.get('refresh_token') else 'no'}")
    print(f"  scope: {cached.get('scope', '(not returned)')}")
    print()
    print("next step:")
    print(f"  python scripts/export_healthbankone_mcp.py "
          f"--tenant-id {args.tenant_id} --discover")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cached = _load_cached()
    if not cached:
        print(f"no cached tokens at {TOKEN_CACHE}")
        return 1
    expires_at = cached.get("expires_at", 0)
    now = datetime.now(timezone.utc).timestamp()
    if expires_at:
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        remaining = max(0, expires_at - now)
        status = "VALID" if remaining > 0 else "EXPIRED"
        print(f"status: {status}")
        print(f"  expires: {expiry.isoformat(timespec='seconds')} "
              f"({remaining:.0f}s remaining)")
    else:
        print("status: UNKNOWN (no expiry recorded)")
    print(f"  tenant_id: {cached.get('tenant_id', '(not set)')}")
    print(f"  scope: {cached.get('scope', '(not set)')}")
    print(f"  refresh_token: {'yes' if cached.get('refresh_token') else 'no'}")
    print(f"  obtained_at: {cached.get('obtained_at', '(unknown)')}")
    print(f"  cache: {TOKEN_CACHE}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    cached = _load_cached()
    if not cached:
        print(f"no cached tokens at {TOKEN_CACHE}")
        return 0
    if not args.yes:
        confirm = input("Revoke HBO tokens? This cannot be undone. [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("cancelled")
            return 0
    _do_revoke(cached)
    return 0


def cmd_refresh(_args: argparse.Namespace) -> int:
    cached = _load_cached()
    if not cached:
        print(f"no cached tokens at {TOKEN_CACHE}")
        return 1
    if not cached.get("refresh_token"):
        print("error: no refresh_token in cache — run authorize again", file=sys.stderr)
        return 1
    print("refreshing tokens...")
    try:
        updated = _do_refresh(cached)
    except Exception as exc:
        print(f"error: refresh failed: {exc}", file=sys.stderr)
        return 1
    expiry = datetime.fromtimestamp(updated["expires_at"], tz=timezone.utc)
    print(f"refreshed — new expiry: {expiry.isoformat(timespec='seconds')}")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("authorize",
                             help="Open browser, complete OAuth dance, cache tokens")
    p_auth.add_argument("--tenant-id", default="ev-personal-hbo",
                        help="HealthClaw tenant ID this grant belongs to")
    p_auth.add_argument("--scopes", default=None,
                        help="Override HBO_SCOPES (space-separated)")
    p_auth.add_argument("--no-browser", action="store_true",
                        help="Print the auth URL without opening a browser")

    sub.add_parser("status", help="Show current token state")

    p_rev = sub.add_parser("revoke",
                            help="Revoke tokens at HBO and delete local cache")
    p_rev.add_argument("--yes", "-y", action="store_true",
                       help="Skip confirmation prompt")

    sub.add_parser("refresh", help="Force a token refresh using cached refresh_token")

    args = parser.parse_args()

    if args.command == "authorize":
        return cmd_authorize(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "revoke":
        return cmd_revoke(args)
    if args.command == "refresh":
        return cmd_refresh(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
