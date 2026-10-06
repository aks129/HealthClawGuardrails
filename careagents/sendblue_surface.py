"""Sendblue in front of the text-message core (careagents.imessage).

Two halves:

- The webhook, in the web process. It checks Sendblue's shared secret,
  drops what is not a one-to-one inbound text, remembers each
  message_handle so a Sendblue retry is not answered twice, and hands the
  text to `imessage.handle_inbound`. That call queues the turn and returns;
  it never waits on the agent. An immediate reply (a sign-in link, HELP, a
  refusal) is sent from a background thread after the 200.
- The Deliverer, in the run worker. A turn's answer is owed until the run
  finishes; the owed row is in the database, so a web restart loses
  nothing. The worker reads the run, sends its text once, and marks it.

Nothing here logs a message body or a full number (imessage.mask), and the
database keeps pointers only (models.SendblueMessage).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from flask import jsonify, request

from careagents import imessage, sendblue
from careagents.accounts import secret_matches
from careagents.healthclaw import HealthClawError

logger = logging.getLogger(__name__)

PHOTO_TEXT = "I can only read text for now, so I didn't see the photo."


def real_records_text(origin: str) -> str:
    host = origin.split("://", 1)[-1].rstrip("/") or origin
    return ("Your assistant is using your real records, so I can't send "
            f"answers by text yet. Open {host} to read it.")


def real_records_blocked(cfg, ctx: dict) -> bool:
    """Whether this agent's answers must stay off the Sendblue line.

    Sendblue carries real records only once its HIPAA instance and BAA are
    in place, and SENDBLUE_REAL_RECORDS says so. Anything but the sample
    connection counts as real, so a new connection kind is held back too.
    """
    if cfg.sendblue_real_records:
        return False
    return (ctx.get("connection") or {}).get("kind") != "sample"
#: How often the worker looks for finished runs to deliver.
DELIVERY_POLL_SECONDS = 2.0


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="sendblue-reply", daemon=True).start()


@dataclass
class SendblueSurface:
    """What the webhook needs at request time. Tests swap both."""
    client: object                              # sendblue.Client
    spawn: Callable[[Callable[[], None]], None] = _spawn_thread


def _key(message_handle: str) -> str:
    return hashlib.sha256(message_handle.encode()).hexdigest()


def _ignored(body: dict) -> str | None:
    """Why this webhook is not a one-to-one inbound text, or None."""
    if body.get("is_outbound"):
        return "outbound"
    if body.get("group_id"):
        return "group"
    for field in ("type", "webhook_type"):
        kind = str(body.get(field) or "").strip().lower()
        if kind and kind != "receive":
            return "not a receive"
    status = str(body.get("status") or "").strip().upper()
    if status and status != "RECEIVED":
        return "not a receive"
    return None


def register(app, cfg, svc, deps: imessage.Deps) -> None:
    """Add the webhook route. It answers 404 while Sendblue is off."""
    ext = SendblueSurface(client=sendblue.Client(cfg))
    app.extensions["careagents_sendblue"] = ext

    def _send_later(to: str, replies: list[str], typing: bool) -> None:
        def job() -> None:
            try:
                for reply in replies:                # in order
                    ext.client.send_message(to, reply)
                if typing:
                    ext.client.send_typing(to)       # best effort
            except Exception as exc:  # noqa: BLE001 - a thread must not die loud
                logger.warning("sendblue reply to %s failed: %s",
                               imessage.mask(to), type(exc).__name__)
        ext.spawn(job)

    @app.post("/api/surfaces/sendblue/webhook")
    def sendblue_webhook():
        if not cfg.sendblue_enabled:
            return jsonify({"error": "not found"}), 404
        got = request.headers.get("sb-signing-secret") or ""
        # secret_matches hashes both sides first (no header shape can raise,
        # #557) and an empty configured secret matches nothing.
        if not got or not secret_matches(got, cfg.sendblue_webhook_secret):
            logger.warning("sendblue webhook refused: bad signing secret")
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "invalid body"}), 400
        why = _ignored(body)
        if why:
            return jsonify({"ok": True, "ignored": why}), 200
        message_handle = str(body.get("message_handle") or "").strip()
        raw = str(body.get("from_number") or "").strip()
        if not message_handle or not raw:
            return jsonify({"error": "missing message_handle or sender"}), 400

        key = _key(message_handle)
        if not svc.sendblue_claim_inbound(key):
            return jsonify({"ok": True, "duplicate": True}), 200
        content = str(body.get("content") or "")
        handle = imessage.normalize_handle(raw)
        to = handle or raw
        has_media = bool(body.get("media_url"))
        run_id = None
        try:
            if not content.strip() and has_media:
                # Only to a number we can text that has not said STOP.
                out, status = ({"reply": PHOTO_TEXT}, 200) if (
                    handle and not svc.imessage_opted_out(handle)) else (
                    {}, 200)
            else:
                # The engine's request ids are [A-Za-z0-9._:-]; Sendblue's
                # handle format is not ours to promise, so it is hashed.
                out, status = imessage.handle_inbound(
                    deps, raw, content, request_id=f"sendblue-{key[:32]}",
                    transport_block=lambda ctx: (
                        real_records_text(cfg.origin)
                        if real_records_blocked(cfg, ctx) else None))
                if has_media and (out.get("reply") or out.get("run_id")) and (
                        imessage.keyword(content) is None):
                    # The caption is answered; the photo is said to be
                    # unseen, first. Silence (an opted-out number) stays
                    # silence, and a keyword's confirmation stays one text.
                    out = {**out, "notice": PHOTO_TEXT}
            run_id = out.get("run_id")
            if run_id:
                surface = svc.find_surface_by_handle(to, kind="imessage",
                                                     also=raw)
                if surface:
                    svc.sendblue_track_run(key, surface["id"], str(run_id))
        except Exception as exc:  # noqa: BLE001 - let Sendblue retry it
            logger.error("sendblue inbound from %s failed: %s",
                         imessage.mask(to), type(exc).__name__)
            svc.sendblue_release_inbound(key)
            return jsonify({"error": "inbound failed"}), 500
        logger.info("sendblue inbound from %s: %s%s", imessage.mask(to),
                    status, " (run queued)" if run_id else "")
        replies = [r for r in (out.get("notice"), out.get("reply")) if r]
        if replies or run_id:
            _send_later(to, replies, bool(run_id))
        return jsonify({"ok": True}), 200


class Deliverer:
    """Sends each finished run's answer once. Runs in the worker process."""

    def __init__(self, cfg, hc, svc, client, clock=time.time):
        self.cfg = cfg
        self.hc = hc
        self.svc = svc
        self.client = client
        self.clock = clock
        # The relay waited 180s; a longer run deadline gets its own minute.
        self.timeout_seconds = max(180, cfg.run_deadline_seconds + 60)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.once()
            except Exception as exc:  # noqa: BLE001 - keep delivering
                logger.warning("sendblue delivery pass failed: %s",
                               type(exc).__name__)
            stop.wait(DELIVERY_POLL_SECONDS)

    def once(self) -> None:
        for row in self.svc.sendblue_pending():
            try:
                self._deliver(row)
            except Exception as exc:  # noqa: BLE001 - one row, not the pass
                logger.warning("sendblue delivery of run %s failed: %s",
                               row["run_id"], type(exc).__name__)

    def _close(self, row: dict, outcome: str) -> bool:
        return self.svc.sendblue_mark_delivered(row["id"], outcome)

    def _deliver(self, row: dict) -> None:
        surface = (self.svc.get_surface(row["surface_id"])
                   if row.get("surface_id") else None)
        if (surface is None or surface.get("kind") != "imessage"
                or not surface.get("agent_id") or not surface.get("handle")):
            # STOP or disconnect since: the answer is not sent anywhere.
            self._close(row, "unbound")
            return
        if imessage.reverify_due(surface, self.clock(),
                                 self.cfg.imessage_reverify_days * 86400):
            # Queued while fresh, due now (#871): whoever holds the number
            # may not be its owner. Nothing is sent.
            self._close(row, "withheld")
            return
        agent_id = surface["agent_id"]
        ctx = self.svc.get_agent_context(surface["account_id"], agent_id)
        if not ctx:
            self._close(row, "unbound")
            return
        tenant, run_id = ctx["tenant"], row["run_id"]
        page = None
        try:
            run = self.hc.get_agent_run(tenant, run_id)
            if (run.get("tenant_id") != tenant
                    or run.get("agent_id") != agent_id):
                self._close(row, "absent")
                return
            page = self.hc.agent_run_events(tenant, run_id, after=0,
                                            limit=500)
        except HealthClawError as exc:
            if exc.status == 404:
                self._close(row, "absent")
                return
            # Could not ask: try again next pass, unless it is too late.
        if page is not None and page.get("status") in imessage.FINAL_STATUSES:
            if real_records_blocked(self.cfg, ctx):
                # Read fresh at delivery: a connection switched to real
                # records after the turn was queued still holds it back.
                text, outcome = real_records_text(self.cfg.origin), "withheld"
            else:
                text = imessage.run_reply(page.get("events") or [],
                                          self.cfg.origin, agent_id)
                outcome = "sent"
        elif self.clock() - float(row.get("created_at") or 0) >= (
                self.timeout_seconds):
            text, outcome = imessage.timeout_text(self.cfg.origin), "timeout"
        else:
            return
        # Closed before sending: two workers never both send, and a crash
        # between here and the send loses this answer rather than repeating
        # it.
        if not self._close(row, outcome):
            return
        # A long answer goes as several texts, in order. A part that fails
        # stops the rest: a later part without the one before it reads as
        # a different answer.
        for part in imessage.split_reply(text, self.cfg.origin, agent_id):
            if not self.client.send_message(surface["handle"], part).ok:
                self.svc.sendblue_set_outcome(row["id"], "failed")
                return
