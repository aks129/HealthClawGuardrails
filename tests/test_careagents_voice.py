"""Voice PR 2: config, the sample-records gate, the ElevenLabs client and
the daily ceilings (careagents/voice.py).

The HTTP session is a fake, so these prove what we send and how we treat
what comes back, not that ElevenLabs accepts it. The key is a fake too.
Synthetic data only.
"""

from __future__ import annotations

import logging

import pytest
import requests

from careagents import voice
from careagents.config import Config, ConfigError
from tests.test_careagents import _make_account, cfg, svc  # noqa: F401

FAKE_KEY = "fake-eleven-key-0000"
VOICE_ID = "StockVoice01"
SENTINEL = "SENTINEL-echoed-by-provider"


def _cfg(**env):
    base = {"CARE_DATABASE_URL": "sqlite:///:memory:"}
    base.update(env)
    return Config(env=base)


def _on(**env):
    return _cfg(CARE_VOICE="on", ELEVENLABS_API_KEY=FAKE_KEY,
                ELEVENLABS_VOICE_ID=VOICE_ID, **env)


# --- config --------------------------------------------------------------------

def test_voice_is_off_by_default():
    c = _cfg()
    assert c.voice is False
    assert c.voice_real_records is False
    assert c.voice_stt_seconds_per_day == 600       # 10 minutes
    assert c.voice_tts_chars_per_day == 20000


@pytest.mark.parametrize("value,expected", [
    ("", False), ("off", False), ("OFF", False), ("0", False),
    ("false", False), ("no", False),
    ("on", True), ("ON", True), ("true", True), ("1", True), ("yes", True)])
def test_voice_reads_like_the_other_flags(value, expected):
    assert _cfg(CARE_VOICE=value, ELEVENLABS_API_KEY=FAKE_KEY,
                ELEVENLABS_VOICE_ID=VOICE_ID).voice is expected


@pytest.mark.parametrize("name", ["CARE_VOICE", "CARE_VOICE_REAL_RECORDS"])
@pytest.mark.parametrize("value", ["enabled", "onn", "2", "y"])
def test_an_unrecognised_flag_refuses_to_boot(name, value):
    """Not silently off: an operator who typed `enabled` meant something."""
    with pytest.raises(ConfigError, match=name):
        _cfg(**{name: value})


@pytest.mark.parametrize("name", ["CARE_VOICE_STT_SECONDS_PER_DAY",
                                  "CARE_VOICE_TTS_CHARS_PER_DAY",
                                  "CARE_CHAT_TURNS_PER_DAY"])
@pytest.mark.parametrize("value", ["ten", "1.5", "600s"])
def test_a_non_integer_cap_refuses_to_boot_naming_it(name, value):
    with pytest.raises(ConfigError, match=name):
        _cfg(**{name: value})


def test_voice_on_without_a_key_refuses_to_boot():
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        _cfg(CARE_VOICE="on", ELEVENLABS_VOICE_ID=VOICE_ID)


@pytest.mark.parametrize("voice_id", ["", "../v1/voices", "a b", "x" * 65])
def test_voice_on_needs_a_plain_voice_id(voice_id):
    with pytest.raises(ConfigError, match="ELEVENLABS_VOICE_ID") as e:
        _cfg(CARE_VOICE="on", ELEVENLABS_API_KEY=FAKE_KEY,
             ELEVENLABS_VOICE_ID=voice_id)
    assert FAKE_KEY not in str(e.value)


@pytest.mark.parametrize("key", ["   ", "\t", "\n"])
def test_a_blank_key_refuses_to_boot(key):
    """#974 F2."""
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        _cfg(CARE_VOICE="on", ELEVENLABS_API_KEY=key,
             ELEVENLABS_VOICE_ID=VOICE_ID)


def test_a_pasted_key_is_stripped():
    """#974 F4: the trailing newline an env UI adds is not part of it."""
    c = _cfg(CARE_VOICE="on", ELEVENLABS_API_KEY=f" {FAKE_KEY}\n",
             ELEVENLABS_VOICE_ID=f"{VOICE_ID}\n")
    assert (c.elevenlabs_api_key, c.elevenlabs_voice_id) == (FAKE_KEY,
                                                             VOICE_ID)


@pytest.mark.parametrize("key", [FAKE_KEY + "\r\nX-Injected: 1",
                                 "fake eleven key", "fake\tkey"])
def test_a_key_with_whitespace_inside_refuses_to_boot_without_naming_it(key):
    """#974 F4: the error names the variable, never the value."""
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY") as e:
        _cfg(CARE_VOICE="on", ELEVENLABS_API_KEY=key,
             ELEVENLABS_VOICE_ID=VOICE_ID)
    assert "fake" not in str(e.value)


def test_voice_on_with_key_and_voice_boots():
    c = _on()
    assert c.voice is True
    assert c.elevenlabs_voice_id == VOICE_ID


def test_caps_are_configurable_and_never_negative():
    c = _cfg(CARE_VOICE_STT_SECONDS_PER_DAY="60",
             CARE_VOICE_TTS_CHARS_PER_DAY="500")
    assert (c.voice_stt_seconds_per_day, c.voice_tts_chars_per_day) == (
        60, 500)
    with pytest.raises(ConfigError):
        _cfg(CARE_VOICE_STT_SECONDS_PER_DAY="-1")
    with pytest.raises(ConfigError):
        _cfg(CARE_VOICE_TTS_CHARS_PER_DAY="-1")


@pytest.mark.parametrize("value,expected", [
    ("", False), ("off", False), ("on", True), ("1", True), ("TRUE", True)])
def test_voice_real_records_flag(value, expected):
    assert _cfg(CARE_VOICE_REAL_RECORDS=value).voice_real_records is expected


# --- the sample-records gate ---------------------------------------------------

@pytest.mark.parametrize("ctx", [
    {"connection": {"kind": "fasten"}},
    {"connection": {"kind": "smart"}},
    {"connection": {"kind": "a-kind-added-later"}},
    {"connection": {}},
    {"connection": None},
    {},
])
def test_anything_but_the_sample_is_blocked(ctx):
    """Mutation M1: delete the kind check and this goes red."""
    assert voice.voice_blocked(_cfg(), ctx) is True


def test_the_sample_is_allowed():
    assert voice.voice_blocked(
        _cfg(), {"connection": {"kind": "sample"}}) is False


def test_real_records_pass_only_with_the_flag():
    """Mutation M2: delete the flag check and this goes red."""
    real = {"connection": {"kind": "fasten"}}
    assert voice.voice_blocked(_cfg(), real) is True
    assert voice.voice_blocked(
        _cfg(CARE_VOICE_REAL_RECORDS="on"), real) is False


# --- the client ----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, chunks=(), raw=None,
                 broken_after=None):
        self.status_code = status
        self._body = body
        self._raw = raw
        self._chunks = list(chunks)
        self._broken_after = broken_after
        self.closed = False
        self.text = raw if raw is not None else repr(body)

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body

    def iter_content(self, chunk_size=1):
        for i, c in enumerate(self._chunks):
            if self._broken_after is not None and i == self._broken_after:
                raise requests.ConnectionError("broke " + SENTINEL)
            yield c

    def close(self):
        self.closed = True


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(body={"text": "hello"})
        self.error = error
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.error:
            raise self.error
        return self.response


def _client(http, **env):
    return voice.Client(_on(**env), http=http)


def test_a_client_without_configuration_refuses():
    with pytest.raises(voice.VoiceError):
        voice.Client(_cfg(), http=FakeHTTP())


def test_transcribe_sends_one_multipart_scribe_request():
    http = FakeHTTP(FakeResponse(body={
        "text": "  What was my last A1c?  ", "audio_duration_secs": 4.2}))
    t = _client(http).transcribe(b"\x1a\x45audio", "audio/webm")
    assert t == voice.Transcript("What was my last A1c?", 4.2)
    (url, kw), = http.calls
    assert url == "https://api.elevenlabs.io/v1/speech-to-text"
    assert kw["headers"] == {"xi-api-key": FAKE_KEY}
    assert kw["data"] == {"model_id": "scribe_v2"}
    assert kw["allow_redirects"] is False
    assert kw["files"]["file"][1:] == (b"\x1a\x45audio", "audio/webm")
    assert kw["timeout"] == voice.TIMEOUT
    # Not Enterprise: zero retention is not asked for (it would be refused).
    assert "enable_logging" not in kw["params"]


def test_real_records_mode_asks_for_zero_retention_on_both_calls():
    http = FakeHTTP()
    c = _client(http, CARE_VOICE_REAL_RECORDS="on")
    c.transcribe(b"a", "audio/webm")
    http.response = FakeResponse(chunks=[b"mp3"])
    c.speak("Hello.")
    assert [kw["params"]["enable_logging"] for _, kw in http.calls] == [
        "false", "false"]


@pytest.mark.parametrize("seconds", [None, "4", -1, True])
def test_a_missing_or_odd_duration_is_none(seconds):
    http = FakeHTTP(FakeResponse(body={"text": "hi",
                                       "audio_duration_secs": seconds}))
    assert _client(http).transcribe(b"a", "audio/webm").seconds is None


@pytest.mark.parametrize("audio", [b"", b"x" * (voice.MAX_UPLOAD_BYTES + 1)],
                         ids=["empty", "oversize"])
def test_empty_or_oversize_audio_never_reaches_elevenlabs(audio):
    http = FakeHTTP()
    with pytest.raises(ValueError):
        _client(http).transcribe(audio, "audio/webm")
    assert http.calls == []


@pytest.mark.parametrize("response", [
    FakeResponse(status=200, raw="<html>" + SENTINEL),
    FakeResponse(status=200, body={"detail": SENTINEL}),
    FakeResponse(status=200, body=[SENTINEL]),
])
def test_an_unreadable_transcript_is_an_error_not_a_fake(response):
    with pytest.raises(voice.VoiceError):
        _client(FakeHTTP(response)).transcribe(b"a", "audio/webm")


def test_speak_returns_the_whole_reply_from_the_stock_voice():
    resp = FakeResponse(chunks=[b"ID3", b"", b"frames"])
    http = FakeHTTP(resp)
    out = _client(http).speak("Your A1c was 5.6 in March.")
    assert out == b"ID3frames"
    assert resp.closed
    (url, kw), = http.calls
    assert url == (
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream")
    assert kw["json"] == {"text": "Your A1c was 5.6 in March.",
                          "model_id": "eleven_multilingual_v2"}
    assert kw["params"] == {"output_format": "mp3_44100_128"}
    assert kw["headers"] == {"xi-api-key": FAKE_KEY}
    assert kw["stream"] is True
    assert kw["allow_redirects"] is False
    assert kw["timeout"] == voice.TIMEOUT


def test_a_refused_speak_is_an_exception_at_the_call():
    http = FakeHTTP(FakeResponse(status=401))
    with pytest.raises(voice.VoiceError) as e:
        _client(http).speak("Hello.")
    assert e.value.status == 401
    assert http.response.closed


@pytest.mark.parametrize("text", ["", "   ", "x" * (voice.TTS_MAX_CHARS + 1)],
                         ids=["empty", "blank", "too-long"])
def test_speak_never_truncates_and_never_sends_nothing(text):
    http = FakeHTTP()
    with pytest.raises(ValueError):
        _client(http).speak(text)
    assert http.calls == []


def test_a_reply_at_the_limit_is_sent_whole():
    http = FakeHTTP(FakeResponse(chunks=[b"a"]))
    text = "x" * (voice.TTS_MAX_CHARS - 9) + " call 988"
    assert len(text) == voice.TTS_MAX_CHARS
    assert _client(http).speak(text) == b"a"
    assert http.calls[0][1]["json"]["text"].endswith("call 988")


def test_a_stream_that_breaks_gives_no_audio_at_all():
    """#974 F1: the head of a reply is never handed over; its tail is
    where "call 988" sits."""
    resp = FakeResponse(chunks=[b"a", b"b"], broken_after=1)
    with pytest.raises(voice.VoiceError):
        _client(FakeHTTP(resp)).speak("Hello. Call 988.")
    assert resp.closed


@pytest.mark.parametrize("chunks", [[], [b""]])
def test_an_empty_audio_answer_is_an_error(chunks):
    with pytest.raises(voice.VoiceError):
        _client(FakeHTTP(FakeResponse(chunks=chunks))).speak("Hello.")


def test_audio_far_past_any_reply_is_refused(monkeypatch):
    monkeypatch.setattr(voice, "MAX_AUDIO_BYTES", 4)
    resp = FakeResponse(chunks=[b"abc", b"def"])
    with pytest.raises(voice.VoiceError):
        _client(FakeHTTP(resp)).speak("Hello.")
    assert resp.closed


def test_a_client_refuses_while_voice_is_off():
    """#974 F3: the flag binds the client, not only the boot check."""
    c = _cfg(ELEVENLABS_API_KEY=FAKE_KEY, ELEVENLABS_VOICE_ID=VOICE_ID)
    assert c.voice is False
    with pytest.raises(voice.VoiceError):
        voice.Client(c, http=FakeHTTP())


# --- nothing spoken or heard is logged ----------------------------------------

SPOKEN = "Synthetic: my metformin dose is 500 mg"


@pytest.mark.parametrize("call", ["transcribe", "speak"])
@pytest.mark.parametrize("http", [
    lambda: FakeHTTP(FakeResponse(status=500, body={"detail": SENTINEL})),
    lambda: FakeHTTP(FakeResponse(status=422, raw=SENTINEL)),
    lambda: FakeHTTP(error=requests.ConnectionError(SENTINEL)),
    lambda: FakeHTTP(error=requests.Timeout(SENTINEL)),
    # #974 F4: http.client's own ValueError quotes a bad header, the key.
    lambda: FakeHTTP(error=ValueError(f"Invalid header value {FAKE_KEY}")),
    lambda: FakeHTTP(error=TypeError(SENTINEL)),
])
def test_a_failure_carries_no_content_body_or_key(caplog, call, http):
    caplog.set_level(logging.DEBUG)
    c = _client(http())
    with pytest.raises(voice.VoiceError) as e:
        if call == "transcribe":
            c.transcribe(SPOKEN.encode(), "audio/webm")
        else:
            c.speak(SPOKEN)
    for leaked in (SENTINEL, FAKE_KEY, SPOKEN, "metformin"):
        assert leaked not in caplog.text
        assert leaked not in str(e.value)
    # A traceback must not chain the transport error, whose text can carry
    # what was sent.
    assert e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__


def test_a_success_logs_nothing_heard_or_spoken(caplog):
    caplog.set_level(logging.DEBUG)
    http = FakeHTTP(FakeResponse(body={"text": SPOKEN}))
    c = _client(http)
    assert c.transcribe(b"a", "audio/webm").text == SPOKEN
    http.response = FakeResponse(chunks=[b"mp3"])
    c.speak(SPOKEN)
    assert "metformin" not in caplog.text
    assert FAKE_KEY not in caplog.text


def test_a_broken_stream_logs_no_content(caplog):
    caplog.set_level(logging.DEBUG)
    resp = FakeResponse(chunks=[b"a", b"b"], broken_after=1)
    with pytest.raises(voice.VoiceError):
        _client(FakeHTTP(resp)).speak(SPOKEN)
    assert SENTINEL not in caplog.text
    assert "metformin" not in caplog.text


# --- daily ceilings ------------------------------------------------------------

def test_charge_counts_and_then_refuses(svc, monkeypatch):  # noqa: F811
    acct = _make_account(svc, monkeypatch, "voicecap@example.com")
    assert voice.charge(svc, acct.id, "stt_seconds", 30, cap=60) == (True, 30)
    assert voice.charge(svc, acct.id, "stt_seconds", 30, cap=60) == (True, 60)
    assert voice.charge(svc, acct.id, "stt_seconds", 1, cap=60) == (False, 60)
    assert voice.used(svc, acct.id) == {"stt_seconds": 60, "tts_chars": 0}


def test_a_charge_that_would_cross_the_cap_charges_nothing(
        svc, monkeypatch):  # noqa: F811
    """All or nothing: a reply too long for what is left is not spoken in
    part, and the attempt costs nothing."""
    acct = _make_account(svc, monkeypatch, "voiceall@example.com")
    assert voice.charge(svc, acct.id, "tts_chars", 15000, cap=20000)[0]
    assert voice.charge(svc, acct.id, "tts_chars", 6000, cap=20000) == (
        False, 15000)
    assert voice.charge(svc, acct.id, "tts_chars", 5000, cap=20000) == (
        True, 20000)


def test_a_first_charge_over_the_cap_makes_no_row(svc, monkeypatch):  # noqa: F811
    acct = _make_account(svc, monkeypatch, "voicebig@example.com")
    assert voice.charge(svc, acct.id, "tts_chars", 20001, cap=20000) == (
        False, 0)
    assert voice.charge(svc, acct.id, "stt_seconds", 1, cap=0) == (False, 0)
    assert voice.used(svc, acct.id) == {"stt_seconds": 0, "tts_chars": 0}


def test_the_two_ceilings_are_separate_and_per_account(
        svc, monkeypatch):  # noqa: F811
    a = _make_account(svc, monkeypatch, "voicea@example.com")
    b = _make_account(svc, monkeypatch, "voiceb@example.com")
    assert voice.charge(svc, a.id, "stt_seconds", 600, cap=600)[0]
    assert voice.charge(svc, a.id, "stt_seconds", 1, cap=600)[0] is False
    assert voice.charge(svc, a.id, "tts_chars", 100, cap=20000) == (True, 100)
    assert voice.charge(svc, b.id, "stt_seconds", 30, cap=600) == (True, 30)


@pytest.mark.parametrize("metric,amount", [
    ("audio", 1), ("stt_seconds", 0), ("tts_chars", -5),
    ("tts_chars", 1.5), ("tts_chars", True)])
def test_charge_refuses_bad_input(svc, metric, amount):  # noqa: F811
    with pytest.raises(ValueError):
        voice.charge(svc, "acct_x", metric, amount, cap=100)


def test_the_usage_table_holds_counts_only():
    from careagents.models import VoiceUsageDay
    assert {c.name for c in VoiceUsageDay.__table__.columns} == {
        "id", "account_id", "day", "stt_seconds", "tts_chars"}
