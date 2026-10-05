"""The hub's words (calm hub spec section 3 and 7)."""

from __future__ import annotations

from careagents import hub

DAY = 86400.0
NOW = 1_800_000_000.0


def _conn(cid, kind="direct", status="active", **kw):
    return {"id": cid, "kind": kind, "status": status, "label": f"L-{cid}",
            "tenant_id": f"ca-{cid}", "provider": None, "connected_at": NOW,
            "last_synced_at": None, "last_count": None,
            "last_uncounted": None, **kw}


def _agent(aid, conn, name="Juniper"):
    return {"id": aid, "name": name, "persona": "calm", "advisor": None,
            "connection_id": conn}


def test_every_status_word_is_plain():
    assert hub.status_word("active") == "Connected"
    assert hub.status_word("pending") == "Connecting…"
    assert hub.status_word("empty") == "No records yet"
    assert hub.status_word("revoked") == ""
    assert hub.status_word(None) == ""


def test_updated_line_counts_whole_days_and_never_says_zero_days():
    assert hub.updated_line(None, NOW) == ""
    assert hub.updated_line(NOW - 60, NOW) == "Updated today"
    assert hub.updated_line(NOW + 60, NOW) == "Updated today"
    assert hub.updated_line(NOW - DAY - 1, NOW) == "Updated yesterday"
    assert hub.updated_line(NOW - 3 * DAY - 1, NOW) == "Updated 3 days ago"


def test_an_unknown_count_is_no_count_never_zero():
    assert hub.count_line(None) == ""
    assert hub.count_line(0) == "0 records"
    assert hub.count_line(1) == "1 record"
    assert hub.count_line(52) == "52 records"


def test_build_splits_live_from_past_and_names_what_each_agent_reads():
    home = {"connections": [_conn("a"), _conn("s", kind="sample"),
                            _conn("p", status="pending"),
                            _conn("r", status="revoked")],
            "agents": [_agent("g1", "s"), _agent("g2", "gone")],
            "surfaces": []}
    view = hub.build(home, NOW)
    assert [r["id"] for r in view["records"]] == ["a", "s", "p"]
    assert [r["id"] for r in view["past"]] == ["r"]
    assert [r["id"] for r in view["active_records"]] == ["a", "s"]
    assert view["move_choices"] == [{"id": "a", "label": "L-a"},
                                    {"id": "s", "label": "L-s"}]
    assert view["agents"][0]["reads"] == "L-s"
    assert view["agents"][1]["reads"] == ""
    assert view["connected_kinds"] == ["direct", "sample"]
    assert view["has_real"] is True
    sample = next(r for r in view["records"] if r["id"] == "s")
    assert sample["is_sample"] is True and sample["updated"] == "Updated today"


def test_has_real_counts_any_real_connection_not_yet_revoked():
    """#856 sign-off F3: still connecting or empty is still real."""
    def has_real(*conns):
        return hub.build({"connections": list(conns), "agents": []},
                         NOW)["has_real"]
    sample = _conn("s", kind="sample")
    assert has_real(sample, _conn("p", "fasten", "pending")) is True
    assert has_real(sample, _conn("e", "direct", "empty")) is True
    assert has_real(sample, _conn("r", "fasten", "revoked")) is False
    assert has_real(sample) is False


def test_the_switch_prompt_needs_an_agent_on_the_sample_and_a_real_source():
    sample, real = _conn("s", kind="sample"), _conn("f", kind="fasten")
    only_sample = {"connections": [sample], "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(only_sample) is None
    both = {"connections": [sample, real], "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(both) == {"agent_id": "g", "agent_name": "Juniper",
                                       "connection_id": "f", "label": "L-f"}
    on_real = {"connections": [sample, real], "agents": [_agent("g", "f")]}
    assert hub.switch_prompt(on_real) is None
    pending = {"connections": [sample, _conn("f", "fasten", "pending")],
               "agents": [_agent("g", "s")]}
    assert hub.switch_prompt(pending) is None


def test_a_connection_that_never_synced_does_not_say_updated():
    """Nothing arrived, so there is no "Updated today" (PR #843 QA). An
    active row with no sync stamp still reads from when it connected."""
    view = hub.build({"connections": [
        _conn("p", kind="fasten", status="pending"),
        _conn("e", status="empty"),
        _conn("a", status="active"),
        _conn("s", kind="fasten", status="pending", last_synced_at=NOW - DAY),
    ], "agents": []}, NOW)
    by_id = {r["id"]: r["updated"] for r in view["records"]}
    assert by_id == {"p": "", "e": "", "a": "Updated today",
                     "s": "Updated yesterday"}
