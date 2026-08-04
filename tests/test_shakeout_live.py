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


def test_every_check_query_actually_executes(app):
    """MUTATION: rename any queried column (e.g. `recorded` -> `recorded_at`)
    -> red. This is the defect that reached production.

    Each check is called with a cursor wired to the real test database, so a
    column or table that does not exist raises here instead of at 3am against
    the live record.
    """
    import sqlite3

    executed = []

    class _RealCur:
        """Adapts psycopg2-style SQL onto the test database session."""

        def __init__(self):
            self._rows = []

        def execute(self, sql, params=None):
            executed.append(sql)
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

    with app.app_context():
        for name, check in shakeout.CHECKS:
            verdict, detail = check(_RealCur(), "some-tenant")
            assert verdict in ("PASS", "FAIL", "SKIP"), (name, verdict)

    assert len(executed) >= len(shakeout.CHECKS), (
        "a check returned without running its query — it cannot have "
        "measured anything")


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
