"""Voice: ElevenLabs as our ears and mouth, and nothing more.

Speech-to-text turns a push-to-talk clip into text the person reviews
before sending it through /api/chat; text-to-speech reads one of our
agent's replies back. The model, routing, the crisis check, caps, audit and
the transcript all stay where they are. ElevenLabs never runs the turn.

Three pieces, each small:

- `voice_blocked`, the sample-records gate, a copy of
  sendblue_surface.real_records_blocked. ElevenLabs keeps request history
  unless an Enterprise account turns on zero retention, so a real record
  never goes to it until CARE_VOICE_REAL_RECORDS says the BAA exists.
- `Client`, the two REST calls over `requests`, each with a timeout.
- `charge` / `used`, the durable per-account daily ceilings.

Nothing here logs, or puts in an error, audio, transcript text, spoken
text, ElevenLabs' response body (it can echo either) or the key. Logs carry
the call name and the HTTP status.

API (checked 2026-10-16):
https://elevenlabs.io/docs/api-reference/speech-to-text/convert
https://elevenlabs.io/docs/api-reference/text-to-speech/stream
https://elevenlabs.io/docs/overview/models
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from careagents.models import VoiceUsageDay

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
STT_MODEL = "scribe_v2"
#: ElevenLabs' default TTS model; 10,000 characters per request.
TTS_MODEL = "eleven_multilingual_v2"
TTS_MAX_CHARS = 10_000
TTS_OUTPUT_FORMAT = "mp3_44100_128"
#: Push-to-talk ceilings. The endpoint refuses a bigger upload before it
#: reaches here; the client refuses it again, before any spend.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_CLIP_SECONDS = 30
#: (connect, read) seconds. For the stream, read is the gap between chunks.
TIMEOUT = (5, 30)
STREAM_CHUNK_BYTES = 8192
#: speak() holds the whole reply before handing it over. 10,000 characters
#: is minutes of mp3, a few MB; anything far past that is not our reply.
MAX_AUDIO_BYTES = 16 * 1024 * 1024

METRICS = ("stt_seconds", "tts_chars")


class VoiceError(RuntimeError):
    """A speech call failed. `status` is ElevenLabs' HTTP status, 0 for no
    answer. The message names the call, never its content."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def voice_blocked(cfg, ctx: dict) -> bool:
    """Whether this agent must not use voice.

    ElevenLabs carries real records only once its BAA and zero retention
    are in place, and CARE_VOICE_REAL_RECORDS says so. Anything but the
    sample connection counts as real, so a new connection kind is held back
    too.
    """
    if cfg.voice_real_records:
        return False
    return (ctx.get("connection") or {}).get("kind") != "sample"


@dataclass
class Transcript:
    text: str
    seconds: float | None      # ElevenLabs' audio_duration_secs, if given


class Client:
    """ElevenLabs speech-to-text and text-to-speech. Tests swap `http`."""

    def __init__(self, cfg, http=None):
        # Fail loud: there is no fake transcript to fall back to. The flag
        # binds the client too, not only the boot check (#974 F3).
        if not (cfg.voice and cfg.elevenlabs_api_key
                and cfg.elevenlabs_voice_id):
            raise VoiceError("voice is not configured")
        self.cfg = cfg
        self.http = http or requests.Session()

    def _headers(self) -> dict:
        # requests strips only Authorization on a cross-host redirect, so
        # both calls refuse to follow one: a 3xx is a failure (#974 F5).
        return {"xi-api-key": self.cfg.elevenlabs_api_key}

    def _params(self) -> dict:
        # Zero retention is Enterprise-only and refused on other plans, so
        # it is asked for exactly when real records are allowed, which is
        # only once that plan exists.
        return ({"enable_logging": "false"}
                if self.cfg.voice_real_records else {})

    def _failed(self, call: str, status: int) -> VoiceError:
        logger.warning("elevenlabs %s failed: HTTP %s", call,
                       status or "no answer")
        return VoiceError(f"elevenlabs {call} failed", status)

    def transcribe(self, audio: bytes, content_type: str) -> Transcript:
        """One clip to text. The text goes back to the person to review;
        nothing here keeps it."""
        if not audio:
            raise ValueError("empty audio")
        if len(audio) > MAX_UPLOAD_BYTES:
            raise ValueError("audio over the upload limit")
        try:
            r = self.http.post(
                f"{API_BASE}/v1/speech-to-text",
                headers=self._headers(), params=self._params(),
                data={"model_id": STT_MODEL},
                files={"file": ("clip", audio, content_type)},
                timeout=TIMEOUT, allow_redirects=False)
        except Exception as exc:
            # Any exception, not only RequestException: http.client raises
            # a bare ValueError quoting a bad header value, the key (#974 F4).
            logger.warning("elevenlabs speech-to-text failed: %s",
                           type(exc).__name__)
            raise VoiceError("elevenlabs speech-to-text failed") from None
        if r.status_code != 200:
            raise self._failed("speech-to-text", r.status_code)
        try:
            body = r.json()
        except ValueError:
            body = None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise self._failed("speech-to-text", r.status_code)
        seconds = body.get("audio_duration_secs")
        if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                or seconds < 0):
            seconds = None
        return Transcript(text.strip(), seconds)

    def speak(self, text: str) -> bytes:
        """The whole of `text` as mp3, or an error: never a part of it, so a
        reply's last line ("call 988") cannot be cut from the audio.

        The stream is read to its end before anything is returned (#974
        F1): a connection that drops mid-reply is an error, not the head of
        the reply.
        """
        if not text or not text.strip():
            raise ValueError("nothing to speak")
        if len(text) > TTS_MAX_CHARS:
            raise ValueError("text over the per-request limit")
        voice = self.cfg.elevenlabs_voice_id
        try:
            r = self.http.post(
                f"{API_BASE}/v1/text-to-speech/{voice}/stream",
                headers=self._headers(),
                params={**self._params(), "output_format": TTS_OUTPUT_FORMAT},
                json={"text": text, "model_id": TTS_MODEL},
                stream=True, timeout=TIMEOUT, allow_redirects=False)
        except Exception as exc:            # see transcribe: #974 F4
            logger.warning("elevenlabs text-to-speech failed: %s",
                           type(exc).__name__)
            raise VoiceError("elevenlabs text-to-speech failed") from None
        try:
            if r.status_code != 200:
                raise self._failed("text-to-speech", r.status_code)
            audio = bytearray()
            for chunk in r.iter_content(chunk_size=STREAM_CHUNK_BYTES):
                audio += chunk
                if len(audio) > MAX_AUDIO_BYTES:
                    raise self._failed("text-to-speech", r.status_code)
        except VoiceError:
            raise
        except Exception as exc:
            logger.warning("elevenlabs text-to-speech stream broke: %s",
                           type(exc).__name__)
            raise VoiceError("elevenlabs text-to-speech failed") from None
        finally:
            r.close()
        if not audio:
            raise self._failed("text-to-speech", r.status_code)
        return bytes(audio)


# --- daily ceilings ------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def charge(svc, account_id: str, metric: str, amount: int,
           cap: int) -> tuple[bool, int]:
    """Charge `amount` of `metric` against today's cap, all or nothing.
    Returns (allowed, used).

    The same shape as AccountService.claim_daily_turn: one conditional
    UPDATE both checks and charges, so two racing requests cannot pass on
    one count; with no row yet, insert one, and a racing insert (the unique
    account and day) becomes a retry of the UPDATE. A charge that would
    cross the cap changes nothing, so a reply is spoken whole or not at all.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown voice metric {metric!r}")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("amount must be a positive integer")
    day = _today()
    col = getattr(VoiceUsageDay, metric)
    spent = func.coalesce(col, 0)
    stmt = (update(VoiceUsageDay)
            .where(VoiceUsageDay.account_id == account_id,
                   VoiceUsageDay.day == day, spent + amount <= cap)
            .values({metric: spent + amount})
            .execution_options(synchronize_session=False))
    for _ in range(4):
        with svc.session() as s:
            if s.execute(stmt).rowcount:
                return True, _spent(s, account_id, day, metric)
            if s.query(VoiceUsageDay.id).filter_by(
                    account_id=account_id, day=day).first():
                spent_now = _spent(s, account_id, day, metric)
                if spent_now + amount > cap:
                    return False, spent_now
                # Under the cap: a peer inserted today's row after our
                # UPDATE looked and found none. Charge it.
                continue
        if amount > cap:
            return False, 0
        try:
            with svc.session() as s:
                s.add(VoiceUsageDay(account_id=account_id, day=day,
                                    **{metric: amount}))
            return True, amount
        except IntegrityError:
            continue            # a peer made today's row: charge it instead
    raise RuntimeError("could not charge today's voice use")


def _spent(s, account_id: str, day: str, metric: str) -> int:
    col = getattr(VoiceUsageDay, metric)
    return int(s.query(func.coalesce(func.sum(col), 0))
               .filter(VoiceUsageDay.account_id == account_id,
                       VoiceUsageDay.day == day).scalar())


def used(svc, account_id: str) -> dict:
    """Today's spend for this account, read only: {metric: int}."""
    day = _today()
    with svc.session() as s:
        return {m: _spent(s, account_id, day, m) for m in METRICS}
