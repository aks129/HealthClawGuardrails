#!/usr/bin/env python3
"""CareAgents iMessage relay — runs on the Mac mini, bridges Messages.app to
CareAgents.

It is a *transport only*: it carries no PHI logic and holds no credentials
beyond the shared mint secret. Every inbound text is forwarded to CareAgents,
which resolves the sender's handle to a bound agent, runs the guardrailed turn
server-side, and returns the reply for this script to send back.

Flow per inbound message:
  - "care <code>"  → POST /api/surfaces/imessage/bind   {code, handle}
  - anything else  → POST /api/surfaces/imessage/inbound {handle, text} → reply

Requires macOS **Full Disk Access** for the interpreter (to read
~/Library/Messages/chat.db) and Automation permission for Messages.

Env:
  CAREAGENTS_BASE      default https://careagents.cloud
  CAREAGENTS_MINT_SECRET   the HEALTHCLAW_MINT_SECRET (X-Internal-Secret)  [required]
  IMESSAGE_POLL_SECONDS    default 3
  IMESSAGE_STATE_FILE      default ~/.careagents-imessage-relay.json

Run under launchd/systemd-equivalent (a keepalive LaunchAgent). Not imported by
the CareAgents app.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("CAREAGENTS_BASE", "https://careagents.cloud").rstrip("/")
SECRET = os.environ.get("CAREAGENTS_MINT_SECRET", "")
POLL = float(os.environ.get("IMESSAGE_POLL_SECONDS", "3"))
STATE_FILE = Path(os.environ.get(
    "IMESSAGE_STATE_FILE",
    str(Path.home() / ".careagents-imessage-relay.json")))
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
HTTP_TIMEOUT = 60  # a turn can take a while (LLM + tools)


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "X-Internal-Secret": SECRET})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        print(f"[relay] {path} HTTP {exc.code}: {body}", file=sys.stderr)
    except Exception as exc:  # network, timeout, JSON
        print(f"[relay] {path} failed: {type(exc).__name__}", file=sys.stderr)
    return {}


def _send_imessage(handle: str, text: str) -> None:
    """Send `text` to `handle` via Messages.app (AppleScript)."""
    script = (
        'on run {targetHandle, msg}\n'
        '  tell application "Messages"\n'
        '    set svc to 1st service whose service type = iMessage\n'
        '    send msg to buddy targetHandle of svc\n'
        '  end tell\n'
        'end run')
    try:
        subprocess.run(["osascript", "-e", script, handle, text],
                       check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        print(f"[relay] send to {handle} failed: "
              f"{exc.stderr.decode(errors='replace')[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"[relay] send error: {type(exc).__name__}", file=sys.stderr)


def _load_last_rowid() -> int:
    try:
        return int(json.loads(STATE_FILE.read_text()).get("last_rowid", 0))
    except Exception:
        return 0


def _save_last_rowid(rowid: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"last_rowid": rowid}))
    except Exception as exc:
        print(f"[relay] state write failed: {type(exc).__name__}",
              file=sys.stderr)


def _new_inbound(last_rowid: int) -> list[tuple[int, str, str]]:
    """Return (rowid, handle, text) for inbound messages after last_rowid.

    Reads chat.db read-only. `text` can be NULL for attachment-only messages;
    those are skipped. Apple stores some bodies in attributedBody only — we
    take the plain `text` column and skip rows without it.
    """
    uri = f"file:{CHAT_DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT m.ROWID, h.id, m.text "
            "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.text IS NOT NULL "
            "ORDER BY m.ROWID ASC LIMIT 50",
            (last_rowid,)).fetchall()
    finally:
        con.close()
    return [(r[0], r[1], r[2]) for r in rows if r[1] and r[2]]


def _handle_message(handle: str, text: str) -> None:
    stripped = text.strip()
    low = stripped.lower()
    if low.startswith("care ") or low.startswith("care_"):
        code = stripped[5:].strip()
        res = _post("/api/surfaces/imessage/bind",
                    {"code": code, "handle": handle})
        if res.get("ok"):
            _send_imessage(handle, "You're connected — I'm your CareAgent. "
                                   "Ask me anything about your records.")
        else:
            _send_imessage(handle, "That code didn't match. Generate a fresh "
                                   "one in the CareAgents app and try again.")
        return
    res = _post("/api/surfaces/imessage/inbound",
                {"handle": handle, "text": stripped})
    reply = res.get("reply")
    if reply:
        _send_imessage(handle, reply)
    # No reply + no error → handle isn't bound; stay silent (don't spam
    # strangers who text the number).


def main() -> int:
    if not SECRET:
        print("[relay] CAREAGENTS_MINT_SECRET is required", file=sys.stderr)
        return 2
    if not CHAT_DB.exists():
        print(f"[relay] chat.db not found at {CHAT_DB} — grant Full Disk "
              "Access to this interpreter", file=sys.stderr)
        return 2
    last = _load_last_rowid()
    if last == 0:
        # First run: start from the newest row so we don't replay history.
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        last = con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message"
                           ).fetchone()[0]
        con.close()
        _save_last_rowid(last)
    print(f"[relay] watching {CHAT_DB} from ROWID {last}; base {BASE}")
    while True:
        try:
            for rowid, handle, text in _new_inbound(last):
                _handle_message(handle, text)
                last = rowid
                _save_last_rowid(last)
        except Exception as exc:  # keep the loop alive
            print(f"[relay] poll error: {type(exc).__name__}", file=sys.stderr)
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
