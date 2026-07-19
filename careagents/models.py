"""CareAgents account data — identity + pointers, NEVER PHI.

careagents stores who you are (email, passkeys) and what you own (connections,
agents, surfaces). Health data itself lives only in HealthClaw tenants, behind
redaction/audit/step-up. A Connection here is a pointer (tenant id) to one of
those spaces.

Its own SQLAlchemy metadata + engine (separate from the HealthClaw app's db);
SQLite on the VPS, file-locked 0600.
"""

from __future__ import annotations

import secrets
import time

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Integer,
                        LargeBinary, String, create_engine, inspect, text)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


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


def _ensure_columns(engine) -> None:
    """Idempotently add columns introduced after a table first shipped.

    create_all() only creates missing tables, never new columns on an existing
    one — so the live SQLite DB needs this for `attempts`. SQLite and Postgres
    both support ADD COLUMN ... DEFAULT.
    """
    insp = inspect(engine)
    tables = insp.get_table_names()
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


def make_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
