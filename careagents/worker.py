"""Dedicated durable-run worker for CareAgents.

The web app only persists an inbound message, creates its AgentRun, and replays
events. This process owns model inference and tool execution. A lease heartbeat
keeps a live claim from being recovered; after a crash, checkpoints and durable
tool results let another worker continue without repeating completed tools.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import threading
import traceback
import uuid
from datetime import datetime, timezone

from careagents import beta, causes, checkins, funnel, llm, routing
from careagents import tester_terms, text_self
from careagents.accounts import AccountService
from careagents.agent import (GENERIC_FAILURE_TEXT, MAX_TOOL_ROUNDS,
                              SAMPLE_TOOL_LABELS, TOOL_LABELS, tools_for_turn,
                              failure_text as agent_failure_text,
                              records_arriving,
                              tool_error_result as agent_tool_error_result,
                              _execute_tool, _trim_history)
from careagents.config import Config
from careagents.healthclaw import (HealthClawClient, HealthClawError, act_as,
                                   acting_for)
from careagents.hub import relation_of
from careagents.personas import system_prompt

logger = logging.getLogger(__name__)


def _plain(text: str) -> str:
    """A sentence reduced for comparison: no markdown emphasis, quotes or
    apostrophes, one case, single spaces, no end punctuation."""
    text = re.sub(r"[*_`~>\"'\u2018\u2019\u201c\u201d]", "", text).lower()
    return " ".join(text.split()).strip(" .!:;,-")


#: A sentence that says the records are made up: one of these words ...
_MADE_UP = ("made-up", "made up", "sample", "fictional", "fake")
#: ... and one of these phrases, matched as whole words ("not young" is
#: not "not you").
_NOT_YOURS = (("not", "yours"), ("arent", "yours"), ("isnt", "yours"),
              ("not", "about", "you"), ("isnt", "about", "you"),
              ("not", "your"), ("arent", "your"), ("not", "you"))
#: The only words a disclaimer is made of. A sentence is dropped only when
#: every word in it is one of these, so anything it says about the sample,
#: a condition, a medicine, a number or advice, keeps it (#908 QA and
#: security): "The sample person takes metformin, which is not yours" stays.
_DISCLAIMER_WORDS = frozenset("""
    these this those they them it its are is were be been all just only
    the a an and or of for to in on as so from with that which here there
    records record data chart file person persons patient people someone
    anyone else nobody nothing anything about you your yours not arent
    isnt dont doesnt made up made-up sample fictional fake example demo
    real belong belongs reminder note remember please again quick
    know own fyi mind keep worth noting heads by way
    """.split())
#: A disclaimer is short. Anything longer is treated as content.
_DISCLAIMER_MAX_WORDS = 16


def _has_phrase(words: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(words[i:i + n]) == phrase
               for i in range(len(words) - n + 1))


def _disclaimer_only(s: str) -> bool:
    """A plain sentence that only says the records are made up and not the
    reader's: short, and every word one a disclaimer is made of."""
    words = re.findall(r"[a-z0-9-]+", s.replace("'", ""))
    return (0 < len(words) <= _DISCLAIMER_MAX_WORDS
            and all(w in _DISCLAIMER_WORDS for w in words)
            and any(m in s for m in _MADE_UP)
            and any(_has_phrase(words, p) for p in _NOT_YOURS))


def _drop_echo(text: str, lead: str) -> str:
    """`text` without the model's own copy of `lead` at its start.

    The line is in the stored history, so a model may open with it, bold,
    quoted or reworded, and the answer would say it twice (#908 QA). Only
    leading sentences of the first paragraph are dropped, and only when one
    is the line itself or is a disclaimer and nothing else (no number, no
    advice, no clinical word, short). The rest of the answer is untouched."""
    lead_sentences = {_plain(s) for s in re.split(r"(?<=[.!?])\s+", lead)}
    first, sep, rest = text.partition("\n\n")
    # Sentences with the whitespace after each kept, so what stays keeps
    # its own line breaks: the 911 and 988 lines are two lines (#1017).
    parts = re.split(r"(?<=[.!?])(\s+)", first.strip())
    while parts:
        s = _plain(parts[0])
        if not s or s in lead_sentences or _disclaimer_only(s):
            del parts[:2]
            continue
        break
    first = "".join(parts)
    return (first + sep + rest if first else rest).strip()


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


def _said(run: dict) -> str:
    """The person's message on a claimed run, for beta.emergency_first."""
    return str((run.get("message") or {}).get("text") or "")


def _error_class(exc: Exception) -> str:
    name = type(exc).__name__
    return name if name and name[0].isalpha() else "WorkerError"


def _raised_at(exc: BaseException) -> str:
    """Where `exc` was raised, as file:line in function. Code, not data:
    unlike a formatted traceback it carries no part of the message."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "-"
    f = frames[-1]
    return f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}"


# One source of truth for patient-facing failure text — careagents/agent.py
# owns it so the worker, the streamed chat, and the SMS collapse cannot
# drift apart again. Re-exported here because this module's callers and
# tests reach it by this path.
_failure_text = agent_failure_text


#: The run event that says this run's daily turn is already charged. Empty
#: payload; the browser projection drops event types it does not know.
CHARGED_EVENT = "agent.charged"

#: The run event that says this failed run's turn was given back. Its
#: payload is the UTC day the turn was charged on, as CHARGED_EVENT's is.
REFUNDED_EVENT = "agent.charge_refunded"

#: Consecutive heartbeat failures before the lease is given up. The interval
#: is a third of the lease, so two misses still leave a third of it to
#: recover in; one miss ending the run was the whole of the defect.
_HEARTBEAT_MAX_MISSES = 3


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
        self._misses = 0
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

    def _beat_once(self) -> None:
        """One heartbeat. Gives the lease up only after repeated failures.

        A single failure used to end the run. The interval is a third of the
        lease, so one blip shorter than the remaining two thirds costs
        nothing — while abandoning the turn cost the patient an answer the
        model may already have produced, reported as "something went wrong on
        our side". Recovery then waited out the full lease inside a shorter
        deadline, so the run usually died rather than being retried.

        Consecutive failures still lose it: if the engine is genuinely gone,
        holding a slot for a lease nobody is honouring helps no one.
        """
        try:
            result = self.hc.heartbeat_agent_run(
                self.run_id, self.worker_id, self.lease_seconds)
        except HealthClawError as exc:
            self._misses += 1
            logger.warning("heartbeat %d/%d failed for run %s: %s",
                           self._misses, _HEARTBEAT_MAX_MISSES,
                           self.run_id, exc)
            if self._misses >= _HEARTBEAT_MAX_MISSES:
                logger.error("lease lost for run %s after %d misses",
                             self.run_id, self._misses)
                self.lost = True
            return
        self._misses = 0
        self.cancel_requested = bool(result.get("cancel_requested"))

    def _loop(self) -> None:
        interval = max(2.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            self._beat_once()
            if self.lost:
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
            logger.warning("CareAgents run %s failed: cause=%s", run_id,
                           causes.DEADLINE)
            funnel.safely(funnel.count_chat_error, self.accounts.session,
                          causes.DEADLINE)
            refund = self._mark_refund(run)
            try:
                self.hc.transition_agent_run(
                    run_id, self.worker_id, "failed",
                    event_type="run.deadline_exceeded",
                    payload={"status": "failed"},
                    error_class="RunDeadlineExceeded")
                self._refund(refund)
                # This worker failed it, so this worker answers. When the
                # engine got there first it wrote the answer itself.
                self._answer_failed_turn(run, beta.with_911(
                    _failure_text(RunDeadlineExceeded()), _said(run)))
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
            # The class, the cause code and where it was raised (#926);
            # never the message, which can carry the question or answer.
            cause = causes.cause_of(exc)
            logger.error("CareAgents run %s failed: %s cause=%s at %s",
                         run_id, _error_class(exc), cause, _raised_at(exc))
            funnel.safely(funnel.count_chat_error, self.accounts.session,
                          cause)
            if causes.needs_owner(exc):
                funnel.safely(funnel.alert_owner, self.accounts.session,
                              self.cfg, cause)
            text = beta.with_911(
                _failure_text(exc, owner_told=bool(self.cfg.owner_emails)),
                _said(run))
            refund = self._mark_refund(run)
            try:
                self.hc.append_agent_run_event(
                    run_id, self.worker_id, "agent.error",
                    {"text": text})
                self.hc.transition_agent_run(
                    run_id, self.worker_id, "failed",
                    event_type="run.failed",
                    payload={"status": "failed"},
                    error_class=_error_class(exc))
                self._refund(refund)
                # Only once the engine took this worker's failure: a worker
                # that lost its lease lands here too, and the run may yet
                # finish elsewhere with a real answer.
                self._answer_failed_turn(run, text)
            except HealthClawError:
                logger.exception("could not record failure for run %s", run_id)
        else:
            heartbeat.stop()

    def _mark_refund(self, run: dict) -> tuple[str, str] | None:
        """Before a failed run is closed: if its turn was charged and not
        yet given back, mark it given back on the run and return (account,
        day) for `_refund`. None when there is nothing to give back.

        A turn that failed on our side must not use up the day's cap. The
        marker goes first, while this worker still owns the run (the
        engine takes no events on a closed run), so no run is given back
        twice: a second worker sees the marker. The turn itself is given
        back only once this worker's failure is accepted (`_refund`): a
        worker that lost its lease lands in the same branch, and the run
        may yet finish elsewhere. Every miss errs toward the cap, never
        past it. A charge from before the day was recorded is kept.

        It reads what `_execute_turn` left on the run (`_refund`), never the
        engine again: this branch runs outside the turn's `acting_for`
        block, so a read here would not be labelled with the account."""
        info = run.get("_refund") or {}
        account_id, day = info.get("account_id"), info.get("day")
        if info.get("refunded") or not account_id or not day:
            return None
        if info.get("answered"):
            # A model round already answered, so the model call was paid
            # for: the turn counts, as any answered turn does.
            return None
        try:
            self.hc.append_agent_run_event(
                str(run["id"]), self.worker_id, REFUNDED_EVENT, {"day": day})
            info["refunded"] = True
            return account_id, day
        except Exception:  # noqa: BLE001 - a refund never fails a failure
            logger.warning("could not mark run %s for a refund", run.get("id"))
            return None

    def _refund(self, refund: tuple[str, str] | None) -> None:
        """Give back the turn `_mark_refund` marked. Never raises."""
        if refund is None:
            return
        try:
            self.accounts.refund_daily_turn(*refund)
        except Exception:  # noqa: BLE001 - a refund never fails a failure
            logger.warning("could not give back a failed turn")

    def _mark(self, account_id: str, milestone: str) -> None:
        """A funnel milestone (careagents/funnel.py). Never fails a turn."""
        funnel.safely(funnel.mark, self.accounts.session, account_id,
                      milestone)

    def _answer_failed_turn(self, run: dict, text: str) -> None:
        """Put the failure sentence in the transcript as the turn's answer.

        A completed run's answer is written by finalize; a failed run wrote
        nothing, so on reload the question stood alone and asking again put
        it twice in a row (#876). The text is one of agent.py's fixed
        sentences, never the exception. One request id per run, so a
        replayed failure writes it once. Losing it costs only the line."""
        tenant = str(run.get("tenant_id") or "")
        if not tenant:
            return
        self.hc.log_message(
            tenant, "assistant", text,
            agent_id=str(run.get("agent_id") or "") or None,
            conversation_id=run.get("conversation_id"),
            surface=str(run.get("surface") or "web"),
            reply_to=run.get("message_id"),
            request_id=f"run:{run['id']}:failed")

    def _execute(self, run: dict, heartbeat: LeaseHeartbeat) -> None:
        # The turn's tenant reads are labelled with the account that asked
        # (spec 6.2), and only for this turn; the run's own lifecycle calls
        # stay careagents-worker.
        with acting_for(None):
            self._execute_turn(run, heartbeat)

    def _execute_turn(self, run: dict, heartbeat: LeaseHeartbeat) -> None:
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
        act_as(context["account_id"])
        blocked = beta.turn_block(context["connection"], context["paused"],
                                  tester_terms.CONSENT_VERSION)
        if blocked:
            # Before any record read and before any model call (beta spec
            # 4.3 and 4.6). The run still ends normally, with this sentence.
            self._finish(run, {"text": beta.with_911(blocked, user_text),
                               "checkpoint_id": "blocked"},
                         set(), heartbeat)
            return
        if (context["connection"].get("kind") != "sample"
                and not self.cfg.model_host_vetted):
            # Fail closed per turn, before any record read or model call:
            # real records go only to an approved model host. The boot check
            # in config.py runs only where CARE_REAL_RECORDS is set, and this
            # process may not have it. Anything not sample counts as real.
            raise llm.ModelHostNotApproved(
                "model host not approved for real records")
        if context["connection"].get("kind") != "sample":
            # The weekly number (beta spec 4.5): integers only. A retried run
            # can count twice; the weekly figure counts accounts, so that
            # does not move it.
            try:
                self.accounts.count_activity(context["account_id"], "asked")
            except Exception:  # noqa: BLE001 - a count never fails a turn
                logger.warning("could not count activity for run %s", run_id)
        agent = context["agent"]
        # Made-up records answer as made-up, on every surface: the prompt
        # says whose they are, and the answer ends with beta.SAMPLE_FRAME.
        sample = context["connection"].get("kind") == "sample"
        # The engine tags each run with the surface its message arrived on;
        # a text thread gets the texting style (careagents/personas.py).
        # From the engine's import, not the stored status (QA945-F1).
        arriving = records_arriving(self.hc, tenant, context["connection"])
        # Someone the holder cares for, by relationship ("your parent"),
        # in the prompt and the sample line alike (FHI-44).
        who = relation_of(context.get("person") or {})
        prompt = system_prompt(
            agent["name"], agent["persona"], agent.get("advisor"),
            surface=str(run.get("surface") or ""), sample=sample,
            arriving=arriving, routines=agent.get("routines"), who=who)

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
                "advisor_calls": payload.get("advisor_calls") or 0,
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

        charged = any(event.get("type") == CHARGED_EVENT for event in events)
        # What a failure needs to give the turn back, kept on the run, so
        # the failure branch reads nothing more from the engine
        # (`_mark_refund`): the account, the day it was charged, whether it
        # was already given back, and whether a model round has answered.
        charge = next((e for e in events if e.get("type") == CHARGED_EVENT),
                      None)
        run["_refund"] = {
            "account_id": context["account_id"],
            "day": str(((charge or {}).get("payload") or {}).get("day") or ""),
            "refunded": any(e.get("type") == REFUNDED_EVENT for e in events),
            "answered": bool(checkpoints)}
        if not checkpoints and not charged:
            # The day's allowance is charged here, where the model is called
            # and after turn_block passed (#856 sign-off F4): a turn admitted
            # while refused, then unblocked before it was claimed, is
            # charged like any other. The charge leaves a marker on the run,
            # so a run recovered after its worker died in the model call is
            # not charged again (#862 QA). A run with a checkpoint and no
            # marker was charged before markers existed. A worker that dies
            # between the charge and the marker still charges twice: the
            # error is toward the cap, never past it.
            # The day goes on the marker, so a refund gives back this day's
            # turn even when the run fails after midnight.
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            allowed, _used = self.accounts.claim_daily_turn(
                context["account_id"], self.cfg.chat_turns_per_day, day=day)
            if not allowed:
                # The current message only, as at admission: the model's
                # own "not urgent" must not lead an ordinary limit with 911.
                self._finish(run, {"text": beta.with_911(
                                       beta.daily_limit_text(), user_text),
                                   "checkpoint_id": "daily_limit"},
                             emitted, heartbeat)
                return
            self.hc.append_agent_run_event(
                run_id, self.worker_id, CHARGED_EVENT, {"day": day})
            run["_refund"]["day"] = day

        lead = beta.sample_frame(who) if sample else ""
        labels = SAMPLE_TOOL_LABELS if sample else TOOL_LABELS

        def sms_phone() -> str | None:
            return text_self.sms_phone_for(
                self.cfg, self.accounts, context["account_id"],
                context.get("email") or "")
        # Built per turn: propose_action is offered only with a number to
        # text, and _execute_tool asks again when it is called.
        phone_now = sms_phone()
        offered = tools_for_turn(phone_now)
        if (phone_now is None and not context.get("paused")
                and self.cfg.actions_sms_open_for(context.get("email") or "")):
            # Open to this account, but no phone to text: the model says
            # how to link one instead of promising a text (design 9).
            prompt = f"{prompt}\n\n{text_self.LINK_PHONE_NOTE}"
        if final_checkpoint is not None:
            self._finish(run, final_checkpoint, emitted, heartbeat, lead=lead)
            self._mark(context["account_id"], "first_answer")
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
                tools = offered if round_number < MAX_TOOL_ROUNDS else []
                turn = llm.complete(self.cfg, prompt, history, tools)
                # The provider call may outlive the lease or hard deadline.
                # Never checkpoint or persist a late result after the
                # authoritative heartbeat transaction revoked ownership.
                heartbeat.check()
                self._check_deadline(run)
                round_number += 1
                declined = getattr(turn, "stop_reason", "") == "refusal"
                checkpoint_id = f"round-{round_number}"
                pending_checkpoint = {
                    "checkpoint_id": checkpoint_id,
                    "round": round_number,
                    # A decline's partial words are what it declined:
                    # never kept, shown or sent back to the model (#1017).
                    "text": (llm.REFUSAL_TEXT if declined else turn.text),
                    # "refusal" when the model declined (#1017): _finish
                    # gives a decline the 911 line.
                    "stop_reason": "refusal" if declined else "",
                    # A decline means stop: no tool it asked for runs.
                    "tool_calls": [] if declined else [
                        {"id": call.id, "name": call.name,
                         "arguments": call.arguments}
                        for call in turn.tool_calls],
                    "raw_tool_calls": [] if declined else turn.raw_tool_calls,
                    # Caps the advisor per turn across a recovered run too
                    # (careagents/routing.py advisor_used).
                    "advisor_calls": getattr(turn, "advisor_calls", 0),
                }
                self.hc.append_agent_run_event(
                    run_id, self.worker_id, "agent.checkpoint",
                    pending_checkpoint)
                checkpoints.append({"type": "agent.checkpoint",
                                    "payload": pending_checkpoint})
                # A model round answered: the turn's cost is spent, and a
                # later failure no longer gives the turn back.
                run["_refund"]["answered"] = True
                history.append({
                    "role": "assistant",
                    "content": pending_checkpoint["text"],
                    "tool_calls": pending_checkpoint["tool_calls"],
                    "_openai_tool_calls": pending_checkpoint["raw_tool_calls"],
                    "advisor_calls": pending_checkpoint["advisor_calls"],
                })

            calls = pending_checkpoint.get("tool_calls") or []
            if pending_checkpoint.get("stop_reason") == "refusal":
                calls = []          # a decline means stop, a replayed one too
            if not calls:
                self._finish(run, pending_checkpoint, emitted, heartbeat,
                             lead=lead)
                self._mark(context["account_id"], "first_answer")
                return

            for call in calls:
                heartbeat.check()
                self._check_deadline(run)
                call_id = str(call["id"])
                result_event = tool_results.get(call_id)
                if result_event is None:
                    result_event = self._execute_durable_tool(
                        run, call, emitted, labels, arriving=arriving,
                        sms_phone=sms_phone)
                    tool_results[call_id] = result_event
                history.append({"role": "tool", "tool_call_id": call_id,
                                "content": result_event.get("content") or "{}"})
            # The checkpoint is now complete. A following iteration performs
            # the next model call; the completed tools remain replayable.

    def _execute_durable_tool(self, run: dict, call: dict,
                              emitted: set[tuple[str, str]],
                              labels: dict = TOOL_LABELS,
                              arriving: bool = False,
                              sms_phone=None) -> dict:
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
                 "label": labels.get(tool_name, tool_name)})
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
                    self.hc, tenant, tool_name, arguments, side_events,
                    agent_id=str(run.get("agent_id") or ""),
                    surface=str(run.get("surface") or ""),
                    origin=self.cfg.origin, arriving=arriving,
                    sms_phone=sms_phone)
            except HealthClawError as exc:
                content = agent_tool_error_result(exc)
            envelope = {"content": content, "ui_events": side_events}
            outcome_ref = next((
                str(event.get("action_id"))
                for event in side_events if event.get("action_id")), None)
            self.hc.transition_agent_tool_call(
                run_id, durable["id"], self.worker_id, "completed",
                result=envelope, outcome_ref=outcome_ref)
            if outcome_ref:
                # An action was proposed for the person to approve.
                funnel.safely(funnel.mark_for_agent, self.accounts.session,
                              str(run.get("agent_id") or ""),
                              "action_proposed")

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
                emitted: set[tuple[str, str]], heartbeat: LeaseHeartbeat,
                lead: str = "") -> None:
        """`lead` closes a model's answer on its own line (the sample's
        SAMPLE_FRAME). The checkpoint keeps the model's text alone, so a
        replayed run adds it once, here."""
        heartbeat.check()
        self._check_deadline(run)
        run_id = str(run["id"])
        text = str(checkpoint.get("text") or "").strip()
        said = _said(run)
        if (checkpoint.get("stop_reason") == "refusal"
                or text == llm.REFUSAL_TEXT):
            # The model declined: a refusal like any other, and likeliest
            # after self-harm wording. The signal is the stop reason; the
            # exact sentence is the fallback for checkpoints written before
            # it was kept. Whatever the model wrote before it stopped is
            # dropped: the fixed sentence only, with 911 and 988 first for
            # a crisis, else the 911 line after it.
            text = beta.with_911(llm.REFUSAL_TEXT, said)
        elif not text:
            # An empty answer is a failure, not an answer of "…".
            text = beta.with_911(GENERIC_FAILURE_TEXT, said)
        if lead:
            # The line is in the stored history, so a model may already
            # open with it on a later turn. Say it once. A real answer
            # (no lead) is never touched.
            body = _drop_echo(text, lead) or "…"
            # The model sees the line closing every earlier answer and may
            # close with it too: any copy that is a paragraph of its own
            # goes, so the line is said once (security round 8).
            body = "\n\n".join(
                p for p in body.split("\n\n")
                if p.strip() != lead).strip() or "…"
            # At the end of every answer, never ahead of anything in it:
            # the #1015 live run read "Nothing here is about you." before
            # "call 911", and no word list can say which answers carry
            # 911 guidance (#1017). The web shows it in the banner above
            # instead (beta.under_banner, chat.js withoutFrame).
            text = f"{body}\n\n{lead}"
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


class _IdleBackoff:
    """Per-slot idle poll interval for one worker pool.

    A slot doubles its own interval while the queue hands it nothing, up to
    the cap; a claim by any slot returns every slot to the floor. Slots are
    threads in one process, so that reset is a shared list under a lock, not
    coordination between processes.

    The cap is bounded by latency, not by safety. Presence is committed by
    the claim itself, so an idle slot's interval is also how stale its
    presence row gets, and the cap has to stay well inside
    CARE_RUN_WORKER_STALE_SECONDS.
    """

    def __init__(self, slots: int, floor: float, cap: float) -> None:
        self._floor = floor
        self._cap = cap
        self._lock = threading.Lock()
        self._intervals = [floor] * slots

    def on_claim(self) -> None:
        """Note that a slot took work: every slot returns to the floor."""
        with self._lock:
            self._intervals = [self._floor] * len(self._intervals)

    def on_empty(self, slot: int) -> float:
        """Return this slot's wait, then double it for the next empty poll."""
        with self._lock:
            interval = self._intervals[slot]
            self._intervals[slot] = min(interval * 2, self._cap)
            return interval


def warn_real_records_unset(cfg: Config, accounts: AccountService) -> bool:
    """At boot: warn, never fail, when this worker has CARE_REAL_RECORDS off
    but real-record connections exist, which only the web can have made.
    The worker still refuses each real-record turn whose model host is not
    approved; the warning says the setting should be mirrored here."""
    if cfg.real_records != "off":
        return False
    from careagents.models import Connection
    try:
        with accounts.session() as s:
            real = (s.query(Connection.id)
                    .filter(Connection.kind != "sample").first())
    except Exception:  # noqa: BLE001 - a boot warning never stops a worker
        logger.warning("could not check for real-record connections")
        return False
    if real is None:
        return False
    logger.warning(
        "CARE_REAL_RECORDS is unset on this worker but real-record "
        "connections exist: mirror the web's CARE_REAL_RECORDS here. Model "
        "host %s is %s for real records; unapproved, their turns are "
        "refused.", cfg.model_host,
        "approved" if cfg.model_host_vetted else "NOT approved")
    return True


def run_worker_pool(cfg: Config, stop: threading.Event | None = None) -> None:
    """Run a fixed-size pool; each slot owns its HTTP session and lease id."""
    stop = stop or threading.Event()
    accounts = AccountService(cfg)
    warn_real_records_unset(cfg, accounts)
    # The routing counts (careagents/routing.py) write through this pool
    # rather than opening one of their own.
    routing.use_session_scope(getattr(accounts, "session", None))
    base_id = _worker_base_id()
    backoff = _IdleBackoff(cfg.run_worker_concurrency, cfg.run_poll_seconds,
                           cfg.run_poll_max_seconds)

    def loop(slot: int) -> None:
        hc = HealthClawClient(cfg.healthclaw_base, cfg.mint_secret,
                              public_base=cfg.healthclaw_public_base)
        worker = RunWorker(cfg, hc, accounts, f"{base_id}-{slot}")
        while not stop.is_set():
            try:
                worked = worker.run_once()
            except HealthClawError as exc:
                logger.error("run claim failed: %s", exc)
                worked = False
            if worked:
                backoff.on_claim()
            else:
                # Stays stop.wait, never time.sleep: a capped sleep would be
                # added to every SIGTERM drain and every deploy.
                stop.wait(backoff.on_empty(slot))

    threads = [threading.Thread(target=loop, args=(slot,), daemon=False,
                                name=f"careagents-worker-{slot}")
               for slot in range(cfg.run_worker_concurrency)]
    if cfg.sendblue_enabled:
        # The worker, not the web, owns texting an answer back: the owed
        # answers are rows in the account store, so a web restart loses none.
        from careagents import sendblue, sendblue_surface
        deliverer = sendblue_surface.Deliverer(
            cfg, HealthClawClient(cfg.healthclaw_base, cfg.mint_secret,
                                  public_base=cfg.healthclaw_public_base),
            accounts, sendblue.Client(cfg))
        threads.append(threading.Thread(
            target=deliverer.run, args=(stop,), daemon=False,
            name="careagents-sendblue"))
    if cfg.digest_to:
        # The owner's once-a-day funnel digest (careagents/funnel.py). Off
        # unless CARE_DIGEST_TO is set; a day is sent once across workers.
        threads.append(threading.Thread(
            target=funnel.run_digest, args=(accounts.session, cfg, stop),
            daemon=False, name="careagents-digest"))
    if cfg.checkin_emails:
        # Day-2 and day-7 tester check-ins (careagents/checkins.py). Each is
        # claimed once across workers before it is sent.
        threads.append(threading.Thread(
            target=checkins.run, args=(accounts.session, cfg, stop),
            daemon=False, name="careagents-checkins"))
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
