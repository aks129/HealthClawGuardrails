"""Guards for the shakeout scorecard's verdict logic.

The checks are pure functions over a cursor, so they are tested against a
stub — the point here is verdict correctness, not SQL. The one property that
matters most: a check must FAIL loudly on the exact live shapes that
motivated it, because a scorecard that shrugs at the bug it was built for is
worse than none.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "shakeout_live",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "shakeout_live.py")
shakeout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shakeout)


class _Cur:
    """Returns scripted rows for fetchone/fetchall, ignoring the SQL."""

    def __init__(self, one=None, all_=None):
        self._one, self._all = one, all_ or []

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


def test_s1_fails_on_interpreted_zero():
    """The #342 shape: the call happened and touched nothing."""
    cur = _Cur(one=("labs $interpret; interpreted=0 flagged=0 critical=0",
                    "2026-08-04"))
    verdict, detail = shakeout.check_s1_labs_interpreted(cur, "t")
    assert verdict == "FAIL"
    assert "#342" in detail or "0 observations" in detail


def test_s1_passes_when_observations_were_interpreted():
    cur = _Cur(one=("labs $interpret; interpreted=179 flagged=12 critical=0",
                    "2026-08-04"))
    assert shakeout.check_s1_labs_interpreted(cur, "t")[0] == "PASS"


def test_s1_skips_when_never_called():
    """No call yet is not a failure — the row says what to do next."""
    verdict, detail = shakeout.check_s1_labs_interpreted(_Cur(one=None), "t")
    assert verdict == "SKIP"
    assert "what do my labs say" in detail


def test_s5_fails_on_a_defect_class_and_ignores_honest_throttling():
    """LLMRateLimited is a truthfully-reported non-defect (PR #345); only
    defect classes fail the card. MUTATION: add LLMRateLimited to
    DEFECT_ERROR_CLASSES -> the second assertion goes red."""
    bad = _Cur(all_=[("completed", "", 3), ("failed", "LLMError", 1)])
    assert shakeout.check_s5_run_health(bad, "t")[0] == "FAIL"

    throttled = _Cur(all_=[("completed", "", 3),
                           ("failed", "LLMRateLimited", 2)])
    assert shakeout.check_s5_run_health(throttled, "t")[0] == "PASS"


def test_throttling_is_reported_even_though_it_passes():
    """MUTATION: drop the rate-limited note -> red.

    Ruling Q3 chose fail-fast over parking a run; the price is that
    throttling must stay visible. Without the count, 'throttled twice ever'
    and 'throttled constantly' read identically, and the ruling's own
    revisit condition could never be observed.
    """
    cur = _Cur(all_=[("completed", "", 3), ("failed", "LLMRateLimited", 2)])
    verdict, detail = shakeout.check_s5_run_health(cur, "t")
    assert verdict == "PASS"
    assert "2/5" in detail and "40%" in detail
    assert "not a defect" in detail


def test_a_clean_card_says_nothing_about_throttling():
    """No rate limits means no noise — the note must be conditional."""
    cur = _Cur(all_=[("completed", "", 4)])
    _verdict, detail = shakeout.check_s5_run_health(cur, "t")
    assert "rate-limited" not in detail


def test_s8_fails_on_the_loop_shape_that_preceded_the_429():
    assert shakeout.check_s8_tool_rounds(_Cur(one=(8,)), "t")[0] == "FAIL"
    assert shakeout.check_s8_tool_rounds(_Cur(one=(4,)), "t")[0] == "PASS"
    assert shakeout.check_s8_tool_rounds(_Cur(one=(None,)), "t")[0] == "SKIP"


def test_stranded_ingest_fails():
    assert shakeout.check_ingest_not_stranded(_Cur(one=(2,)), "t")[0] == "FAIL"
    assert shakeout.check_ingest_not_stranded(_Cur(one=(0,)), "t")[0] == "PASS"


def test_every_scorecard_row_is_wired():
    """MUTATION: comment a check out of CHECKS -> red. A row that exists but
    is not run reads as covered when it is not."""
    wired = {fn.__name__ for _name, fn in shakeout.CHECKS}
    assert wired == {"check_s1_labs_interpreted", "check_s3_medication_reads",
                     "check_s5_run_health", "check_s8_tool_rounds",
                     "check_ingest_not_stranded"}


def test_the_manual_rows_state_what_the_script_cannot_see():
    """The honesty contract from prod_watch: narrow, stated scope."""
    for probe in ("labs", "conditions", "medications", "allergies",
                  "cholesterol", "another tenant"):
        assert probe in shakeout.MANUAL_ROWS


# ---------------------------------------------------------------------------
# The SQL itself
# ---------------------------------------------------------------------------
# Everything above stubs the cursor and ignores the SQL, which is right for
# verdict logic and blind to the thing that actually broke: the first live run
# of this script crashed with UndefinedColumn, because audit_events has
# `recorded`, not `recorded_at`. Every unit test passed while the query could
# not execute at all — the repo's standing trap (a fake proves the call is
# MADE, not that it is ACCEPTED) in its purest form.
#
# So: run the real statements against a real database. On the Postgres CI lane
# this executes them; on SQLite it still catches a wrong column or table name,
# which is the class of defect that shipped.

from models import db  # noqa: E402


def _sqlite_params(sql: str) -> str:
    """psycopg2 %s -> sqlite ?, and %% -> % for LIKE patterns."""
    return sql.replace("%%", "\x00").replace("%s", "?").replace("\x00", "%")


EXECUTED: list[str] = []


class _RealCur:
    """Adapts psycopg2-style SQL onto the real test database session.

    Module-level rather than nested, because every query this script grows
    has to come through here — the one thing unit stubs structurally cannot
    check is whether the statement is executable at all.
    """

    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        import sqlite3

        EXECUTED.append(sql)
        bind = db.engine
        is_sqlite = bind.dialect.name == "sqlite"
        statement = _sqlite_params(sql) if is_sqlite else sql
        if is_sqlite:
            # now() / make_interval are Postgres-only; the point of this
            # test on SQLite is name resolution, so neutralise the clause
            # while leaving every identifier intact.
            statement = statement.replace(
                "created_at > now() - make_interval(hours => ?)",
                "created_at IS NOT NULL AND ? IS NOT NULL")
        raw = bind.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute(statement, tuple(params or ()))
            self._rows = cur.fetchall()
            cur.close()
        except sqlite3.OperationalError as exc:
            raise AssertionError(
                f"shakeout query does not match the schema: {exc}\n{sql}"
            ) from exc
        finally:
            raw.close()

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def test_every_check_query_actually_executes(app):
    """MUTATION: rename any queried column (e.g. `recorded` -> `recorded_at`)
    -> red. This is the defect that reached production.

    Each check is called with a cursor wired to the real test database, so a
    column or table that does not exist raises here instead of at 3am against
    the live record.
    """
    EXECUTED.clear()
    with app.app_context():
        for name, check in shakeout.CHECKS:
            verdict, detail = check(_RealCur(), "some-tenant")
            assert verdict in ("PASS", "FAIL", "SKIP"), (name, verdict)

    assert len(EXECUTED) >= len(shakeout.CHECKS), (
        "a check returned without running its query — it cannot have "
        "measured anything")


# ---------------------------------------------------------------------------
# #378 — tenant discovery
# ---------------------------------------------------------------------------
# The scorecard could not name one of its own states. Scored against a tenant
# the agent does not run on, every behavioural row returned SKIP — identical
# output to "the deploy is unexercised", which is what it was read as. Three
# attempts went by before the real tenant was found by hand-querying
# AgentRun. These pin the two states apart.


def test_discovery_sql_actually_executes(app):
    """MUTATION: rename any column in DISCOVER_TENANTS_SQL (say
    `last_updated` -> `updated_at`) -> red.

    Same reason as the check queries above: this script's one shipped defect
    was a query that every stubbed test accepted and no database would run.
    A discovery query that crashes is worse than no discovery query, because
    it crashes exactly when someone is lost.
    """
    EXECUTED.clear()
    with app.app_context():
        rows = shakeout.discover_tenants(_RealCur())
        assert rows == [], "a fresh database has no tenant activity"
        assert shakeout.tenant_record_count(_RealCur(), "nobody") == 0
    assert len(EXECUTED) == 2, EXECUTED


def _seed_activity(tenant, *, resources=0, audits=0, runs=0, when=None):
    """Insert real rows through the real models, FKs and all."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from r6.models import AuditEventRecord, R6Resource
    from r6.agent_runs.models import AgentRun
    from r6.command_center.models import Conversation, ConversationMessage

    stamp = when or datetime.now(timezone.utc)
    for i in range(resources):
        row = R6Resource(resource_type="Observation", resource_json="{}",
                         resource_id=f"obs-{i}", tenant_id=tenant)
        row.last_updated = stamp
        db.session.add(row)
    for i in range(audits):
        db.session.add(AuditEventRecord(
            id=str(uuid.uuid4()), event_type="read", tenant_id=tenant,
            resource_type="Observation", detail="read; n=1", recorded=stamp))
    if runs:
        conv = Conversation(id=f"conv-{tenant}", tenant_id=tenant)
        db.session.add(conv)
        db.session.flush()
        for i in range(runs):
            msg = ConversationMessage(
                id=f"msg-{tenant}-{i}", tenant_id=tenant,
                conversation_id=conv.id, role="user", text="hi")
            db.session.add(msg)
            db.session.flush()
            db.session.add(AgentRun(
                id=f"run-{tenant}-{i}", tenant_id=tenant,
                conversation_id=conv.id, message_id=msg.id,
                deadline_at=stamp + timedelta(minutes=2), created_at=stamp))
    db.session.commit()


def test_discover_tenants_finds_all_three_kinds_of_activity(app):
    """MUTATION: drop any one of the three UNION branches -> red.

    #378 in one test. The tenant that mattered had agent runs; the two that
    were scored had records and audit events. A listing built on any single
    source hides exactly the tenant someone is hunting for.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("t-runs-only", runs=3, when=now)
        _seed_activity("t-records-only", resources=7,
                       when=now - timedelta(hours=2))
        _seed_activity("t-audits-only", audits=5,
                       when=now - timedelta(hours=4))
        rows = shakeout.discover_tenants(_RealCur())

    by_tenant = {r[0]: r for r in rows}
    assert set(by_tenant) == {"t-runs-only", "t-records-only", "t-audits-only"}
    assert by_tenant["t-runs-only"][1] == 3       # runs
    assert by_tenant["t-records-only"][2] == 7    # resources
    assert by_tenant["t-audits-only"][3] == 5     # audits


def test_discover_tenants_orders_newest_activity_first(app):
    """MUTATION: drop the ORDER BY, or flip it to ASC -> red.

    "Newest first" is the whole ergonomic point: the tenant someone is
    looking for is the one that was just used.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("t-old", resources=1, when=now - timedelta(days=9))
        _seed_activity("t-newest", resources=1, when=now)
        _seed_activity("t-middle", resources=1, when=now - timedelta(days=3))
        rows = shakeout.discover_tenants(_RealCur())

    assert [r[0] for r in rows] == ["t-newest", "t-middle", "t-old"]


def test_counts_merge_across_sources_for_one_tenant(app):
    """MUTATION: change the outer GROUP BY to also group by a per-branch
    column -> red (the tenant splits into three rows).

    One tenant is one row, with all three counts on it — otherwise the
    listing is longer than the tenant list and reads as more tenants than
    exist.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("t-all", runs=2, resources=4, audits=6, when=now)
        rows = shakeout.discover_tenants(_RealCur())

    assert len(rows) == 1
    tenant, runs, resources, audits, _last = rows[0]
    assert (tenant, runs, resources, audits) == ("t-all", 2, 4, 6)


def test_deleted_resources_are_not_counted_as_records(app):
    """MUTATION: drop the is_deleted filter -> red.

    A purged tenant reporting records is the discovery version of the bug
    this whole card exists to catch: a number that says data is there when
    it is not.
    """
    from datetime import datetime, timezone

    from r6.models import R6Resource

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("t-purged", resources=2, when=now)
        for row in R6Resource.query.filter_by(tenant_id="t-purged").all():
            row.is_deleted = True
        db.session.commit()
        assert shakeout.tenant_record_count(_RealCur(), "t-purged") == 0
        rows = shakeout.discover_tenants(_RealCur())

    assert [r[0] for r in rows] == [], (
        "a tenant whose only rows are soft-deleted holds no records")


def test_tenant_record_count_is_scoped_to_the_tenant(app):
    """MUTATION: drop `tenant_id = %s` from tenant_record_count -> red.

    An unscoped count would report every tenant's records as this one's,
    which would silently turn the empty-tenant SKIP wording back off.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("t-has", resources=5, when=now)
        _seed_activity("t-empty", audits=1, when=now)
        assert shakeout.tenant_record_count(_RealCur(), "t-has") == 5
        assert shakeout.tenant_record_count(_RealCur(), "t-empty") == 0


def test_the_listing_reads_no_record_content(app):
    """MUTATION: add `detail` or `resource_json` to the SELECT -> red.

    PHI-free BY CONSTRUCTION, not by filtering afterwards. `audit_events`
    and `r6_resources` both hold free text that real feeds put patient names
    into (docs/constitution.md; the `display` rule in CLAUDE.md). The
    discovery query must never name those columns at all — a listing that
    reads them and then drops them is one refactor away from printing them.
    """
    sql = shakeout.DISCOVER_TENANTS_SQL.lower()
    for content_column in ("resource_json", "detail", "text", "payload",
                           "metadata_json", "outcome_detail_code",
                           "resource_id", "agent_id", "sha256"):
        assert content_column not in sql, (
            f"discovery SQL touches {content_column!r} — the listing carries "
            "ids, counts and timestamps only")

    # And prove it on real rows: nothing from a seeded record's content can
    # appear in the rendered table.
    from datetime import datetime, timezone
    with app.app_context():
        _seed_activity("t-content", resources=1, audits=1,
                       when=datetime.now(timezone.utc))
        table = shakeout.format_tenant_table(
            shakeout.discover_tenants(_RealCur()))
    assert "Observation" not in table and "read; n=1" not in table


def test_the_table_shows_counts_and_a_timestamp_per_tenant():
    """MUTATION: drop a count column from format_tenant_table -> red.

    "with counts and the most recent timestamp" is the requirement; a bare
    list of ids would not have told anyone which tenant to score.
    """
    rows = [("ca-3f9a21bd7e", 142, 1284, 3908, "2026-08-04 21:14:07"),
            ("desktop-demo", 0, 698, 1205, "2026-08-04 18:02:55")]
    table = shakeout.format_tenant_table(rows)
    assert "ca-3f9a21bd7e" in table and "desktop-demo" in table
    for number in ("142", "1284", "3908", "698", "1205"):
        assert number in table
    assert "2026-08-04 21:14:07" in table
    assert table.index("ca-3f9a21bd7e") < table.index("desktop-demo")


def test_an_empty_database_says_so_rather_than_printing_a_bare_header():
    empty = shakeout.format_tenant_table([])
    assert "no tenant" in empty


def test_a_fruitless_run_names_where_the_activity_is():
    """MUTATION: return "" from recent_activity_hint -> red.

    The #378 report: two tenants scored all-SKIP and the output left the
    reader to guess between "unexercised deploy" and "wrong tenant". The run
    has to end by pointing somewhere.
    """
    rows = [("ca-3f9a21bd7e", 142, 1284, 3908, "2026-08-04 21:14:07"),
            ("desktop-demo", 0, 698, 1205, "2026-08-04 18:02:55")]
    hint = shakeout.recent_activity_hint(rows)
    assert hint.startswith("tenants with recent activity:")
    assert "ca-3f9a21bd7e" in hint and "desktop-demo" in hint


def test_the_hint_truncates_instead_of_printing_every_tenant():
    """A hint that scrolls off the terminal is not a hint."""
    rows = [(f"t-{i}", 1, 1, 1, "2026-08-04") for i in range(9)]
    hint = shakeout.recent_activity_hint(rows, limit=3)
    assert "t-0" in hint and "t-8" not in hint and "+6 more" in hint


def test_the_hint_is_honest_when_the_database_itself_is_empty():
    """No tenants anywhere is a different answer from "look over there",
    and saying "look over there" would restart the hunt that #378 is about."""
    hint = shakeout.recent_activity_hint([])
    assert "none" in hint


def test_a_skip_on_an_empty_tenant_says_it_holds_no_records():
    """MUTATION: make explain_skip return `detail` unconditionally -> red.

    Two different problems with two different fixes. On a stocked tenant a
    SKIP means "ask the agent something". On a tenant holding nothing,
    asking changes nothing — the records are elsewhere, which is precisely
    the #378 situation.
    """
    original = "no $interpret call recorded yet — ask 'what do my labs say?' first"
    empty = shakeout.explain_skip(original, 0)
    assert "holds no records" in empty
    assert "ask" not in empty.split("—")[0]
    assert shakeout.explain_skip(original, 186) == original


def test_the_empty_tenant_wording_does_not_send_the_reader_back_to_the_agent():
    """The wrong next step is worse than no next step: it burns another
    round trip before anyone questions the tenant id."""
    assert "holds no records" in shakeout.EMPTY_TENANT_SKIP
    assert "ask the agent" not in shakeout.EMPTY_TENANT_SKIP


def test_a_vacuous_pass_is_not_counted_as_evidence_about_the_agent():
    """MUTATION: add `check_ingest_not_stranded` to BEHAVIOURAL_CHECKS -> red.

    `ingest not stranded` returns PASS on a tenant containing nothing at all,
    because "no stranded jobs" is trivially true of no jobs. Counting it as a
    scored row is what made the first draft of this fix dead code: every run,
    on every wrong tenant, had one PASS and so never looked fruitless.
    """
    wired = {fn.__name__ for _name, fn in shakeout.CHECKS}
    assert shakeout.BEHAVIOURAL_CHECKS < wired, (
        "behavioural rows must be a strict subset — at least one row passes "
        "without the agent having done anything")
    assert "check_ingest_not_stranded" not in shakeout.BEHAVIOURAL_CHECKS

    # And the row really does pass on an empty tenant, which is the premise.
    assert shakeout.check_ingest_not_stranded(_Cur(one=(0,)), "t")[0] == "PASS"


def _run_main(app, monkeypatch, capsys, *argv):
    """Drive main() end to end against the real test database."""
    import sys

    class _Conn:
        def cursor(self):
            return _RealCur()

        def close(self):
            pass

    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", "postgresql://x/y")
    monkeypatch.setattr(shakeout, "_connect", lambda uri: _Conn())
    monkeypatch.setattr(sys, "argv", ["shakeout_live.py", *argv])
    with app.app_context():
        code = shakeout.main()
    return code, capsys.readouterr().out


def test_scoring_the_wrong_tenant_exits_2_and_names_the_right_one(
        app, monkeypatch, capsys):
    """MUTATION: count every non-SKIP row instead of behavioural ones -> red.

    #378 end to end. `desktop-demo` holds 698 records and has never been
    asked anything; the agent runs on `ca-live`. Before this, the run
    reported "all evaluable rows pass (1 checked)" and exited 0 — success,
    for a run that measured nothing about the agent, on the wrong tenant.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("desktop-demo", resources=698,
                       when=now - timedelta(hours=3))
        _seed_activity("ca-live", runs=4, resources=186, when=now)

    code, out = _run_main(app, monkeypatch, capsys, "--tenant", "desktop-demo")

    assert code == 2, out
    assert "no behavioural row was evaluable" in out
    assert "tenants with recent activity:" in out
    assert "ca-live" in out, "the run must name where the activity actually is"


def test_scoring_the_tenant_the_agent_runs_on_does_not_print_the_hint(
        app, monkeypatch, capsys):
    """The pointer is for lost readers. On a tenant that scored something it
    would be noise, and noise is how a card stops being read."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("ca-live", runs=4, resources=186, when=now)

    code, out = _run_main(app, monkeypatch, capsys, "--tenant", "ca-live")

    assert "S5 run health honest" in out
    assert "tenants with recent activity:" not in out
    assert code == 0, out


def test_list_tenants_prints_the_table_and_exits_0(app, monkeypatch, capsys):
    """The whole point of #378, driven through main()."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with app.app_context():
        _seed_activity("desktop-demo", resources=698,
                       when=now - timedelta(hours=3))
        _seed_activity("ca-live", runs=4, resources=186, audits=40, when=now)

    code, out = _run_main(app, monkeypatch, capsys, "--list-tenants")

    assert code == 0, out
    assert "ca-live" in out and "desktop-demo" in out
    assert "698" in out and "186" in out and "40" in out
    assert out.index("ca-live") < out.index("desktop-demo"), "newest first"
    assert "--tenant" in out, "the listing must say what to do with a tenant id"


def test_list_tenants_on_an_empty_database_exits_2(app, monkeypatch, capsys):
    """Nothing anywhere is "cannot evaluate", not "all clear"."""
    code, out = _run_main(app, monkeypatch, capsys, "--list-tenants")
    assert code == 2
    assert "no tenant" in out


def test_list_tenants_runs_without_a_tenant_and_scoring_still_needs_one():
    """MUTATION: restore `required=True` on --tenant -> red.

    Discovery that requires the answer it exists to find is not discovery.
    The reverse must stay true too: scoring without a tenant is an error,
    not a silent scan of everything.
    """
    import contextlib
    import io
    import subprocess
    import sys

    parser_probe = subprocess.run(
        [sys.executable, "scripts/shakeout_live.py", "--list-tenants"],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        env={"PATH": "/usr/bin:/bin"})
    # No database configured -> exit 2 with the URI message, NOT an argparse
    # usage error about a missing --tenant.
    assert parser_probe.returncode == 2, parser_probe.stderr
    assert "SQLALCHEMY_DATABASE_URI" in parser_probe.stdout
    assert "--tenant" not in parser_probe.stderr

    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        _argv = sys.argv
        sys.argv = ["shakeout_live.py"]
        try:
            shakeout.main()
        finally:
            sys.argv = _argv
    assert exc.value.code == 2
    assert "--tenant is required" in err.getvalue()


def test_run_health_is_windowed_not_all_time():
    """MUTATION: drop the window -> red.

    Scored over all history, one already-fixed failure fails the card
    forever, and a card that cannot return to green stops being read. The
    first live run hit exactly this: a single pre-#345 LLMError from a
    provider 429, fixed hours earlier, still failing the row.
    """
    assert shakeout.RUN_HEALTH_WINDOW_HOURS > 0
    cur = _Cur(all_=[("completed", "", 2)])
    _verdict, detail = shakeout.check_s5_run_health(cur, "t")
    assert str(shakeout.RUN_HEALTH_WINDOW_HOURS) in detail, (
        "the card must say the score is windowed, or a reader will take it "
        "as all-time")
