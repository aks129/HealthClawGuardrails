"""Dedicated durable-run worker for CareAgents.

The web app only persists an inbound message, creates its AgentRun, and replays
events. This process owns model inference and tool execution. A lease heartbeat
keeps a live claim from being recovered; after a crash, checkpoints and durable
tool results let another worker continue without repeating completed tools.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import uuid
from datetime import datetime, timezone

from careagents import llm
from careagents.accounts import AccountService
from careagents.agent import (MAX_TOOL_ROUNDS,
                              TOOL_LABELS, TOOLS,
                              failure_text as agent_failure_text,
                              _execute_tool, _trim_history)
from careagents.config import Config
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.personas import system_prompt

logger = logging.getLogger(__name__)


class RunCancelled(RuntimeError):
    pass


class RunDeadlineExceeded(RuntimeError):
    pass


class AmbiguousToolOutcome(RuntimeError):
    pass


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _error_class(exc: Exception) -> str:
    name = type(exc).__name__
    return name if name and name[0].isalpha() else "WorkerError"


# One source of truth for patient-facing failure text — careagents/agent.py
# owns it so the worker, the streamed chat, and the SMS collapse cannot
# drift apart again. Re-exported here because this module's callers and
# tests reach it by this path.
_failure_text = agent_failure_text


class LeaseHeartbeat:
    """Refresh one run lease independently of blocking provider calls."""

    def __init__(self, hc: HealthClawClient, run_id: str, worker_id: str,
                 lease_seconds: int):
        self.hc = hc
        self.run_id = run_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.cancel_requested = False
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{run_id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def check(self) -> None:
        if self.cancel_requested:
            raise RunCancelled("run cancellation requested")
        if self.lost:
            raise HealthClawError("worker lease was lost", 409)

    def _loop(self) -> None:
        interval = max(2.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                result = self.hc.heartbeat_agent_run(
                    self.run_id, self.worker_id, self.lease_seconds)
                self.cancel_requested = bool(result.get("cancel_requested"))
            except HealthClawError as exc:
                logger.error("heartbeat failed for run %s: %s",
                             self.run_id, exc)
                self.lost = True
                return


class RunWorker:
    def __init__(self, cfg: Config, hc: HealthClawClient,
                 accounts: AccountService, worker_id: str):
        self.cfg = cfg
        self.hc = hc
        self.accounts = accounts
        self.worker_id = worker_id

    def run_once(self) -> bool:
        run = self.hc.claim_agent_run(
            self.worker_id, self.cfg.run_lease_seconds)
        if run is None:
            return False
        self.process(run)
        return True

    def process(self, run: dict) -> None:
        run_id = str(run["id"])
        heartbeat = LeaseHeartbeat(
            self.hc, run_id, self.worker_id, self.cfg.run_lease_seconds)
        heartbeat.start()
        try:
            self._execute(run, heartbeat)
        except RunCancelled:
            heartbeat.stop()
            self.hc.transition_agent_run(
                run_id, self.worker_id, "cancelled",
                event_type="run.cancelled",
                payload={"status": "cancelled"})
        except RunDeadlineExceeded:
            heartbeat.stop()
            try:
                self.hc.transition_agent_run(
                    run_id, self.worker_id, "failed",
                    event_type="run.deadline_exceeded",
                    payload={"status": "failed"},
                    error_class="RunDeadlineExceeded")
            except HealthClawError:
                # The authoritative heartbeat may already have committed the
                # same terminal deadline transition and revoked this lease.
                logger.info("run %s was already terminal at deadline", run_id)
        except AmbiguousToolOutcome:
            heartbeat.stop()
            self.hc.append_agent_run_event(
                run_id, self.worker_id, "agent.error", {
                    "text": ("A tool may have completed before the worker "
                             "stopped. It will not be repeated until its "
                             "outcome is reconciled.")})
            self.hc.transition_agent_run(
                run_id, self.worker_id, "waiting_for_human",
                event_type="run.needs_reconciliation",
                payload={"status": "waiting_for_human"},
                error_class="AmbiguousToolOutcome")
        except Exception as exc:  # noqa: BLE001 - boundary records safe class
            heartbeat.stop()
            logger.exception("CareAgents run %s failed", run_id)
            try:
                self.hc.append_agent_run_event(
                    run_id, self.worker_id, "agent.error",
                    {"text": _failure_text(exc)})
                self.hc.transition_agent_run(
                    run_id, self.worker_id, "failed",
                    event_type="run.failed",
                    payload={"status": "failed"},
                    error_class=_error_class(exc))
            except HealthClawError:
                logger.exception("could not record failure for run %s", run_id)
        else:
            heartbeat.stop()

    def _execute(self, run: dict, heartbeat: LeaseHeartbeat) -> None:
        run_id = str(run["id"])
        tenant = str(run["tenant_id"])
        agent_id = str(run.get("agent_id") or "")
        message = run.get("message") or {}
        user_text = str(message.get("text") or "")
        if not agent_id or not user_text:
            raise ValueError("claimed run has no CareAgents agent or message")

        context = self.accounts.get_worker_agent_context(agent_id)
        if context is None or context["tenant"] != tenant:
            raise ValueError("claimed run does not match a CareAgents tenant")
        agent = context["agent"]
        prompt = system_prompt(
            agent["name"], agent["persona"], agent.get("advisor"))

        history = self.hc.recent_messages(
            tenant, limit=40, conversation_id=run["conversation_id"],
            agent_id=agent_id, through_message_id=run["message_id"])
        _trim_history(history)
        if not history or history[-1].get("role") != "user" or (
                history[-1].get("content") != user_text):
            # The claimed message is authoritative. This fallback covers a
            # projection lag without ever appending it twice on the normal path.
            history.append({"role": "user", "content": user_text})

        replay = self.hc.agent_run_events(tenant, run_id, after=0, limit=500)
        events = list(replay.get("events") or [])
        checkpoints = [event for event in events
                       if event.get("type") == "agent.checkpoint"]
        tool_results = {
            str((event.get("payload") or {}).get("provider_call_id")): (
                event.get("payload") or {})
            for event in events if event.get("type") == "agent.tool_result"
        }
        emitted = {
            (event.get("type"), str((event.get("payload") or {}).get(
                "event_key") or (event.get("payload") or {}).get(
                    "checkpoint_id") or (event.get("payload") or {}).get(
                    "provider_call_id") or ""))
            for event in events
        }

        round_number = 0
        final_checkpoint = None
        for checkpoint in checkpoints:
            payload = checkpoint.get("payload") or {}
            round_number = max(round_number, int(payload.get("round") or 0))
            calls = payload.get("tool_calls") or []
            history.append({
                "role": "assistant",
                "content": payload.get("text") or "",
                "tool_calls": calls,
                "_openai_tool_calls": payload.get("raw_tool_calls") or [],
            })
            for call in calls:
                result = tool_results.get(str(call.get("id")))
                if result is None:
                    # Only the newest checkpoint may be incomplete. Older
                    # missing results indicate corrupted recovery state.
                    if checkpoint is not checkpoints[-1]:
                        raise ValueError("incomplete historical checkpoint")
                    break
                self._emit_ui_events(
                    run["id"], str(call["id"]),
                    result.get("ui_events") or [], emitted)
                history.append({"role": "tool",
                                "tool_call_id": call["id"],
                                "content": result.get("content") or "{}"})
            if not calls:
                final_checkpoint = payload

        if final_checkpoint is not None:
            self._finish(run, final_checkpoint, emitted, heartbeat)
            return

        while True:
            heartbeat.check()
            self._check_deadline(run)

            pending_checkpoint = checkpoints[-1].get("payload") if (
                checkpoints and any(
                    str(call.get("id")) not in tool_results
                    for call in ((checkpoints[-1].get("payload") or {}).get(
                        "tool_calls") or []))) else None
            if pending_checkpoint is None:
                tools = TOOLS if round_number < MAX_TOOL_ROUNDS else []
                turn = llm.complete(self.cfg, prompt, history, tools)
                # The provider call may outlive the lease or hard deadline.
                # Never checkpoint or persist a late result after the
                # authoritative heartbeat transaction revoked ownership.
                heartbeat.check()
                self._check_deadline(run)
                round_number += 1
                checkpoint_id = f"round-{round_number}"
                pending_checkpoint = {
                    "checkpoint_id": checkpoint_id,
                    "round": round_number,
                    "text": turn.text,
                    "tool_calls": [
                        {"id": call.id, "name": call.name,
                         "arguments": call.arguments}
                        for call in turn.tool_calls],
                    "raw_tool_calls": turn.raw_tool_calls,
                }
                self.hc.append_agent_run_event(
                    run_id, self.worker_id, "agent.checkpoint",
                    pending_checkpoint)
                checkpoints.append({"type": "agent.checkpoint",
                                    "payload": pending_checkpoint})
                history.append({
                    "role": "assistant", "content": turn.text,
                    "tool_calls": pending_checkpoint["tool_calls"],
                    "_openai_tool_calls": turn.raw_tool_calls,
                })

            calls = pending_checkpoint.get("tool_calls") or []
            if not calls:
                self._finish(run, pending_checkpoint, emitted, heartbeat)
                return

            for call in calls:
                heartbeat.check()
                self._check_deadline(run)
                call_id = str(call["id"])
                result_event = tool_results.get(call_id)
                if result_event is None:
                    result_event = self._execute_durable_tool(
                        run, call, emitted)
                    tool_results[call_id] = result_event
                history.append({"role": "tool", "tool_call_id": call_id,
                                "content": result_event.get("content") or "{}"})
            # The checkpoint is now complete. A following iteration performs
            # the next model call; the completed tools remain replayable.

    def _execute_durable_tool(self, run: dict, call: dict,
                              emitted: set[tuple[str, str]]) -> dict:
        run_id = str(run["id"])
        tenant = str(run["tenant_id"])
        provider_call_id = str(call["id"])
        tool_name = str(call["name"])
        arguments = call.get("arguments") or {}

        durable = self.hc.register_agent_tool_call(
            run_id, self.worker_id, provider_call_id, tool_name, arguments)
        marker = ("agent.tool", provider_call_id)
        if marker not in emitted:
            self.hc.append_agent_run_event(
                run_id, self.worker_id, "agent.tool",
                {"provider_call_id": provider_call_id,
                 "name": tool_name,
                 "label": TOOL_LABELS.get(tool_name, tool_name)})
            emitted.add(marker)

        if durable.get("status") == "completed":
            envelope = durable.get("result") or {}
        else:
            if durable.get("status") == "running":
                self.hc.transition_agent_tool_call(
                    run_id, durable["id"], self.worker_id,
                    "needs_reconciliation",
                    error_class="WorkerLeaseExpired")
                raise AmbiguousToolOutcome(
                    "tool was running when its worker lease expired")
            if durable.get("status") == "needs_reconciliation":
                raise AmbiguousToolOutcome(
                    "tool outcome still requires reconciliation")
            self.hc.transition_agent_tool_call(
                run_id, durable["id"], self.worker_id, "running")
            side_events: list[dict] = []
            try:
                content = _execute_tool(
                    self.hc, tenant, tool_name, arguments, side_events)
            except HealthClawError as exc:
                content = json.dumps({"error": str(exc)})
            envelope = {"content": content, "ui_events": side_events}
            outcome_ref = next((
                str(event.get("action_id"))
                for event in side_events if event.get("action_id")), None)
            self.hc.transition_agent_tool_call(
                run_id, durable["id"], self.worker_id, "completed",
                result=envelope, outcome_ref=outcome_ref)

        result_payload = {
            "provider_call_id": provider_call_id,
            "content": envelope.get("content") or "{}",
            "ui_events": envelope.get("ui_events") or [],
        }
        result_marker = ("agent.tool_result", provider_call_id)
        if result_marker not in emitted:
            self.hc.append_agent_run_event(
                run_id, self.worker_id, "agent.tool_result", result_payload)
            emitted.add(result_marker)
        self._emit_ui_events(
            run_id, provider_call_id, result_payload["ui_events"], emitted)
        return result_payload

    def _emit_ui_events(self, run_id: str, provider_call_id: str,
                        ui_events: list[dict],
                        emitted: set[tuple[str, str]]) -> None:
        for index, ui_event in enumerate(ui_events):
            event_key = f"{provider_call_id}:{index}"
            marker = ("agent.card", event_key)
            if marker in emitted:
                continue
            payload = dict(ui_event)
            payload["provider_call_id"] = provider_call_id
            payload["event_key"] = event_key
            self.hc.append_agent_run_event(
                run_id, self.worker_id, "agent.card", payload)
            emitted.add(marker)

    def _finish(self, run: dict, checkpoint: dict,
                emitted: set[tuple[str, str]], heartbeat: LeaseHeartbeat) -> None:
        heartbeat.check()
        self._check_deadline(run)
        run_id = str(run["id"])
        text = str(checkpoint.get("text") or "").strip() or "…"
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "final")
        marker = ("agent.text", checkpoint_id)
        # HealthClaw owns the final fencing transaction. A client-side
        # heartbeat check cannot atomically order transcript persistence
        # against a concurrent deadline sweep or lease recovery.
        self.hc.finalize_agent_run(
            run_id, self.worker_id, text, checkpoint_id)
        emitted.add(marker)

    @staticmethod
    def _check_deadline(run: dict) -> None:
        deadline = _parse_time(run.get("deadline_at"))
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise RunDeadlineExceeded("run deadline exceeded")


def _worker_base_id() -> str:
    """Name this process instance, not just this host and PID.

    HealthClaw hands a running run back to the worker id that claimed it when
    the claim response was lost (#374), which is only safe while one id means
    one live claim loop. A container restart can reuse both the hostname and
    the PID — PID 1 under Docker is the ordinary case — so the instance suffix
    is what keeps a restarted process from being handed the dead one's run.
    """
    host = socket.gethostname().split(".")[0][:48]
    return f"careagents-{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def run_worker_pool(cfg: Config, stop: threading.Event | None = None) -> None:
    """Run a fixed-size pool; each slot owns its HTTP session and lease id."""
    stop = stop or threading.Event()
    accounts = AccountService(cfg)
    base_id = _worker_base_id()

    def loop(slot: int) -> None:
        hc = HealthClawClient(cfg.healthclaw_base, cfg.mint_secret)
        worker = RunWorker(cfg, hc, accounts, f"{base_id}-{slot}")
        while not stop.is_set():
            try:
                worked = worker.run_once()
            except HealthClawError as exc:
                logger.error("run claim failed: %s", exc)
                worked = False
            if not worked:
                stop.wait(cfg.run_poll_seconds)

    threads = [threading.Thread(target=loop, args=(slot,), daemon=False,
                                name=f"careagents-worker-{slot}")
               for slot in range(cfg.run_worker_concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    cfg = Config()
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_worker_pool(cfg, stop)


if __name__ == "__main__":
    main()
