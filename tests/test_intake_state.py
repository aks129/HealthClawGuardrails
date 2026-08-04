"""Zero-from-empty and zero-from-not-yet-arrived are the same number (#336).

A patient completed a Fasten/MEDENT connect, opened their agent, and was told
"I found 0 conditions, 0 medications, and 0 lab results in your records" while
the import was still running — the connect page having just promised records
"over the next 5-45 minutes". The count was accurate and the sentence was
false: it reads as *you have no conditions*.

These tests pin the rule the fix turns on: a count is only ever speakable once
something is known to have arrived. Everything else is a statement about the
import, not about the person.
"""

from careagents.intake_state import (
    ARRIVAL_WINDOW_SECONDS,
    ARRIVING,
    NO_CONNECTION,
    OVERDUE,
    READY,
    classify,
)

NOW = 1_754_300_000.0
SOME = {"conditions": 52, "medications": 9, "labs": 186}
ZERO = {"conditions": 0, "medications": 0, "labs": 0}


def test_zero_while_import_in_flight_is_not_a_finding():
    """THE bug. Two minutes after connecting, zero means 'not yet'."""
    st = classify(totals=ZERO, connection_status="pending",
                  connected_at=NOW - 120, now=NOW, provider="MEDENT")
    assert st.state == ARRIVING
    assert st.counts is None, "a count in flight is a claim we cannot make"
    assert st.provider == "MEDENT"


def test_zero_after_a_completed_import_is_a_finding():
    """State 3 of the issue: the original sentence is correct here, and only
    here. An empty chart is a real answer once the import is done."""
    st = classify(totals=ZERO, connection_status="active",
                  connected_at=NOW - 120, now=NOW, provider="MEDENT")
    assert st.state == READY
    assert st.counts == ZERO


def test_records_that_arrived_beat_a_stale_pending_status():
    """`status` only flips to active while the connect page is open polling.
    Close the tab and it stays 'pending' forever, so records-in-hand has to
    win — otherwise a real chart is reported as still arriving."""
    st = classify(totals=SOME, connection_status="pending",
                  connected_at=NOW - 120, now=NOW, provider="MEDENT")
    assert st.state == READY
    assert st.counts == SOME


def test_past_the_promised_window_we_stop_claiming_they_are_on_the_way():
    """"Still arriving" is itself a claim about a job this app cannot see. Past
    the window /connect promises, we know only that nothing has landed."""
    st = classify(totals=ZERO, connection_status="pending",
                  connected_at=NOW - ARRIVAL_WINDOW_SECONDS - 1, now=NOW,
                  provider="MEDENT")
    assert st.state == OVERDUE
    assert st.counts is None


def test_the_window_boundary_is_still_arriving():
    st = classify(totals=ZERO, connection_status="pending",
                  connected_at=NOW - ARRIVAL_WINDOW_SECONDS, now=NOW)
    assert st.state == ARRIVING


def test_no_connection_at_all():
    st = classify(totals=None, connection_status=None, connected_at=None,
                  now=NOW)
    assert st.state == NO_CONNECTION
    assert st.counts is None


def test_counts_cannot_survive_a_non_ready_state():
    """The structural guard, not a reminder. A caller that hands counts to a
    state that must not speak them gets them dropped rather than rendered —
    'there is nothing to print' beats 'remember not to print it'."""
    st = classify(totals=SOME, connection_status="pending",
                  connected_at=NOW - 60, now=NOW)
    st_forced = classify(totals=ZERO, connection_status="pending",
                         connected_at=NOW - 60, now=NOW)
    assert st.counts is not None and st.state == READY   # arrived: fine
    assert st_forced.counts is None                      # in flight: dropped


def test_unknown_totals_never_read_as_zero():
    """A failed count is not an empty chart. If we could not look, we do not
    get to say what we would have seen."""
    st = classify(totals=None, connection_status="pending",
                  connected_at=NOW - 60, now=NOW)
    assert st.state == ARRIVING
    assert st.counts is None

    active = classify(totals=None, connection_status="active",
                      connected_at=NOW - 60, now=NOW)
    assert active.state == READY
    assert active.counts is None, "records are here; how many is unknown"


def test_minutes_waiting_is_reported_for_the_waiting_states():
    st = classify(totals=ZERO, connection_status="pending",
                  connected_at=NOW - 600, now=NOW)
    assert st.minutes_waiting == 10


def test_a_missing_connected_at_does_not_manufacture_an_overdue():
    """Rows predating the column have no timestamp. Unknown elapsed time is
    not a long elapsed time."""
    st = classify(totals=ZERO, connection_status="pending",
                  connected_at=None, now=NOW)
    assert st.state == ARRIVING
    assert st.minutes_waiting is None


def test_a_revoked_connection_still_reports_the_records_it_left_behind():
    """Disconnect stops new data; it does not erase what already landed."""
    st = classify(totals=SOME, connection_status="revoked",
                  connected_at=NOW - 600, now=NOW)
    assert st.state == READY
    assert st.counts == SOME
