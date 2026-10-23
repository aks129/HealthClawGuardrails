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

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Index, Integer,
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
    # How that latest sign-in was made: one of careagents/signins.py's
    # METHODS, a name we write. The per-day counts are ca_sign_in_days.
    last_login_method = Column(String(16), nullable=True)
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
    people = relationship("Person", back_populates="account",
                          cascade="all, delete-orphan")


#: Who a person is to the account holder (one assistant per person spec,
#: section 2.1, owner decision D4). NULL for the holder themselves.
RELATIONSHIPS = ("parent", "child", "partner", "other")

#: The one free-text field on a person, capped (D4).
NICKNAME_MAX = 24

#: One self person per account: a partial unique index, so `kind="other"`
#: rows are untouched.
SELF_PERSON_UNIQUE = "uq_ca_people_one_self"


class Person(Base):
    """Whose records an assistant reads: the account holder ("self"), or
    someone they care for ("other"). One assistant per person spec,
    section 2.1.

    A grouping and nothing else. It must never gain a legal name, a date of
    birth or any clinical field: those stay in the tenant, behind the
    engine's redaction and audit. The relationship is an enum and the
    nickname ("Mom") is capped at NICKNAME_MAX characters. The engine never
    learns a person exists.
    """
    __tablename__ = "ca_people"
    __table_args__ = (Index(SELF_PERSON_UNIQUE, "account_id", unique=True,
                            sqlite_where=text("kind = 'self'"),
                            postgresql_where=text("kind = 'self'")),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("person"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"),
                        nullable=False)
    kind = Column(String(8), nullable=False, default="self")  # self | other
    # RELATIONSHIPS. Named `relation` here so it does not shadow
    # sqlalchemy's relationship() in this class body; the column is
    # `relationship`, as the spec names it.
    relation = Column("relationship", String(16), nullable=True)
    nickname = Column(String(NICKNAME_MAX), nullable=True)
    # Authority for real records (spec 6.1). Nothing stamps it yet: someone
    # you care for has made-up records only until owner decision D1.
    attested_at = Column(Float, nullable=True)
    attestation_version = Column(String(16), nullable=True)
    # Set while this person has an assistant, cleared when their last one is
    # removed: the compare-and-set that keeps it to one assistant per person
    # (spec 2.2), as Account.first_agent_at is for the first one.
    agent_at = Column(Float, nullable=True)
    created_at = Column(Float, default=now)
    account = relationship("Account", back_populates="people")


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
    # Whose records these are (one assistant per person spec, section 2.2).
    # Always set by the service; nullable in the schema only because neither
    # SQLite nor Postgres can add a NOT NULL column over existing rows.
    person_id = Column(String(32), ForeignKey("ca_people.id"), nullable=True)
    account = relationship("Account", back_populates="connections")
    agents = relationship("Agent", back_populates="connection")


class Agent(Base):
    __tablename__ = "ca_agents"
    id = Column(String(32), primary_key=True, default=lambda: _uid("agent"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    connection_id = Column(String(32), ForeignKey("ca_connections.id"))
    name = Column(String(48), default="Juniper")
    # Capability specialization (advisors.py); persona stays the voice.
    # Superseded by `routines`, and kept readable for one release (spec 2.2).
    advisor = Column(String(32), nullable=True)
    # The tone (personas.py), a setting on the assistant.
    persona = Column(String(16), default="calm")
    # Comma-separated routine ids (advisors.ROUTINES). Prompt blocks only:
    # a routine changes no access. Empty is the base assistant.
    routines = Column(String(255), nullable=True)
    # Whose assistant this is. One per person, kept by the service with
    # Person.agent_at, not by the schema: accounts from before hold several
    # (spec section 4), and they are kept. Nullable for the same reason as
    # Connection.person_id.
    person_id = Column(String(32), ForeignKey("ca_people.id"), nullable=True)
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
    # When a pending pairing code stops working. NULL on a pending row made
    # before this column existed reads as expired (fail closed).
    code_exp = Column(Float, nullable=True)
    # 1 when the next reply on this surface should open with the welcome:
    # set by a bind that had no message to answer (the sign-in link).
    welcome_due = Column(Integer, default=0)
    # iMessage re-verification (#871): a number can be reassigned to someone
    # else. When the owner last proved the handle is theirs (a bind, or a
    # re-confirm through /link), when it last texted, and when we first
    # asked it to re-confirm (NULL while nothing is pending). See
    # careagents.imessage.reverify_due.
    verified_at = Column(Float, nullable=True)
    last_inbound_at = Column(Float, nullable=True)
    reverify_notified_at = Column(Float, nullable=True)
    account = relationship("Account", back_populates="surfaces")


class ImessageLink(Base):
    """A one-time sign-in link texted to an unbound iMessage handle.

    Only the token's SHA-256 is stored. Used once, then stamped; expires
    after 30 minutes. The handle is the sender's address (a phone number or
    an Apple ID email), the same pointer a bound Surface keeps. No PHI.
    """
    __tablename__ = "ca_imessage_links"
    id = Column(String(32), primary_key=True, default=lambda: _uid("iml"))
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    handle = Column(String(120), nullable=False)
    created_at = Column(Float, default=now)
    exp = Column(Float, nullable=False)
    used_at = Column(Float, nullable=True)


class ImessageHandleState(Base):
    """Per-sender counters and the STOP flag, keyed by a SHA-256 of the
    normalized handle so the sender's address itself is not kept here.

    Counts only: failed pairing codes and links issued, each in a window.
    """
    __tablename__ = "ca_imessage_handles"
    handle_key = Column(String(64), primary_key=True)
    opted_out_at = Column(Float, nullable=True)
    fail_count = Column(Integer, default=0)
    fail_window_start = Column(Float, nullable=True)
    link_count = Column(Integer, default=0)
    link_window_start = Column(Float, nullable=True)


class SendblueMessage(Base):
    """One inbound Sendblue text, as pointers: seen once, and the run whose
    answer the worker owes it.

    `key_hash` is a SHA-256 of Sendblue's message_handle, so a webhook
    retry is recognised without keeping Sendblue's id. No content, no
    number: the answer is read from the run when it is sent, and the
    number from the surface. `surface_id` is a plain pointer, not a foreign
    key, because STOP and disconnect delete the surface.
    """
    __tablename__ = "ca_sendblue_messages"
    id = Column(String(32), primary_key=True, default=lambda: _uid("sbm"))
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(Float, default=now)
    surface_id = Column(String(32), nullable=True)
    run_id = Column(String(64), nullable=True)
    delivered_at = Column(Float, nullable=True, index=True)
    # sent | withheld | failed | timeout | unbound | absent
    outcome = Column(String(16), nullable=True)


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


#: How many failed turns one account may be given back per UTC day. A
#: failure still costs the operator its model call, so the refund is
#: bounded: past it, a failed turn counts like any other.
REFUNDS_PER_DAY = 3


class RefundDay(Base):
    """How many failed turns one account was given back on one UTC day
    (AccountService.refund_daily_turn). Counts only, like UsageDay. Its own
    table so `ca_usage_days`, whose shape older databases are migrated
    from, keeps its columns."""
    __tablename__ = "ca_refund_days"
    __table_args__ = (UniqueConstraint("account_id", "day",
                                       name="uq_ca_refund_days_acct_day"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("rfd"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    day = Column(String(10), nullable=False)               # UTC "YYYY-MM-DD"
    count = Column(Integer, nullable=False, default=0)


class VoiceUsageDay(Base):
    """Per-account daily speech spend: seconds transcribed, characters
    spoken (careagents/voice.py). ElevenLabs bills both to the operator, so
    the cap is durable and shared, like UsageDay.

    Integers only. No audio, no transcript, no spoken text.
    """
    __tablename__ = "ca_voice_usage_days"
    __table_args__ = (UniqueConstraint("account_id", "day",
                                       name="uq_ca_voice_usage_days_acct_day"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("vu"))
    account_id = Column(String(32), ForeignKey("ca_accounts.id"), index=True)
    day = Column(String(10), nullable=False, index=True)   # UTC "YYYY-MM-DD"
    stt_seconds = Column(Integer, default=0)
    tts_chars = Column(Integer, default=0)


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


class ModelUsageDay(Base):
    """How chat turns were answered, one integer per UTC day per metric.

    The metric is one of careagents/routing.py's fixed names (turns by tier,
    escalations, advisor consultations, refusals, cut-offs). No account, no
    conversation, no text: a row cannot be about a person.
    """
    __tablename__ = "ca_model_usage_days"
    __table_args__ = (UniqueConstraint("day", "metric",
                                       name="uq_ca_model_usage_days_day_metric"),)
    id = Column(String(32), primary_key=True, default=lambda: _uid("mu"))
    day = Column(String(10), nullable=False, index=True)    # UTC "YYYY-MM-DD"
    metric = Column(String(32), nullable=False)
    count = Column(Integer, default=0)


class RealRecordInvite(Base):
    """An invitation to connect real records (beta pathway spec section 4.2).

    Read in `CARE_REAL_RECORDS=allowlist` mode only, alongside the
    environment allowlist. Account-level data: an email, when, and who
    invited it. No health information.

    One row per inbox: `email` is the match key (`invites.invite_key`: case
    everywhere, and dots and +tags on Gmail only), so every alias of an
    invited inbox matches; `display_email` is the address as the owner
    typed it, for their list. Revoking stamps `revoked_at`; inviting again
    clears it. A revoked invite blocks NEW real connections; existing ones
    keep working until the person disconnects them or the operator closes
    real records.
    """
    __tablename__ = "ca_real_record_invites"
    email = Column(String(255), primary_key=True)
    invited_at = Column(Float, nullable=False, default=now)
    invited_by = Column(String(255), nullable=False)
    revoked_at = Column(Float, nullable=True)
    # NULL: the same as `email` (a row from before the key was the inbox).
    display_email = Column(String(255), nullable=True)


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
    # The inbox `email` delivers to (beta_signup.mailbox): the key the
    # per-address caps count on, so variants of one inbox share them.
    # NULL on codes issued before it existed.
    mailbox = Column(String(255), index=True)


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
        if "last_login_method" not in cols:
            _add_column(engine, "ca_accounts", "last_login_method",
                        "VARCHAR(16)")
    if "ca_surfaces" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_surfaces")}
        if "code_exp" not in cols:
            _add_column(engine, "ca_surfaces", "code_exp", "FLOAT")
        if "welcome_due" not in cols:
            _add_column(engine, "ca_surfaces", "welcome_due",
                        "INTEGER DEFAULT 0")
        for name in ("verified_at", "last_inbound_at",
                     "reverify_notified_at"):
            if name not in cols:
                _add_column(engine, "ca_surfaces", name, "FLOAT")
        # A binding from before #871 counts as verified when it was bound,
        # or now if that was never recorded, and as last heard from now:
        # silence counts from the deploy, so existing testers are not all
        # asked (and their owners emailed) that day. Idempotent: only NULLs
        # change.
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE ca_surfaces SET verified_at = COALESCE(bound_at, :t) "
                "WHERE kind = 'imessage' AND status = 'active' "
                "AND verified_at IS NULL"), {"t": now()})
            conn.execute(text(
                "UPDATE ca_surfaces SET last_inbound_at = :t "
                "WHERE kind = 'imessage' AND status = 'active' "
                "AND last_inbound_at IS NULL"), {"t": now()})
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
        if "mailbox" not in cols:
            _add_column(engine, "ca_email_tokens", "mailbox", "VARCHAR(255)")
            # The index create_all gives a new table, under its name. A
            # peer racing the same statement on Postgres loses with a
            # duplicate key on the catalogue: the index is there.
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_ca_email_tokens_mailbox "
                        "ON ca_email_tokens (mailbox)"))
            except IntegrityError as exc:
                if "duplicate key" not in str(exc.orig).lower():
                    raise
    if "ca_agents" in tables:
        cols = {c["name"] for c in insp.get_columns("ca_agents")}
        if "advisor" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE ca_agents ADD COLUMN advisor VARCHAR(32)"))
        if "routines" not in cols:
            _add_column(engine, "ca_agents", "routines", "VARCHAR(255)")
        if "person_id" not in cols:
            _add_column(engine, "ca_agents", "person_id", "VARCHAR(32)")
    if "ca_beta_mail_caps" in tables:
        # The rolling day's send times (beta_signup.claim_mail) and the
        # last "how to sign in" email (claim_already). A row from before
        # them reads as one email, at sent_at.
        cols = {c["name"] for c in insp.get_columns("ca_beta_mail_caps")}
        if "sends" not in cols:
            _add_column(engine, "ca_beta_mail_caps", "sends", "VARCHAR(128)")
        if "already_at" not in cols:
            _add_column(engine, "ca_beta_mail_caps", "already_at", "FLOAT")
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
        if "person_id" not in cols:
            _add_column(engine, "ca_connections", "person_id", "VARCHAR(32)")


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


#: One active iMessage binding per handle (#866 security F2): a partial
#: unique index, so pending rows (which carry a pairing code in `handle`)
#: and other surface kinds are untouched.
IMESSAGE_HANDLE_UNIQUE = "uq_ca_surfaces_imessage_active_handle"

#: Advisory-lock key for that migration; distinct from the usage-day one.
_IMESSAGE_HANDLE_LOCK_KEY = 8_660_002


def _imessage_handle_is_unique(conn_or_engine) -> bool:
    return any(i["name"] == IMESSAGE_HANDLE_UNIQUE
               for i in inspect(conn_or_engine).get_indexes("ca_surfaces"))


def _ensure_imessage_handle_unique(engine) -> None:
    """Create the partial unique index on active iMessage handles,
    idempotently, on SQLite and Postgres.

    A table from before it may hold one handle active on two rows (the race
    the index closes). Each such group keeps its newest binding (latest
    `bound_at`, then highest id) and the rest are deleted, in the same
    transaction as the index. On Postgres an advisory lock serialises peers
    booting together, as `_ensure_usage_day_unique` does.
    """
    from careagents.imessage import normalize_handle
    if "ca_surfaces" not in inspect(engine).get_table_names():
        return
    if _imessage_handle_is_unique(engine):
        return
    postgres = engine.dialect.name == "postgresql"
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                if postgres:
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                 {"k": _IMESSAGE_HANDLE_LOCK_KEY})
                    if _imessage_handle_is_unique(conn):
                        return
                # Rows bound before handles were normalized are rewritten to
                # the normalized form first, so two spellings of one sender
                # meet in the dedupe below. One that will not normalize keeps
                # its spelling (still found by the raw-handle fallback).
                rows = conn.execute(text(
                    "SELECT id, handle, bound_at FROM ca_surfaces "
                    "WHERE kind = 'imessage' AND status = 'active'")).all()
                groups: dict = {}
                for row_id, handle, bound_at in rows:
                    norm = normalize_handle(handle) or handle
                    groups.setdefault(norm, []).append(
                        (bound_at or 0.0, row_id, handle))
                for norm, members in groups.items():
                    members.sort()
                    keep_bound, keep_id, keep_handle = members[-1]
                    for _, row_id, _h in members[:-1]:
                        conn.execute(text(
                            "DELETE FROM ca_surfaces WHERE id = :i"),
                            {"i": row_id})
                    if keep_handle != norm:
                        conn.execute(text(
                            "UPDATE ca_surfaces SET handle = :h "
                            "WHERE id = :i"), {"h": norm, "i": keep_id})
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS "
                    f"{IMESSAGE_HANDLE_UNIQUE} ON ca_surfaces (handle) "
                    "WHERE kind = 'imessage' AND status = 'active'"))
            return
        except (IntegrityError, OperationalError, ProgrammingError) as exc:
            msg = str(exc.orig).lower()
            if any(m in msg for m in _CREATED_BY_A_PEER):
                return
            retryable = (isinstance(exc, IntegrityError)
                         or getattr(exc.orig, "pgcode", None) == _DEADLOCK)
            if attempt == 2 or not retryable:
                raise
            logger.warning("ca_surfaces handle index migration retried")


#: Advisory-lock key for the people migration; distinct from the others.
_PEOPLE_LOCK_KEY = 9_430_001

#: The routine each legacy advisor becomes (spec sections 4 and 7, owner
#: decision D6). Anything not listed (general, NULL, an unavailable
#: advisor) becomes no routine: the base assistant.
LEGACY_ADVISOR_ROUTINES = {"healthy-habits": "habits",
                           "diet-exercise": "habits",
                           "care-completion": "care-gaps",
                           "medication-refills": "refills"}


def _people_backfilled(conn) -> bool:
    """Is there nothing left for `_ensure_people` to do?"""
    return conn.execute(text(
        "SELECT "
        "(SELECT COUNT(*) FROM ca_accounts a WHERE NOT EXISTS ("
        "SELECT 1 FROM ca_people p WHERE p.account_id = a.id "
        "AND p.kind = 'self')) + "
        "(SELECT COUNT(*) FROM ca_connections WHERE person_id IS NULL) + "
        "(SELECT COUNT(*) FROM ca_agents WHERE person_id IS NULL "
        "OR routines IS NULL)")).scalar() == 0


def _ensure_people(engine) -> None:
    """Give every account its self person and point every existing
    connection and assistant at it (one assistant per person spec,
    section 4). Idempotent, on SQLite and Postgres.

    Every legacy row goes to the SELF person, a second assistant on another
    connection included. Its records might be someone else's, but marking
    them so would infer authority from a pointer; the holder says so, never
    the migration. Nothing is merged: each assistant keeps its transcript,
    which lives in its tenant under its own id.

    A self person whose account has an assistant starts with `agent_at`
    set, so one assistant per person holds from the first boot. One whose
    first assistant was removed starts clear: the holder may ask for one,
    and `first_agent_at` still keeps a callback from bringing it back. On Postgres an advisory lock serialises web and worker
    booting together, as `_ensure_usage_day_unique` does.
    """
    tables = set(inspect(engine).get_table_names())
    if not {"ca_accounts", "ca_people", "ca_connections",
            "ca_agents"} <= tables:
        return
    with engine.connect() as conn:
        if _people_backfilled(conn):
            return
    postgres = engine.dialect.name == "postgresql"
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                if postgres:
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                 {"k": _PEOPLE_LOCK_KEY})
                    if _people_backfilled(conn):
                        return              # a peer finished while we waited
                missing = conn.execute(text(
                    "SELECT a.id FROM ca_accounts a "
                    "WHERE NOT EXISTS (SELECT 1 FROM ca_people p "
                    "WHERE p.account_id = a.id AND p.kind = 'self')")).all()
                for (account_id,) in missing:
                    has_agent = conn.execute(text(
                        "SELECT 1 FROM ca_agents WHERE account_id = :a"),
                        {"a": account_id}).first() is not None
                    conn.execute(text(
                        "INSERT INTO ca_people (id, account_id, kind, "
                        "agent_at, created_at) "
                        "VALUES (:i, :a, 'self', :g, :t)"),
                        {"i": _uid("person"), "a": account_id,
                         "g": now() if has_agent else None, "t": now()})
                for table in ("ca_connections", "ca_agents"):
                    conn.execute(text(
                        f"UPDATE {table} SET person_id = ("
                        "SELECT p.id FROM ca_people p "
                        f"WHERE p.account_id = {table}.account_id "
                        "AND p.kind = 'self') WHERE person_id IS NULL"))
                for advisor, routine in LEGACY_ADVISOR_ROUTINES.items():
                    conn.execute(text(
                        "UPDATE ca_agents SET routines = :r "
                        "WHERE routines IS NULL AND advisor = :a"),
                        {"r": routine, "a": advisor})
                conn.execute(text(
                    "UPDATE ca_agents SET routines = '' "
                    "WHERE routines IS NULL"))
            return
        except (IntegrityError, OperationalError, ProgrammingError) as exc:
            retryable = (isinstance(exc, IntegrityError)
                         or getattr(exc.orig, "pgcode", None) == _DEADLOCK)
            if attempt == 2 or not retryable:
                raise
            # A peer inserted the same self person first; the next pass
            # finds it and skips that account.
            logger.warning("ca_people migration retried")


#: Advisory-lock key for the invite-key migration; distinct from the others.
_INVITE_KEYS_LOCK_KEY = 9_280_002


def _invites_to_rekey(conn) -> list:
    """Rows of ca_real_record_invites not yet keyed by their inbox, with
    every row that shares an inbox with one of them."""
    from careagents.invites import invite_key
    rows = conn.execute(text(
        "SELECT email, invited_at, invited_by, revoked_at, display_email "
        "FROM ca_real_record_invites")).all()
    stale = {invite_key(r[0]) for r in rows if invite_key(r[0]) != r[0]}
    return [r for r in rows if invite_key(r[0]) in stale]


def _ensure_invite_keys(engine) -> None:
    """Key every real-record invite by its inbox (`invites.invite_key`).
    Before, a row was keyed by the address as typed, and only that exact
    string matched. Idempotent, on SQLite and Postgres.

    A row whose key changes keeps the typed address as `display_email`.
    Two aliases of one inbox become one row: the latest owner action
    between them wins, a revoke at the same moment as an invite included,
    so an inbox the owner last revoked stays revoked.

    Web and worker boot together. On Postgres an advisory lock serialises
    them, as `_ensure_people` does; on SQLite the first write takes the
    database's write lock before the rows are read, so the second boot
    reads the first one's result and finds nothing to do.
    """
    from careagents.invites import invite_key
    insp = inspect(engine)
    if "ca_real_record_invites" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("ca_real_record_invites")}
    if "display_email" not in cols:
        _add_column(engine, "ca_real_record_invites", "display_email",
                    "VARCHAR(255)")
    with engine.connect() as conn:
        if not _invites_to_rekey(conn):
            return
    postgres = engine.dialect.name == "postgresql"
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                if postgres:
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                 {"k": _INVITE_KEYS_LOCK_KEY})
                else:
                    conn.execute(text(
                        "UPDATE ca_real_record_invites SET email = email "
                        "WHERE 1 = 0"))
                groups: dict[str, list] = {}
                for r in _invites_to_rekey(conn):
                    groups.setdefault(invite_key(r[0]), []).append(r)
                for key, group in groups.items():
                    # (moment of the last action, revoked) - a tie goes
                    # to the revoke.
                    win = max(group, key=lambda r: (
                        r[3] if r[3] is not None else r[1], r[3] is not None))
                    display = win[4] or (win[0] if win[0] != key else None)
                    for r in group:
                        conn.execute(text(
                            "DELETE FROM ca_real_record_invites "
                            "WHERE email = :e"), {"e": r[0]})
                    conn.execute(text(
                        "INSERT INTO ca_real_record_invites (email, "
                        "invited_at, invited_by, revoked_at, display_email) "
                        "VALUES (:e, :a, :b, :r, :d)"),
                        {"e": key, "a": win[1], "b": win[2], "r": win[3],
                         "d": display})
            return
        except (IntegrityError, OperationalError, ProgrammingError) as exc:
            retryable = (isinstance(exc, IntegrityError)
                         or getattr(exc.orig, "pgcode", None) == _DEADLOCK
                         or "locked" in str(exc.orig).lower())
            if attempt == 2 or not retryable:
                raise
            logger.warning("ca_real_record_invites key migration retried")


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
    # hide_parameters: a database error otherwise writes its bound values
    # (emails, handles) into str(exc) and tracebacks, and so into the log
    # (FHI-60).
    engine = create_engine(url, connect_args=connect_args, future=True,
                           hide_parameters=True, **pool_kwargs)
    _create_tables(engine)
    _ensure_columns(engine)
    _ensure_usage_day_unique(engine)
    _ensure_imessage_handle_unique(engine)
    _ensure_people(engine)
    _ensure_invite_keys(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
