"""Sign-in codes: burned codes count toward the resend wait, and daily and
per-IP limits hold.

Every code issued for an address counts toward its resend wait and its caps,
used and burned ones included. On top of that: codes per address per hour
and per day, wrong guesses per address per day across codes, and code
requests per client address. The per-address answers stay the same for an
address with an account and one without.

Time is moved by backdating `exp` (issue time + CODE_TTL), never by sleeping.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

import careagents.accounts as accounts_mod
import careagents.app as app_mod
import careagents.mail as mailmod
from careagents.accounts import (CODES_PER_DAY, CODES_PER_HOUR, DAY, HOUR,
                                 MAX_CODE_ATTEMPTS, RESEND_COOLDOWN,
                                 WRONG_GUESSES_PER_DAY, AccountService,
                                 AuthError)
from careagents.beta_signup import mailbox
from careagents.config import Config
from careagents.models import Base, EmailToken, make_engine
from tests.test_careagents import _make_account, _sink_code
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture

WRONG = "00000000"


def _age(svc, email, seconds):
    """Move every code of the address's inbox `seconds` into the past."""
    with svc.session() as s:
        s.query(EmailToken).filter_by(mailbox=mailbox(email)).update(
            {"exp": EmailToken.exp - seconds})


def _issue(svc, email):
    """Issue one code, past the resend wait of the one before. Returns the
    wait start_email_code answered (0 for a send)."""
    _age(svc, email, RESEND_COOLDOWN + 1)
    return svc.start_email_code(email)


def _wrong(svc, email, n):
    for _ in range(n):
        with pytest.raises(AuthError):
            svc.verify_email_code(email, WRONG)


# --- 1. the resend wait counts every code -----------------------------------

def test_a_burned_code_counts_toward_the_resend_wait(svc, monkeypatch):
    """MUTATION: put `used=False` back on the resend-wait query -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    assert svc.start_email_code("burn@example.com") == 0
    _wrong(svc, "burn@example.com", MAX_CODE_ATTEMPTS)    # burned
    wait = svc.start_email_code("burn@example.com")
    assert 0 < wait <= RESEND_COOLDOWN
    assert len(codes) == 1, "a burned code was replaced inside the wait"


def test_a_used_code_counts_toward_the_resend_wait(svc, monkeypatch):
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    svc.start_email_code("used@example.com")
    svc.verify_email_code("used@example.com", codes[-1])
    assert svc.start_email_code("used@example.com") > 0
    assert len(codes) == 1


def test_the_resend_wait_ends(svc, monkeypatch):
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    svc.start_email_code("ends@example.com")
    _wrong(svc, "ends@example.com", MAX_CODE_ATTEMPTS)
    assert _issue(svc, "ends@example.com") == 0
    assert len(codes) == 2


def test_a_code_that_was_never_sent_holds_nothing_back(svc, monkeypatch):
    monkeypatch.setattr(mailmod, "send_code",
                        lambda cfg, e, code, purpose: mailmod.NOT_SENT)
    with pytest.raises(accounts_mod.MailError):
        svc.start_email_code("down@example.com")
    with svc.session() as s:
        assert s.query(EmailToken).filter_by(
            email="down@example.com").count() == 0


# --- 2. codes per address per hour and per day -------------------------------

def test_codes_per_address_are_capped_per_hour(svc, monkeypatch):
    """MUTATION: drop the hourly cap from _code_wait -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "hourly@example.com"
    for _ in range(CODES_PER_HOUR):
        assert _issue(svc, email) == 0
    wait = _issue(svc, email)
    assert RESEND_COOLDOWN < wait <= HOUR
    assert len(codes) == CODES_PER_HOUR
    # Rolling: once the oldest code leaves the hour, one more may go.
    _age(svc, email, wait)
    assert svc.start_email_code(email) == 0
    assert len(codes) == CODES_PER_HOUR + 1


def test_codes_per_address_are_capped_per_day(svc, monkeypatch):
    """Spaced so the hourly cap never holds: only the daily one can.

    MUTATION: drop the daily cap from _code_wait -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "daily@example.com"
    spacing = HOUR // (CODES_PER_HOUR - 1) + 1
    for _ in range(CODES_PER_DAY):
        _age(svc, email, spacing)
        assert svc.start_email_code(email) == 0
    _age(svc, email, spacing)
    wait = svc.start_email_code(email)
    assert HOUR < wait <= DAY
    assert len(codes) == CODES_PER_DAY
    _age(svc, email, wait)
    assert svc.start_email_code(email) == 0


def test_the_route_answers_a_cap_with_the_resend_wait(app, svc, monkeypatch):
    """The same shape the code screen already counts down from."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "shape@example.com"
    for _ in range(CODES_PER_HOUR):
        _issue(svc, email)
    _age(svc, email, RESEND_COOLDOWN + 1)
    r = app.test_client().post("/api/auth/email", json={"email": email})
    assert r.status_code == 200
    body = r.get_json()
    assert body["sent"] is False and body["reason"] == "cooldown"
    assert body["retry_after"] > RESEND_COOLDOWN
    assert len(codes) == CODES_PER_HOUR


def test_a_capped_address_answers_the_same_with_or_without_an_account(
        app, svc, monkeypatch):
    codes = []
    _make_account(svc, monkeypatch, "member@example.com")
    # The member signed up over an hour ago, so both start under the caps.
    _age(svc, "member@example.com", HOUR + 1)
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    c = app.test_client()

    def _probe(email):
        out = []
        for _ in range(CODES_PER_HOUR + 1):
            _age(svc, email, RESEND_COOLDOWN + 1)
            r = c.post("/api/auth/email", json={"email": email})
            body = r.get_json()
            # retry_after is a clock reading, not an account fact.
            out.append((r.status_code, sorted(body),
                        body.get("sent"), body.get("reason")))
        return out

    assert _probe("member@example.com") == _probe("stranger@example.com")


# --- 4. wrong guesses per address per day ------------------------------------

def test_wrong_guesses_across_codes_stop_new_codes(svc, monkeypatch):
    """MUTATION: drop the wrong-guess cap from _code_wait -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "guesses@example.com"
    per = 3
    spent = 0
    while spent < WRONG_GUESSES_PER_DAY:
        assert _issue(svc, email) == 0
        n = min(per, WRONG_GUESSES_PER_DAY - spent)
        _wrong(svc, email, n)
        spent += n
    sent = len(codes)
    wait = _issue(svc, email)
    assert HOUR < wait <= DAY, "the wrong-guess cap holds for the day"
    assert len(codes) == sent
    _age(svc, email, wait)
    assert svc.start_email_code(email) == 0


def test_the_right_code_is_refused_once_the_days_guesses_are_spent(
        svc, monkeypatch):
    """The cap is exact: the guess that reaches it ends the live code too.

    MUTATION: drop the wrong-guess check from verify_email_code -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "exact@example.com"
    per = 3
    spent = 0
    while spent < WRONG_GUESSES_PER_DAY:
        assert _issue(svc, email) == 0
        n = min(per, WRONG_GUESSES_PER_DAY - spent)
        _wrong(svc, email, n)
        spent += n
    # The last code has fewer than MAX_CODE_ATTEMPTS misses, so only the
    # daily cap can refuse it.
    assert WRONG_GUESSES_PER_DAY % per < MAX_CODE_ATTEMPTS
    with pytest.raises(AuthError):
        svc.verify_email_code(email, codes[-1])


def test_wrong_guesses_under_the_cap_leave_the_right_code_working(
        svc, monkeypatch):
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    email = "under@example.com"
    for _ in range((WRONG_GUESSES_PER_DAY - 1) // 4):
        _issue(svc, email)
        _wrong(svc, email, 4)
    _issue(svc, email)
    assert svc.verify_email_code(email, codes[-1]).email == email


# --- one inbox, several spellings ---------------------------------------------

#: Three spellings that deliver to one Gmail inbox (beta_signup.mailbox).
GMAIL_VARIANTS = ("ab+1@gmail.com", "a.b@gmail.com", "ab@googlemail.com")


def _addressed(sink):
    """A send_code stand-in that records (address, code)."""
    def _send(cfg, email, code, purpose):
        sink.append((email, code))
        return mailmod.SENT
    return _send


def test_spellings_of_one_gmail_inbox_share_the_resend_wait(
        svc, monkeypatch):
    """MUTATION: key _issued_within on the typed address -> red."""
    assert len({mailbox(v) for v in GMAIL_VARIANTS}) == 1
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    assert svc.start_email_code(GMAIL_VARIANTS[0]) == 0
    for v in GMAIL_VARIANTS[1:]:
        assert 0 < svc.start_email_code(v) <= RESEND_COOLDOWN
    assert len(sent) == 1


def test_spellings_of_one_gmail_inbox_share_the_hourly_cap(svc, monkeypatch):
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    for i in range(CODES_PER_HOUR):
        assert _issue(svc, GMAIL_VARIANTS[i % 3]) == 0
    for v in GMAIL_VARIANTS:
        _age(svc, v, RESEND_COOLDOWN + 1)
        assert svc.start_email_code(v) > RESEND_COOLDOWN, v
    assert len(sent) == CODES_PER_HOUR


def test_spellings_of_one_gmail_inbox_share_the_wrong_guess_cap(
        svc, monkeypatch):
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    spent, i = 0, 0
    while spent < WRONG_GUESSES_PER_DAY:
        v = GMAIL_VARIANTS[i % 3]
        assert _issue(svc, v) == 0
        n = min(3, WRONG_GUESSES_PER_DAY - spent)
        _wrong(svc, v, n)
        spent, i = spent + n, i + 1
    for v in GMAIL_VARIANTS:
        _age(svc, v, RESEND_COOLDOWN + 1)
        assert svc.start_email_code(v) > HOUR, v


def test_the_code_goes_to_the_address_as_typed(svc, monkeypatch):
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    svc.start_email_code("A.B+Tag@Gmail.com")
    assert sent[0][0] == "a.b+tag@gmail.com"
    assert svc.verify_email_code("a.b+tag@gmail.com",
                                 sent[0][1]).email == "a.b+tag@gmail.com"


def test_a_new_code_for_one_spelling_retires_another_spellings_code(
        svc, monkeypatch):
    """One live code per inbox, whichever spelling asked."""
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    svc.start_email_code("ab+1@gmail.com")
    _age(svc, "ab+1@gmail.com", RESEND_COOLDOWN + 1)
    svc.start_email_code("a.b@gmail.com")
    with pytest.raises(AuthError):
        svc.verify_email_code("ab+1@gmail.com", sent[0][1])


def test_dots_are_not_folded_outside_gmail(svc, monkeypatch):
    sent = []
    monkeypatch.setattr(mailmod, "send_code", _addressed(sent))
    assert svc.start_email_code("a.b@example.com") == 0
    assert svc.start_email_code("ab@example.com") == 0
    assert len(sent) == 2


def test_an_existing_code_table_gains_the_inbox_column_and_index(tmp_path):
    """MUTATION: delete the `mailbox` block from _ensure_columns -> red."""
    from sqlalchemy import create_engine, inspect, text

    from careagents.models import _ensure_columns
    # A bare engine: make_engine runs the very step under test.
    engine = create_engine(f"sqlite:///{tmp_path}/legacy_codes.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_email_tokens (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL, code_hash VARCHAR(64) NOT NULL, "
            "purpose VARCHAR(16), exp FLOAT NOT NULL, used BOOLEAN, "
            "attempts INTEGER DEFAULT 0)"))
    _ensure_columns(engine)
    insp = inspect(engine)
    assert "mailbox" in {c["name"] for c in insp.get_columns(
        "ca_email_tokens")}
    assert "ix_ca_email_tokens_mailbox" in {
        i["name"] for i in insp.get_indexes("ca_email_tokens")}
    _ensure_columns(engine)                      # and again: idempotent
    engine.dispose()


# --- 3. code requests per client address -------------------------------------

def _ask(c, n, ip, prefix="fam", **headers):
    return [c.post("/api/auth/email", json={"email": f"{prefix}{i}@ex.com"},
                   headers={"X-Real-IP": ip, **headers}).status_code
            for i in range(n)]


def test_code_requests_are_limited_per_client_address(app, monkeypatch):
    """MUTATION: skip the per-IP check in auth_email -> red."""
    monkeypatch.setattr(mailmod, "send_code", _sink_code([]))
    c = app.test_client()
    assert _ask(c, app_mod.CODE_REQUESTS_PER_IP, "198.51.100.7") == (
        [200] * app_mod.CODE_REQUESTS_PER_IP)
    r = c.post("/api/auth/email", json={"email": "one-more@ex.com"},
               headers={"X-Real-IP": "198.51.100.7"})
    assert r.status_code == 429
    body = r.get_json()
    assert "Try again in" in body["error"]
    assert 0 < body["retry_after"] <= app_mod.CODE_REQUEST_WINDOW
    # Another network is not held.
    assert _ask(c, 1, "198.51.100.8", prefix="other") == [200]


def test_the_client_address_limit_reads_x_real_ip_not_forwarded_for(
        app, monkeypatch):
    monkeypatch.setattr(mailmod, "send_code", _sink_code([]))
    c = app.test_client()
    statuses = [
        c.post("/api/auth/email", json={"email": f"spoof{i}@ex.com"},
               headers={"X-Real-IP": "198.51.100.9",
                        "X-Forwarded-For": f"203.0.113.{i}"}).status_code
        for i in range(app_mod.CODE_REQUESTS_PER_IP + 1)]
    assert statuses[-1] == 429


def test_the_client_address_limit_is_a_rolling_window(app, monkeypatch):
    monkeypatch.setattr(mailmod, "send_code", _sink_code([]))
    clock = [1_000_000.0]
    monkeypatch.setattr(app_mod.time, "time", lambda: clock[0])
    c = app.test_client()
    _ask(c, app_mod.CODE_REQUESTS_PER_IP, "198.51.100.10")
    assert _ask(c, 1, "198.51.100.10", prefix="late") == [429]
    clock[0] += app_mod.CODE_REQUEST_WINDOW + 1
    assert _ask(c, 1, "198.51.100.10", prefix="later") == [200]


def test_a_racing_burst_cannot_beat_the_client_address_limit(
        app, svc, monkeypatch):
    # The limiter is under test. In-memory SQLite gives each thread its own
    # empty database, so the code store is stood in for.
    monkeypatch.setattr(svc, "start_email_code",
                        lambda email, purpose="verify": 0)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    statuses = []
    try:
        def go(t):
            c = app.test_client()
            statuses.extend(_ask(c, 10, "198.51.100.11", prefix=f"r{t}-"))
        threads = [threading.Thread(target=go, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert statuses.count(200) == app_mod.CODE_REQUESTS_PER_IP
    assert statuses.count(429) == 80 - app_mod.CODE_REQUESTS_PER_IP


# --- racing requests on one address ------------------------------------------

@pytest.fixture
def shared_svc(tmp_path):
    """An AccountService whose threads share one database. In-memory SQLite
    gives each thread its own, so this uses a file, or the Postgres lane's
    database when CARE_TEST_DATABASE_URL is set."""
    url = os.environ.get("CARE_TEST_DATABASE_URL", "")
    if url.startswith("postgres"):
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    else:
        url = f"sqlite:///{tmp_path}/codes.db"
    service = AccountService(Config(env={
        "CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
        "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
        "HEALTHCLAW_MINT_SECRET": "m"}))
    yield service
    service.engine.dispose()


def _race(n, fn):
    barrier = threading.Barrier(n)
    errors = []

    def go():
        barrier.wait()
        try:
            fn()
        except AuthError:
            pass
        except Exception as exc:                 # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_racing_code_requests_cannot_pass_the_resend_wait(
        shared_svc, monkeypatch):
    """MUTATION: make _lock_codes a no-op -> red."""
    codes = []
    lock = threading.Lock()

    def _send(cfg, email, code, purpose):
        with lock:
            codes.append(code)
        return mailmod.SENT
    monkeypatch.setattr(mailmod, "send_code", _send)
    for _ in range(5):
        before = len(codes)
        _age(shared_svc, "race@example.com", RESEND_COOLDOWN + 1)
        _race(8, lambda: shared_svc.start_email_code("race@example.com"))
        assert len(codes) - before == 1, (
            f"{len(codes) - before} codes sent inside one resend wait")


def test_racing_spellings_of_one_inbox_cannot_pass_the_resend_wait(
        shared_svc, monkeypatch):
    """MUTATION: take the lock on the typed address -> red on Postgres. On
    SQLite one write lock covers every address, so there it pins the wait."""
    sent = []
    lock = threading.Lock()

    def _send(cfg, email, code, purpose):
        with lock:
            sent.append(email)
        return mailmod.SENT
    monkeypatch.setattr(mailmod, "send_code", _send)
    spellings = [f"a.b+{i}@gmail.com" if i % 2 else f"ab+{i}@googlemail.com"
                 for i in range(8)]
    for _ in range(5):
        before = len(sent)
        _age(shared_svc, spellings[0], RESEND_COOLDOWN + 1)
        queue = list(spellings)
        qlock = threading.Lock()

        def one():
            with qlock:
                email = queue.pop()
            shared_svc.start_email_code(email)
        _race(len(spellings), one)
        assert len(sent) - before == 1, (
            f"{len(sent) - before} codes sent to one inbox inside one wait")


def test_the_postgres_lock_is_taken_on_the_inbox(svc, monkeypatch):
    """Off the Postgres lane the race test above cannot tell the lock's key
    from the typed address's: SQLite has one write lock for everything.

    MUTATION: take the lock on the typed address -> red."""
    from types import SimpleNamespace
    taken = []

    class _Session:
        def execute(self, stmt, params):
            taken.append(params["k"])
    monkeypatch.setattr(svc, "engine",
                        SimpleNamespace(dialect=SimpleNamespace(
                            name="postgresql")))
    for v in GMAIL_VARIANTS:
        svc._lock_codes(_Session(), v)
    svc._lock_codes(_Session(), "someone@example.com")
    assert len(set(taken[:3])) == 1
    assert taken[3] != taken[0]


def test_racing_wrong_guesses_cannot_pass_the_attempt_limit(
        shared_svc, monkeypatch):
    """Every guess that is compared is counted, however many arrive at once.

    MUTATION: make _lock_codes a no-op -> red."""
    codes = []
    monkeypatch.setattr(mailmod, "send_code", _sink_code(codes))
    compared = []
    lock = threading.Lock()
    real = accounts_mod.hmac.compare_digest

    def _counting(a, b):
        with lock:
            compared.append(1)
        return real(a, b)
    monkeypatch.setattr(accounts_mod.hmac, "compare_digest", _counting)
    # Four rounds of MAX_CODE_ATTEMPTS stay under the day's wrong guesses.
    for _ in range(WRONG_GUESSES_PER_DAY // MAX_CODE_ATTEMPTS - 1):
        _age(shared_svc, "guess@example.com", RESEND_COOLDOWN + 1)
        assert shared_svc.start_email_code("guess@example.com") == 0
        compared.clear()
        _race(12, lambda: shared_svc.verify_email_code("guess@example.com",
                                                       WRONG))
        assert len(compared) <= MAX_CODE_ATTEMPTS, (
            f"{len(compared)} guesses at one code")
