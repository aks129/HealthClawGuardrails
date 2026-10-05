"""CareAgents account data — identity + pointers, NEVER PHI.

careagents stores who you are (email, passkeys) and what you own (connections,
agents, surfaces). Health data itself lives only in HealthClaw tenants, behind
redaction/audit/step-up. A Connection here is a pointer (tenant id) to one of
those spaces.

Its own SQLAlchemy metadata + engine (separate from the HealthClaw app's db);
SQLite on the VPS, file-locked 0600.
"""

from __future__ import annotations

import logging
import secrets
import time

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Integer,
                        LargeBinary, String, UniqueConstraint, create_engine,
                        inspect, text)
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

logger = logging.getLogger(__name__)


def _uid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "ca_accounts"
    id = Column(String(32), primary_key=True, default=lambda: _uid("acct"))
    email = Column(String(255), unique=True, nullable=False, index=True)
    email_verified_at = Column(Float, nullable=True)
    created_at = Column(Float, default=now)
    last_login_at = Column(Float, nullable=True)
    # Sample-connect lease (calm hub spec section 4). Set while this
    # account's sample tenant is minted and seeded, cleared after. A double
    # tap races on this row, not on the tenant mint. A timestamp, not PHI.
    sample_claim_at = Column(Float, nullable=True)
    # When this account was given its first assistant (calm hub spec
    # section 5). A compare-and-set target: it makes a repeated or racing
    # ingest-complete callback a no-op. A timestamp, not PHI.
    first_agent_at = Column(Float, nullable=True)
    # When the person answered "Switch Juniper to your records?" either
    # way (calm hub spec section 5). A timestamp, not PHI.
    switch_prompted_at = Column(Float, nullable=True)
    # When an operator paused this account (beta spec section 4.6). While
    # set, no chat turn reaches a model and no new real connection starts.
    # A timestamp, not PHI.
    real_paused_at = Column(Float, nullable=True)

    passkeys = relationship("Passkey", back_populates="account",
                            cascade="all, delete-orphan")
    connections = relationship("Connection", back_populates="account",
                               cascade="all, delete-orphan")
    agents = relationship("Agent", back_populates="account",
                          cascade="all, delete-orphan")
    surfaces = relationship("Surface", back_populates="account",
                            cascade="all, delete-orphan")


class Passkey(Base):
    __tablename__ = "ca_passkeys"
    id = Column(String(32), primary_key=True, default=lambda: _uid("pk"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    credential_id = Column(LargeBinary, unique=True, nullable=False)
    public_key = Column(LargeBinary, nullable=False)
    sign_count = Column(Integer, default=0)
    name = Column(String(64), default="Passkey")
    created_at = Column(Float, default=now)
    account = relationship("Account", back_populates="passkeys")


class Connection(Base):
    __tablename__ = "ca_connections"
    id = Column(String(32), primary_key=True, default=lambda: _uid("conn"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    kind = Column(String(16), nullable=False)           # sample | fasten
    tenant_id = Column(String(64), nullable=False)      # HealthClaw tenant
    label = Column(String(120), default="My records")
    status = Column(String(16), default="active")       # active|pending|error
    provider = Column(String(120), nullable=True)       # e.g. Epic (Fasten)
    connected_at = Column(Float, default=now)
    # Informed-consent record for real-record connections (CARIN CoC:
    # "informed, proactive consent... in advance of personal data disclosure").
    # Null for sample/synthetic connections, which carry no personal data.
    # consent_version pins WHICH terms were consented to, so a later terms
    # change doesn't silently claim consent it never obtained.
    consented_at = Column(Float, nullable=True)
    consent_version = Column(String(16), nullable=True)
    # When the person last accepted newer terms for this connection (beta
    # spec 4.3). Kept apart from consented_at, which stays the first
    # connect, so the weekly number does not count a re-accept as a new
    # connection.
    reconsented_at = Column(Float, nullable=True)
    # Refresh state. A refresh re-pulls the same tenant; HealthClaw's ingest
    # upserts on (tenant, resource_type, id), so re-pulling never duplicates.
    # last_count is the record count observed at the end of the last sync, so
    # the next one can report "N new records" without diffing every resource.
    last_synced_at = Column(Float, nullable=True)
    last_count = Column(Integer, nullable=True)
    # The same baseline for the types the count deliberately excludes —
    # DocumentReferences, which are ingested but not readable (#226). Without
    # it the only measurable fact is "this tenant holds documents", which is
    # true from the first tick for every MEDENT tenant and so cannot tell an
    # arrival from a record that has held notes all along. A plain integer:
    # no patient data, nothing about the record's content.
    last_uncounted = Column(Integer, nullable=True)
    account = relationship("Account", back_populates="connections")
    agents = relationship("Agent", back_populates="connection")


class Agent(Base):
    __tablename__ = "ca_agents"
    id = Column(String(32), primary_key=True, default=lambda: _uid("agent"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    connection_id = Column(String(32), ForeignKey("ca_connections.id"))
    name = Column(String(48), default="Juniper")
    # Capability specialization (advisors.py); persona stays the voice.
    advisor = Column(String(32), nullable=True)
    persona = Column(String(16), default="calm")
    created_at = Column(Float, default=now)
    account = relationship("Account", back_populates="agents")
    connection = relationship("Connection", back_populates="agents")


class Surface(Base):
    __tablename__ = "ca_surfaces"
    id = Column(String(32), primary_key=True, default=lambda: _uid("surf"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    agent_id = Column(String(32), ForeignKey("ca_agents.id"))
    kind = Column(String(16), nullable=False)           # web|telegram|imessage
    handle = Column(String(120), nullable=True)         # chat id / code
    status = Column(String(16), default="pending")      # active|pending
    bound_at = Column(Float, nullable=True)
    account = relationship("Account", back_populates="surfaces")


class Grant(Base):
    """A consent the person gave a third-party agent to read one connection
    through HealthClaw's MCP server (spec §13.4). A pointer and a decision:
    which tenant, which client, which scopes, when, and whether it was taken
    back. `consent_id` is HealthClaw's key for the same consent; revoking here
    revokes there. No PHI."""
    __tablename__ = "ca_grants"
    id = Column(String(32), primary_key=True, default=lambda: _uid("grant"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    connection_id = Column(String(32), ForeignKey("ca_connections.id"),
                           nullable=True)
    tenant_id = Column(String(64), nullable=False)
    client_id = Column(String(64), nullable=False)
    client_name = Column(String(120), default="An agent")
    # Where the client's codes go (an A-label host): what the hub names the
    # app by, since `client_name` is the client's own choice. NULL on grants
    # from before it was kept.
    redirect_host = Column(String(255), nullable=True)
    scopes = Column(String(255), default="")
    consent_id = Column(String(64), unique=True, nullable=False)
    granted_at = Column(Float, default=now)
    revoked_at = Column(Float, nullable=True)


#: The name of the one-row-per-account-and-day rule on `ca_usage_days`: a
#: constraint on a new table, a unique index on one made before it.
USAGE_DAY_UNIQUE = "uq_ca_usage_days_acct_day"


class UsageDay(Base):
    """Per-account daily LLM turn count — a durable spend ceiling.

    The in-process burst limiter bounds bursts, but it resets on restart and
    is per-worker, so under gunicorn it multiplies by worker count. Inference
    is billed to the operator, so the daily cap has to live somewhere shared
    and durable: one row per account per UTC day.

    Counts only — no message content, nothing PHI-adjacent.

    One row per account and day, enforced (#862 review F5): two racing
    first-of-day inserts made two rows, and a read saw only one. A table
    made before the constraint gets it from `_ensure_usage_day_unique`.
    """
    __tablename__ = "ca_usage_days"
    __table_args__ = (UniqueConstraint("account_id", "day",
                                       name=USAGE_DAY_UNIQUE),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("use"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    turns = Column(Integer, default=0)


class ActivityDay(Base):
    """Per-account daily counts for the weekly number (beta spec 4.5).

    `asked` counts turns on a real-record assistant, `approved` counts
    approvals of a real-record action. Integers only: no message, no action
    kind, nothing about the record.
    """
    __tablename__ = "ca_activity_days"
    __table_args__ = (UniqueConstraint("account_id", "day",
                                       name="uq_ca_activity_days_acct_day"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("act"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    asked = Column(Integer, default=0)
    approved = Column(Integer, default=0)


class PageViewDay(Base):
    """Per-page daily view count for the pages anyone can open.

    Counts only, and deliberately the narrowest thing that answers "is
    anybody coming": one row per UTC day per Flask endpoint name. The
    endpoint name is written by us in this module's routes, so unlike a URL
    it cannot carry a tenant id, a token or a person's data. Nothing about
    the visitor is stored — no address, no agent string, no cookie, no
    identifier of any kind — so this counts views and cannot count people,
    which is the trade this table makes on purpose.
    """
    __tablename__ = "ca_page_view_days"
    __table_args__ = (UniqueConstraint("day", "endpoint",
                                       name="uq_ca_page_view_days_day_endpoint"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("pv"))
    day = Column(String(10), nullable=False, index=True)    # UTC "YYYY-MM-DD"
    endpoint = Column(String(64), nullable=False, index=True)
    views = Column(Integer, default=0)


class RealRecordInvite(Base):
    """An invitation to connect real records (beta pathway spec section 4.2).

    Read in `CARE_REAL_RECORDS=allowlist` mode only, alongside the
    environment allowlist. Account-level data: an email, when, and who
    invited it. No health information.

    One row per email. Revoking stamps `revoked_at`; inviting again clears it.
    A revoked invite blocks NEW real connections; existing ones keep working
    until the person disconnects them or the operator closes real records.
    """
    __tablename__ = "ca_real_record_invites"
    email = Column(String(255), primary_key=True)
    invited_at = Column(Float, nullable=False, default=now)
    invited_by = Column(String(255), nullable=False)
    revoked_at = Column(Float, nullable=True)


class EmailToken(Base):
    """One-time email code (sign-up verify / new-device login)."""
    __tablename__ = "ca_email_tokens"
    id = Column(String(32), primary_key=True, default=lambda: _uid("et"))
    email = Column(String(255), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)
    purpose = Column(String(16), default="verify")
    exp = Column(Float, nullable=False)
    used = Column(Boolean, default=False)
    # Failed-guess counter so a login code can be burned after a few misses
    # (anti-brute-force). See AccountService.verify_email_code.
    attempts = Column(Integer, default=0)


#: What a peer adding the same column first looks like: Postgres says the
#: column "already exists", SQLite says "duplicate column name".
_ADDED_BY_A_PEER = ("already exists", "duplicate column")


def _add_column(engine, table: str, name: str, sql_type: str) -> None:
    """ADD COLUMN, tolerant of a peer process adding it first.

    Web and worker start together on a deploy (see `_create_tables`), so both
    can see the column missing and both issue the ALTER. The loser's "already
    exists" means the column is there, which is what we wanted. Each column
    gets its own transaction: on Postgres one failed statement aborts the
    whole transaction it runs in. Any other error is raised.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
    except (OperationalError, ProgrammingError) as exc:
        if not any(m in str(exc.orig).lower() for m in _ADDED_BY_A_PEER):
            raise


def _ensure_columns(engine) -> None:
    """Idempotently add columns introduced after a table first shipped.

    create_all() only creates missing tables, never new columns on an existing
    one — so the live SQLite DB needs this for `attempts`. SQLite and Postgres
    both support ADD COLUMN ... DEFAULT.
    """
    insp = inspect(engine)
    tables = insp.get_table_names()
    if "ca_accounts" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_accounts")}
        for name in ("sample_claim_at", "first_agent_at",
                     "switch_prompted_at", "real_paused_at"):
            if name not in cols:
                _add_column(engine, "ca_accounts", name, "FLOAT")
    if "ca_grants" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_grants")}
        if "redirect_host" not in cols:
            _add_column(engine, "ca_grants", "redirect_host", "VARCHAR(255)")
    if "ca_email_tokens" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_email_tokens")}
        if "attempts" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE ca_email_tokens ADD COLUMN attempts INTEGER "
                    "DEFAULT 0"))
    if "ca_agents" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_agents")}
        if "advisor" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE ca_agents ADD COLUMN advisor VARCHAR(32)"))
    if "ca_connections" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_connections")}
        with engine.begin() as conn:
            if "consented_at" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_connections ADD COLUMN consented_at FLOAT"))
            if "consent_version" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_connections ADD COLUMN consent_version "
                    "VARCHAR(16)"))
            if "last_synced_at" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_connections ADD COLUMN last_synced_at "
                    "FLOAT"))
            if "last_count" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_connections ADD COLUMN last_count INTEGER"))
            if "last_uncounted" not in cols:
                conn.execute(text(
                    "ALTER TABLE ca_connections ADD COLUMN last_uncounted "
                    "INTEGER"))
        if "reconsented_at" not in cols:
            _add_column(engine, "ca_connections", "reconsented_at", "FLOAT")


def _usage_day_is_unique(engine) -> bool:
    """Is (account_id, day) on ca_usage_days already unique, by constraint
    (a table created from the model) or by index (one migrated here)?"""
    insp = inspect(engine)
    pair = ["account_id", "day"]
    return (any(c["column_names"] == pair
                for c in insp.get_unique_constraints("ca_usage_days"))
            or any(i["unique"] and i["column_names"] == pair
                   for i in insp.get_indexes("ca_usage_days")))


#: Postgres advisory-lock key that serialises the ca_usage_days migration
#: across processes booting together. Any fixed 64-bit number works, as long
#: as nothing else in this database takes the same one.
_USAGE_DAY_LOCK_KEY = 8_620_005

#: Postgres SQLSTATE for a detected deadlock: the transaction was rolled
#: back and is safe to run again.
_DEADLOCK = "40P01"


def _ensure_usage_day_unique(engine) -> None:
    """Give a ca_usage_days made before the constraint one row per account
    and day (#862 review F5), idempotently, on SQLite and Postgres.

    Such a table may already hold duplicates, which would fail the index, so
    each duplicate group is first collapsed into its oldest-id row with the
    group's summed turns: a turn charged is never forgotten. Collapse and
    index run in one transaction. A turn charged by an old process between
    the two can add a duplicate back and fail the index; that is retried.

    Peers are NOT harmless on Postgres (#862 QA): processes booting together
    on duplicates each took row locks to collapse and then the index's table
    lock, and deadlocked; 1 to 3 of 4 boots died. So on Postgres the whole
    transaction first takes one advisory lock, and checks again under it: a
    peer that finished first leaves nothing to do. A deadlock is still
    retried, in case some other writer is involved. SQLite serialises
    writers itself and needs no lock.
    """
    if "ca_usage_days" not in inspect(engine).get_table_names():
        return
    if _usage_day_is_unique(engine):
        return
    postgres = engine.dialect.name == "postgresql"
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                if postgres:
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                 {"k": _USAGE_DAY_LOCK_KEY})
                    if _usage_day_is_unique(conn):
                        return              # a peer finished while we waited
                groups = conn.execute(text(
                    "SELECT account_id, day, MIN(id), SUM(COALESCE(turns, 0)) "
                    "FROM ca_usage_days GROUP BY account_id, day "
                    "HAVING COUNT(*) > 1")).all()
                for account_id, day, keep, total in groups:
                    conn.execute(text(
                        "UPDATE ca_usage_days SET turns = :t WHERE id = :k"),
                        {"t": int(total), "k": keep})
                    conn.execute(text(
                        "DELETE FROM ca_usage_days WHERE account_id = :a "
                        "AND day = :d AND id <> :k"),
                        {"a": account_id, "d": day, "k": keep})
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {USAGE_DAY_UNIQUE} "
                    "ON ca_usage_days (account_id, day)"))
            return
        except (IntegrityError, OperationalError, ProgrammingError) as exc:
            msg = str(exc.orig).lower()
            if any(m in msg for m in _CREATED_BY_A_PEER):
                return                      # a peer made the index first
            retryable = (isinstance(exc, IntegrityError)
                         or getattr(exc.orig, "pgcode", None) == _DEADLOCK)
            if attempt == 2 or not retryable:
                raise
            # Said, not swallowed: under the advisory lock this should not
            # happen, and a test asserts it does not.
            logger.warning("ca_usage_days migration retried: %s",
                           "deadlock" if not isinstance(exc, IntegrityError)
                           else "duplicate added during collapse")


#: What a peer creating the same table first looks like, by backend. SQLite
#: and Postgres both say "already exists"; Postgres can instead trip the
#: unique index on its type catalogue when two CREATE TABLEs overlap.
_CREATED_BY_A_PEER = ("already exists", "pg_type_typname_nsp_index")


def _create_tables(engine) -> None:
    """create_all(), tolerant of another process creating the same tables.

    create_all() checks for each table and then creates it, so web and worker
    starting together on a fresh database both see "missing" and the loser
    dies on "table ca_accounts already exists". That error means the table is
    there, which is what we wanted. The two processes interleave table by
    table, so one retry of create_all() collides again (measured: 4 failures
    in 40 paired starts). Each table is created on its own instead, and a
    collision on it is skipped. Any other error is raised.
    """
    for table in Base.metadata.sorted_tables:
        try:
            table.create(engine, checkfirst=True)
        except (OperationalError, ProgrammingError, IntegrityError) as exc:
            if not any(m in str(exc.orig).lower() for m in _CREATED_BY_A_PEER):
                raise


def make_engine(url: str):
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    pool_kwargs = {}
    if not is_sqlite:
        # Managed Postgres (Railway) drops idle connections. Without these a
        # quiet period is followed by intermittent "server closed the
        # connection" 500s on the next request — check the connection before
        # handing it out, and retire it well before the server would.
        #
        # The numbers: 300s is half of the 600s that PgBouncer's
        # server_idle_timeout defaults to (Railway does not document its own
        # idle window, so we sit well under the common default rather than
        # guess at it). A connection is therefore retired on our side before
        # either the pooler or the server can drop it.
        #
        # Note what pool_recycle does NOT buy: pre_ping pings on *every*
        # checkout, not only stale ones, so recycling does not reduce the
        # per-checkout round trip. What it buys is that the ping almost always
        # succeeds — the expensive discard-and-reconnect path stays rare — and
        # it covers the one race pre_ping cannot, a connection that dies in the
        # gap between a successful ping and the query. One round trip per
        # checkout is the cost; the alternative is a 500 nobody can retry into.
        pool_kwargs = {"pool_pre_ping": True, "pool_recycle": 300}
    engine = create_engine(url, connect_args=connect_args, future=True,
                           **pool_kwargs)
    _create_tables(engine)
    _ensure_columns(engine)
    _ensure_usage_day_unique(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
