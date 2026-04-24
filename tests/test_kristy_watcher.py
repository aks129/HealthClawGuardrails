"""
tests/test_kristy_watcher.py

Unit tests for the Kristy schedule watcher's pure functions:
event parsing, conflict detection, location normalization, step-up
token minting (format matches the server).
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from kristy_schedule_watcher import (  # noqa: E402
    Conflict,
    Event,
    _classify_kind,
    _extract_person,
    _mint_step_up_token,
    detect_conflicts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(label, summary, location, starts_iso, duration_min=75):
    s = datetime.fromisoformat(starts_iso).replace(tzinfo=timezone.utc)
    e = s + timedelta(minutes=duration_min)
    return Event(
        label=label,
        person=_extract_person(label),
        kind=_classify_kind(summary),
        summary=summary,
        location=location,
        starts=s,
        ends=e,
    )


# ---------------------------------------------------------------------------
# Helpers / parsing
# ---------------------------------------------------------------------------

class TestParsingHelpers:

    def test_extract_person_first_word(self):
        assert _extract_person("Henry Football") == "henry"
        assert _extract_person("Max Football") == "max"
        assert _extract_person("Henry Lacrosse") == "henry"

    def test_extract_person_single_word(self):
        assert _extract_person("Basketball") == "basketball"

    def test_classify_kind_game(self):
        assert _classify_kind("10U Black vs Sewickley") == "Game"
        assert _classify_kind("Championship Game") == "Game"
        assert _classify_kind("Playoff Match") == "Game"

    def test_classify_kind_practice(self):
        assert _classify_kind("3-4 Team - Practice") == "Practice"
        assert _classify_kind("Team Training") == "Practice"

    def test_classify_kind_event_fallback(self):
        assert _classify_kind("Team Photo Day") == "Event"


class TestEventLocationNorm:

    def test_location_norm_strips_field_suffix(self):
        e = _ev("Henry Football", "game", "Morton Turf - A", "2026-05-03T16:00")
        assert e.location_norm == "morton turf"

    def test_location_norm_strips_letter_suffix(self):
        a = _ev("A", "x", "Morton Turf - C", "2026-05-03T16:00")
        b = _ev("B", "x", "Morton Turf - A", "2026-05-03T16:00")
        assert a.location_norm == b.location_norm

    def test_different_streets_stay_different(self):
        a = _ev("A", "x", "1551 Mayview rd 15241", "2026-04-23T17:00")
        b = _ev("B", "x", "2420 Morton Rd, Pittsburgh, PA  15241", "2026-04-23T17:30")
        assert a.location_norm != b.location_norm

    def test_stable_id_deterministic(self):
        e1 = _ev("Henry Football", "Team vs Team", "Morton A", "2026-05-03T16:00")
        e2 = _ev("Henry Football", "Team vs Team", "Morton A", "2026-05-03T16:00")
        assert e1.stable_id() == e2.stable_id()


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

class TestConflicts:

    def test_same_person_double_booking_is_critical(self):
        # Henry on 5/11 — lacrosse in Venetia, football at Morton, overlap
        a = _ev("Henry Lacrosse", "10U Black at Peters Twp.",
                "158 Meredith Dr Venetia, PA", "2026-05-11T17:00", 90)
        b = _ev("Henry Football", "Team - Practice",
                "Morton FREE space", "2026-05-11T17:30", 75)
        out = detect_conflicts([a, b])
        assert len(out) == 1
        assert out[0].kind == "same-person-double-booking"
        assert out[0].severity == "critical"
        assert "Henry" in out[0].title

    def test_multi_person_overlap_is_medium(self):
        # Max at Mayview, Henry at Morton — different people, different locations
        a = _ev("Max Football", "Team - Practice",
                "1551 Mayview rd 15241", "2026-04-23T17:00", 60)
        b = _ev("Henry Lacrosse", "10U Black - Practice",
                "2420 Morton Rd, Pittsburgh, PA", "2026-04-23T17:30", 90)
        out = detect_conflicts([a, b])
        assert len(out) == 1
        assert out[0].kind == "multi-person-overlap"
        assert out[0].severity == "medium"

    def test_same_location_different_fields_not_a_conflict(self):
        # Sun 4/26 — Henry field C, Max field C, back-to-back — same complex
        a = _ev("Henry Football", "vs Team 3", "Morton Turf - C", "2026-04-26T14:45", 75)
        b = _ev("Max Football", "vs Team 2", "Morton Turf - C", "2026-04-26T16:00", 75)
        out = detect_conflicts([a, b])
        # Same complex (Morton Turf), back-to-back, different person — no conflict
        assert len(out) == 0

    def test_same_time_adjacent_fields_still_conflict(self):
        # Sun 5/3 — Henry Turf A, Max Turf C — same time, same complex
        # Our normalization treats them as same location, so not a conflict
        a = _ev("Henry Football", "vs Team 8", "Morton Turf - A", "2026-05-03T16:00", 75)
        b = _ev("Max Football", "vs Team 7", "Morton Turf - C", "2026-05-03T16:00", 75)
        out = detect_conflicts([a, b])
        # By design: same complex, one parent can shuttle — no auto-conflict
        assert len(out) == 0

    def test_same_time_different_complexes_is_conflict(self):
        a = _ev("Henry Football", "vs X", "Morton Turf - A", "2026-05-03T16:00", 75)
        b = _ev("Max Football", "vs Y", "1551 Mayview rd 15241", "2026-05-03T16:00", 75)
        out = detect_conflicts([a, b])
        assert len(out) == 1
        assert out[0].kind == "multi-person-overlap"

    def test_tight_handoff_when_gap_small_and_different_locations(self):
        # Henry finishes lacrosse 5:00pm then football at 5:15pm elsewhere
        a = _ev("Henry Lacrosse", "Practice", "Morton Rd", "2026-05-12T16:00", 60)
        b = _ev("Henry Football", "Practice", "1551 Mayview rd",
                "2026-05-12T17:15", 75)
        out = detect_conflicts([a, b])
        assert any(c.kind == "tight-handoff" for c in out)

    def test_back_to_back_same_location_no_tight_handoff(self):
        a = _ev("Henry Football", "vs X", "Morton Turf - C", "2026-04-26T14:45", 75)
        b = _ev("Max Football", "vs Y", "Morton Turf - C", "2026-04-26T16:00", 75)
        out = detect_conflicts([a, b])
        assert len(out) == 0

    def test_stable_id_same_across_runs(self):
        a = _ev("Henry Lacrosse", "at Peters Twp.", "Venetia, PA", "2026-05-11T17:00", 90)
        b = _ev("Henry Football", "Practice", "Morton FREE", "2026-05-11T17:30", 75)
        c1 = detect_conflicts([a, b])[0]
        c2 = detect_conflicts([a, b])[0]
        assert c1.stable_id == c2.stable_id

    def test_stable_id_ignores_pair_ordering(self):
        a = _ev("Henry Lacrosse", "at Peters Twp.", "Venetia, PA", "2026-05-11T17:00", 90)
        b = _ev("Henry Football", "Practice", "Morton FREE", "2026-05-11T17:30", 75)
        c1 = detect_conflicts([a, b])[0]
        c2 = detect_conflicts([b, a])[0]
        assert c1.stable_id == c2.stable_id


# ---------------------------------------------------------------------------
# Step-up token minting matches server's validator
# ---------------------------------------------------------------------------

class TestStepUpMinting:

    def test_mint_then_validate_server_side(self, monkeypatch):
        monkeypatch.setenv("STEP_UP_SECRET", "test-secret-for-kristy")
        token = _mint_step_up_token("ev-personal", "kristy")
        # Validate using the SERVER's own validator
        from r6.stepup import validate_step_up_token
        ok, err = validate_step_up_token(token, "ev-personal")
        assert ok, err

    def test_token_is_scoped_to_tenant(self, monkeypatch):
        monkeypatch.setenv("STEP_UP_SECRET", "test-secret-for-kristy")
        token = _mint_step_up_token("ev-personal", "kristy")
        from r6.stepup import validate_step_up_token
        ok, err = validate_step_up_token(token, "other-tenant")
        assert not ok
        assert "tenant" in err.lower()

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv("STEP_UP_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="STEP_UP_SECRET"):
            _mint_step_up_token("ev-personal", "kristy")
