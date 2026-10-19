"""QA pins for voice PR 2 (#974): gaps the mutation run found.

M13: `_spent` without its account filter survived the suite, so one
account's spend could be reported as another's. M18: the cap day in local
time instead of UTC survived, so the day could roll at the wrong hour.
Synthetic data only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from careagents import voice
from tests.test_careagents import _make_account, cfg, svc  # noqa: F401


def test_spend_is_reported_per_account(svc, monkeypatch):  # noqa: F811
    a = _make_account(svc, monkeypatch, "voiceqa-a@example.com")
    b = _make_account(svc, monkeypatch, "voiceqa-b@example.com")
    assert voice.charge(svc, a.id, "tts_chars", 5000, cap=20000)[0]
    assert voice.charge(svc, b.id, "tts_chars", 100, cap=20000) == (True, 100)
    # Second charge takes the UPDATE path, which reports via _spent.
    assert voice.charge(svc, b.id, "tts_chars", 100, cap=20000) == (True, 200)
    assert voice.used(svc, b.id) == {"stt_seconds": 0, "tts_chars": 200}
    assert voice.used(svc, a.id) == {"stt_seconds": 0, "tts_chars": 5000}


def test_the_cap_day_is_the_utc_day(monkeypatch):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 23:30 local on the 16th is 03:30 UTC on the 17th.
            if tz is None:
                return datetime(2026, 10, 16, 23, 30)
            return datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc
                            ).astimezone(tz)

    monkeypatch.setattr(voice, "datetime", _Clock)
    assert voice._today() == "2026-10-17"
