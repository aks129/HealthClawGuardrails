"""The bot must answer every command it lists, and answer a plain sentence.

Two findings from the physician advisor's dry run before the launch recording,
both of which read as "the product is broken" on camera:

  1. "The conversational BP flow seems not to answer. I sent 'my blood
     pressure this morning was 150 over 94' and got no reply, so I suspect it
     wants different phrasing or something isn't wired on this tenant."

     It was not phrasing. `main()` registered exactly one MessageHandler, on
     `filters.COMMAND`. Nothing was listening for text, so a sentence typed at
     the bot reached no handler and produced silence — the failure mode that
     looks identical to a hung backend.

  2. "/start" advertises twelve commands. Two of them, /summary and
     /curatr_fix, had no handler at all and fell through to "Unknown command.
     Try /start for the command list." — which is the list that had just
     offered them.

The triage this file exercises is r6/smbp/triage.py, which already existed and
is the advisor's own 2025 AHA/ACC spec. Nothing here invents clinical logic;
the bug was that no wire ran from a typed sentence to that module.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token-for-advertised-commands')

sys.path.insert(0, str(Path(__file__).parent.parent / "openclaw"))

import bot  # noqa: E402

BOT_SOURCE = (Path(__file__).parent.parent / "openclaw" / "bot.py").read_text()


def _run(coro):
    return asyncio.run(coro)


def _update(text: str):
    """A minimal Update carrying a text message."""
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text),
        effective_chat=SimpleNamespace(id=4242),
        effective_user=SimpleNamespace(username="advisor", id=99),
        message=SimpleNamespace(text=text),
    )


# --- every advertised command has a handler --------------------------------

def _advertised() -> set[str]:
    return {c.replace("\\", "")
            for c in re.findall(r"'(/[a-z_\\]+) —", BOT_SOURCE)}


def _registered() -> set[str]:
    return {f"/{m}" for m in re.findall(r"CommandHandler\('([a-z_]+)'", BOT_SOURCE)}


def test_the_start_menu_is_not_empty():
    """A menu that lists nothing would satisfy the check below trivially."""
    assert len(_advertised()) >= 8, f"/start lists only {_advertised()}"


def test_every_command_on_the_start_menu_has_a_handler():
    """MUTATION: delete the /summary CommandHandler -> red.

    The advisor films /start first. A command list that answers "Unknown
    command" for its own entries is the worst possible opening frame.
    """
    dead = _advertised() - _registered()
    assert not dead, (
        f"/start advertises commands with no handler: {sorted(dead)}. "
        f"Each one answers 'Unknown command. Try /start for the command "
        f"list.' — which is the list that just offered it.")


# --- a plain sentence gets an answer ---------------------------------------

def test_a_text_handler_is_registered_at_all():
    """MUTATION: drop the TEXT MessageHandler from main() -> red.

    This is the defect itself: the only MessageHandler filtered on COMMAND,
    so typed text reached nothing.
    """
    assert re.search(r"MessageHandler\(\s*filters\.TEXT", BOT_SOURCE), (
        "no MessageHandler for plain text — a sentence typed at the bot "
        "produces silence, which is indistinguishable from a hung backend")


@pytest.mark.parametrize("text,systolic,diastolic", [
    ("my blood pressure this morning was 150 over 94", 150, 94),
    ("BP 150/94", 150, 94),
    ("150 over 94", 150, 94),
    ("my bp was 128/78 today", 128, 78),
    ("blood pressure 182 over 121", 182, 121),
])
def test_a_reading_is_parsed_out_of_ordinary_phrasing(text, systolic, diastolic):
    """The advisor's exact sentence is the first case.

    MUTATION: drop the 'over' alternative from the pattern -> red.
    """
    assert bot._parse_bp(text) == (systolic, diastolic)


@pytest.mark.parametrize("text", [
    "hello",
    "what are my labs?",
    "thanks!",
    "",
    # Not a BP: a date, a ratio, a score. Reading these as a BP would put a
    # triage band on something that is not a measurement.
    "see you on 10/11",
    "I rate it 9 over 10",
])
def test_text_that_is_not_a_reading_is_not_parsed_as_one(text):
    """MUTATION: widen the pattern to bare `\\d+/\\d+` -> red on the date."""
    assert bot._parse_bp(text) is None


def _say(text, triage=None, fail=False):
    """Drive on_text with the server call stubbed; return what was replied."""
    sent = []
    ctx = patch.object(bot, '_smbp_reading', side_effect=RuntimeError("down")) \
        if fail else patch.object(bot, '_smbp_reading', return_value=triage)
    with patch.object(bot, '_reply', side_effect=lambda u, t, a, **k: sent.append(t)), \
         patch.object(bot, '_persist_turn'), ctx:
        _run(bot.on_text(_update(text), None))
    return sent


def test_the_band_comes_from_the_server_not_from_this_container():
    """openclaw/Dockerfile copies only bot.py, so r6.smbp.triage is not
    importable here. Re-deriving the 2025 thresholds in the bot is exactly the
    drift that module exists to prevent, so the reply must be built from what
    the server returned.

    MUTATION: compute the band locally from the numbers -> red.
    """
    sent = _say("bp 150/94", {"band": "at_goal", "emergency": False})
    body = " ".join(sent).lower()
    assert "target range" in body, (
        f"the reply ignored the server's band and decided for itself: {sent}")


def test_a_stage_two_reading_is_answered_with_the_triage_band():
    """The advisor's exact sentence, answered."""
    sent = _say("my blood pressure this morning was 150 over 94",
                {"band": "stage2", "emergency": False})
    assert sent, "the advisor's sentence still produces no reply"
    body = " ".join(sent).lower()
    assert "150" in body and "94" in body, f"the reading is not echoed back: {sent}"
    assert "stage 2" in body, f"150/94 is Stage 2 under the 2025 line: {sent}"


def test_an_unreachable_server_is_reported_not_guessed():
    """The one output worse than an error is an invented reassurance.

    MUTATION: fall back to a locally computed band on failure -> red.
    """
    sent = _say("bp 150/94", fail=True)
    body = " ".join(sent).lower()
    assert sent, "a failed classification produced silence"
    assert "not logged" in body or "could not reach" in body, (
        f"the failure was not reported: {sent}")
    for band_word in ("stage 1", "stage 2", "target range", "crisis"):
        assert band_word not in body, (
            f"a band was stated despite the classifier being unreachable: {sent}")


def test_a_red_flag_symptom_routes_to_emergency_regardless_of_the_number():
    """The one case where being wrong is dangerous, so it is pinned.

    triage.classify evaluates symptoms FIRST and independently: a red-flag
    symptom is 911 even at a normal reading. A handler that checked the number
    first would reassure a symptomatic patient.

    MUTATION: pass symptoms=None from on_text -> red (see the companion
    assertion on _symptoms_in below).
    """
    sent = _say("bp 118/76 and I have chest pain",
                {"band": "at_goal", "emergency": True})
    body = " ".join(sent).lower()
    assert "911" in body, (
        f"a red-flag symptom at a normal reading must route to 911, not "
        f"reassurance: {sent}")


def test_text_that_is_not_a_reading_gets_a_reply_rather_than_silence():
    """Silence is the bug. Anything the bot cannot parse still gets an answer.

    MUTATION: return early without replying when _parse_bp is None -> red.
    """
    sent = _say("what are my labs?")
    assert sent, "unparsed text produces no reply at all — the original defect"


def test_the_bot_does_not_hand_out_clinical_advice():
    """triage.py is 'administrative logic only'; the wire must not add advice.

    The reply may state a band and a next step. It must not tell the patient
    what to take or what they have.

    MUTATION: add 'you should take' copy to the reply -> red.
    """
    sent = _say("bp 150/94", {"band": "stage2", "emergency": False})
    # Scan everything EXCEPT the disclaimer. The disclaimer says "not medical
    # advice or a diagnosis", so a stem match on "diagnos" flags the one line
    # whose whole job is to deny what the guard is looking for.
    body = " ".join(
        line for line in " ".join(sent).splitlines()
        if "not medical advice" not in line.lower()).lower()
    for phrase in ("you should take", "you have hypertension", "diagnos",
                   "prescrib", "mg of", "start taking"):
        assert phrase not in body, (
            f"the reply gives clinical advice ({phrase!r}); this surface is a "
            f"navigator, not a clinician: {sent}")


def test_the_symptoms_are_extracted_and_handed_to_the_classifier():
    """The symptom axis only works if the words reach the server.

    MUTATION: call _smbp_reading with symptoms=[] -> red.
    """
    with patch.object(bot, '_smbp_reading',
                      return_value={"band": "at_goal", "emergency": True}) as call, \
         patch.object(bot, '_reply'), patch.object(bot, '_persist_turn'):
        _run(bot.on_text(_update("bp 118/76 and I have chest pain"), None))

    assert call.call_args is not None, "_smbp_reading was never called"
    assert "chest_pain" in call.call_args.args[2], (
        f"the endorsed symptom never reached the classifier: {call.call_args}")
