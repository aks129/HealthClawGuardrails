#!/usr/bin/env python3
"""CareAgents iMessage relay — runs on the Mac mini, bridges Messages.app to
CareAgents.

It is a *transport only*: it carries no PHI logic and holds no credentials
beyond the shared mint secret. Every inbound text is forwarded to CareAgents,
which resolves the sender's handle to a bound agent and durably queues the
turn. This relay polls the run projection; inference never runs in the web
request that accepted the message.

Flow per inbound message (keywords, pairing codes and sign-in links are
decided on the server, so this stays a transport):
  POST /api/surfaces/imessage/inbound {handle, text, request_id}
    → {reply?, run_id?}: send `reply` now if present, then
  GET /api/surfaces/imessage/runs/<id> until it answers → send its reply.
A run that never answers gets TIMEOUT_TEXT, not silence. Messages are worked
on a small thread pool, one sender at a time in arrival order.

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
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = os.environ.get("CAREAGENTS_BASE", "https://careagents.cloud").rstrip("/")
SECRET = os.environ.get("CAREAGENTS_MINT_SECRET", "")
POLL = float(os.environ.get("IMESSAGE_POLL_SECONDS", "3"))
STATE_FILE = Path(os.environ.get(
    "IMESSAGE_STATE_FILE",
    str(Path.home() / ".careagents-imessage-relay.json")))
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
HTTP_TIMEOUT = 20
RUN_TIMEOUT = 180
WORKERS = 4
_HOST = urllib.parse.urlparse(BASE).netloc or "careagents.cloud"
TIMEOUT_TEXT = f"That took too long. Please try again or open {_HOST}."


def _mask(handle: str) -> str:
    """Never print a full phone number or Apple ID."""
    if "@" in handle:
        name, _, domain = handle.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"***{handle[-2:]}" if len(handle) > 2 else "***"


def _post(path: str, payload: dict) -> dict:
    """The JSON answer, including an error's: a 503 can carry the `reply`
    the texter should read."""
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "X-Internal-Secret": SECRET})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"[relay] {path} HTTP {exc.code}", file=sys.stderr)
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}
    except Exception as exc:  # network, timeout, JSON
        print(f"[relay] {path} failed: {type(exc).__name__}", file=sys.stderr)
    return {}


def _get(path: str, params: dict) -> tuple[int, dict]:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BASE}{path}?{query}", method="GET",
        headers={"X-Internal-Secret": SECRET})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"[relay] runs HTTP {exc.code}", file=sys.stderr)
        return exc.code, {}
    except Exception as exc:
        print(f"[relay] runs failed: {type(exc).__name__}", file=sys.stderr)
    return 0, {}


_send_lock = threading.Lock()


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
        # One AppleScript send at a time: Messages is not driven in parallel.
        with _send_lock:
            subprocess.run(["osascript", "-e", script, handle, text],
                           check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        print(f"[relay] send to {_mask(handle)} failed: exit "
              f"{exc.returncode}", file=sys.stderr)
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


def _handle_message(handle: str, text: str, rowid: int = 0) -> None:
    payload = {"handle": handle, "text": text.strip()}
    if rowid:
        payload["request_id"] = f"imessage-{rowid}"
    res = _post("/api/surfaces/imessage/inbound", payload)
    if res.get("reply"):
        _send_imessage(handle, res["reply"])
    run_id = res.get("run_id")
    if not run_id:
        return  # nothing queued: a keyword, a link, or a stranger to ignore
    deadline = time.monotonic() + RUN_TIMEOUT
    while time.monotonic() < deadline:
        status, result = _get(
            f"/api/surfaces/imessage/runs/{run_id}", {"handle": handle})
        if status == 200:
            if result.get("reply"):
                _send_imessage(handle, result["reply"])
            return
        if status in (403, 404):
            return  # unbound since (STOP), no such run, or a bad secret
        time.sleep(1)  # 202 still running; 0 or 5xx retried (#410)
    print(f"[relay] run {run_id} timed out", file=sys.stderr)
    _send_imessage(handle, TIMEOUT_TEXT)


class _Dispatcher:
    """A small pool, so one slow answer does not hold up everyone else,
    while each sender's messages are still handled one at a time, in order."""

    def __init__(self, workers: int = WORKERS):
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self._queues: dict[str, deque] = {}

    def submit(self, handle: str, text: str, rowid: int = 0) -> None:
        with self._lock:
            queue = self._queues.get(handle)
            if queue is not None:
                queue.append((text, rowid))
                return
            self._queues[handle] = deque([(text, rowid)])
        self._pool.submit(self._drain, handle)

    def _drain(self, handle: str) -> None:
        while True:
            with self._lock:
                queue = self._queues[handle]
                if not queue:
                    del self._queues[handle]
                    return
                text, rowid = queue.popleft()
            try:
                _handle_message(handle, text, rowid)
            except Exception as exc:  # keep this sender's queue moving
                print(f"[relay] message error: {type(exc).__name__}",
                      file=sys.stderr)


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
    dispatcher = _Dispatcher()
    while True:
        try:
            for rowid, handle, text in _new_inbound(last):
                # Advanced on dispatch: a crash mid-answer loses that answer
                # rather than replaying the message on restart.
                dispatcher.submit(handle, text, rowid)
                last = rowid
                _save_last_rowid(last)
        except Exception as exc:  # keep the loop alive
            print(f"[relay] poll error: {type(exc).__name__}", file=sys.stderr)
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
