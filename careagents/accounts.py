"""Account service — email codes + WebAuthn passkeys, over the careagents DB.

All persistence and identity logic lives here so the Flask layer (app.py) stays
thin. No PHI touches this module — only identity (email, passkeys) and the
account's owned pointers (connections/agents/surfaces).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import secrets
import time
from contextlib import contextmanager

from sqlalchemy import text

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from careagents import mail
from careagents.models import (Account, Agent, Connection, EmailToken, Passkey,
                               Surface, UsageDay, make_engine,
                               make_session_factory, now)

logger = logging.getLogger(__name__)

CODE_TTL = 600  # 10 minutes
MAX_CODE_ATTEMPTS = 5   # burn a login code after this many wrong guesses
RESEND_COOLDOWN = 30    # seconds — don't mint a fresh code (or reset attempts)
                        # while a recent one is still in flight
CODE_MAX = 100_000_000  # 8-digit codes (~26.6 bits)


class AuthError(RuntimeError):
    pass


class MailError(RuntimeError):
    """The one-time code could not be sent.

    Distinct from AuthError: nothing is wrong with what the person typed, so
    this is our failure to report (502), not theirs to correct (400). Login is
    the front door — a silent failure here leaves them staring at an empty
    inbox with no idea whether to wait or retry.
    """


class MailUnconfirmed(MailError):
    """We asked the provider to send and never learned whether it did.

    The third state (#220). Reporting this as a failure is its own false claim:
    the mail may already be in the person's inbox, and burning the code to
    "clean up" would kill a code they are about to type. Subclasses MailError
    so any handler that only knows about failure still degrades safely.
    """


class AccountService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = make_engine(cfg.database_url)
        self.Session = make_session_factory(self.engine)

    @contextmanager
    def session(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- email codes --------------------------------------------------------

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    def start_email_code(self, email: str, purpose: str = "verify") -> int:
        """Mint and send a one-time code.

        Returns 0 when a code was actually sent, otherwise the whole seconds
        until another may be requested. The cooldown branch is not a failure —
        the person still holds a live code — but it is not a send either, and
        the caller must not report one it never observed (#262).
        """
        email = email.strip().lower()
        if "@" not in email or len(email) > 255:
            raise AuthError("Enter a valid email address.")
        code = None
        with self.session() as s:
            # If a code was minted very recently, don't send another — this
            # both avoids code-spam and stops an attacker from resetting the
            # per-code attempt counter by re-requesting.
            recent = (s.query(EmailToken)
                      .filter_by(email=email, used=False)
                      .filter(EmailToken.exp >= now())
                      .order_by(EmailToken.exp.desc()).first())
            if recent is not None:
                # exp is mint-time + CODE_TTL, so whatever the code has left
                # beyond its post-cooldown life is the cooldown that remains.
                remaining = ((recent.exp - now())
                             - (CODE_TTL - RESEND_COOLDOWN))
                if remaining > 0:
                    # Round up: never tell someone to retry a moment early.
                    return max(1, math.ceil(remaining))
            # One live code at a time: retire any prior unused codes so an
            # attacker can't accumulate many simultaneously-valid guesses.
            s.query(EmailToken).filter_by(
                email=email, used=False).update({"used": True})
            code = f"{secrets.randbelow(CODE_MAX):08d}"
            s.add(EmailToken(email=email, code_hash=self._hash(code),
                             purpose=purpose, exp=now() + CODE_TTL))
        # Three outcomes, and each one leaves the just-minted code in a
        # different place. Compared by identity against the named states, never
        # truthiness-tested — every state is a truthy string precisely so that
        # a `if not send_code(...)` cannot be written here again.
        outcome = mail.send_code(self.cfg, email, code, purpose)
        if outcome == mail.NOT_SENT:
            # Burn the code we just minted. It was never delivered, and leaving
            # it live would make the resend cooldown above swallow the person's
            # retry — reporting "sent" without sending anything.
            with self.session() as s:
                s.query(EmailToken).filter_by(
                    email=email, used=False).update({"used": True})
            raise MailError(
                "We couldn't send your code just now. Please try again.")
        if outcome != mail.SENT:
            # UNCONFIRMED — and anything unrecognised, which is the same state:
            # we do not know. Keep the code LIVE. Burning it here is what makes
            # this worse than doing nothing: the mail may already have arrived,
            # and the person would type a code we had just killed. The resend
            # cooldown (30s) is the retry path if nothing turns up.
            raise MailUnconfirmed(
                "We couldn't confirm your code was sent. Check your inbox — "
                "if nothing arrives, ask for another code in 30 seconds.")
        # SENT: no cooldown to report. The early return above is the only path
        # that reports one (#262).
        return 0

    def verify_email_code(self, email: str, code: str) -> Account:
        email = email.strip().lower()
        code = (code or "").strip()
        # The session manager rolls back on any exception, so we must NOT raise
        # inside it — that would undo the attempts increment / burn. Record the
        # outcome, let the session commit, then raise afterwards.
        error: str | None = None
        result: _Row | None = None
        with self.session() as s:
            # Fetch the single live code by email (not by hash) so a wrong
            # guess is counted against it and the code can be burned.
            tok = (s.query(EmailToken)
                   .filter_by(email=email, used=False)
                   .filter(EmailToken.exp >= time.time())
                   .order_by(EmailToken.exp.desc()).first())
            if tok is None:
                error = "That code is wrong or expired."
            elif (tok.attempts or 0) >= MAX_CODE_ATTEMPTS:
                tok.used = True
                error = "Too many attempts — request a new code."
            elif not hmac.compare_digest(tok.code_hash, self._hash(code)):
                tok.attempts = (tok.attempts or 0) + 1
                if tok.attempts >= MAX_CODE_ATTEMPTS:
                    tok.used = True
                error = "That code is wrong or expired."
            else:
                tok.used = True
                acct = s.query(Account).filter_by(email=email).first()
                if acct is None:
                    acct = Account(email=email, email_verified_at=now())
                    s.add(acct)
                elif acct.email_verified_at is None:
                    acct.email_verified_at = now()
                acct.last_login_at = now()
                s.flush()
                result = _detach(acct)
        if error:
            raise AuthError(error)
        return result

    def ping(self) -> bool:
        """True if the account store answers. Used by /healthz readiness."""
        try:
            with self.session() as s:
                s.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("account store unreachable")
            return False

    def get_account(self, account_id: str) -> Account | None:
        with self.session() as s:
            acct = s.get(Account, account_id)
            return _detach(acct) if acct else None

    # --- WebAuthn: registration ---------------------------------------------

    def registration_options(self, account: Account) -> tuple[dict, str]:
        opts = webauthn.generate_registration_options(
            rp_id=self.cfg.rp_id, rp_name=self.cfg.rp_name,
            user_id=account.id.encode(), user_name=account.email,
            user_display_name=account.email,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED),
        )
        challenge = bytes_to_base64url(opts.challenge)
        return _opts_to_dict(webauthn.options_to_json(opts)), challenge

    def finish_registration(self, account_id: str, credential: dict,
                            expected_challenge: str, name: str = "Passkey"):
        verification = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_rp_id=self.cfg.rp_id,
            expected_origin=self.cfg.origin,
        )
        with self.session() as s:
            s.add(Passkey(
                account_id=account_id,
                credential_id=verification.credential_id,
                public_key=verification.credential_public_key,
                sign_count=verification.sign_count, name=name[:64]))

    # --- WebAuthn: authentication -------------------------------------------

    def authentication_options(self) -> tuple[dict, str]:
        opts = webauthn.generate_authentication_options(
            rp_id=self.cfg.rp_id,
            user_verification=UserVerificationRequirement.PREFERRED)
        return (_opts_to_dict(webauthn.options_to_json(opts)),
                bytes_to_base64url(opts.challenge))

    def finish_authentication(self, credential: dict,
                              expected_challenge: str) -> Account:
        raw_id = base64url_to_bytes(credential["rawId"])
        with self.session() as s:
            pk = s.query(Passkey).filter_by(credential_id=raw_id).first()
            if pk is None:
                raise AuthError("Unknown passkey — sign in with an email code.")
            verification = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(expected_challenge),
                expected_rp_id=self.cfg.rp_id,
                expected_origin=self.cfg.origin,
                credential_public_key=pk.public_key,
                credential_current_sign_count=pk.sign_count,
            )
            pk.sign_count = verification.new_sign_count
            acct = s.get(Account, pk.account_id)
            acct.last_login_at = now()
            s.flush()
            return _detach(acct)

    def has_passkey(self, account_id: str) -> bool:
        with self.session() as s:
            return (s.query(Passkey)
                    .filter_by(account_id=account_id).first() is not None)

    # --- connections / agents / surfaces (thin CRUD) ------------------------

    def list_home(self, account_id: str) -> dict:
        with self.session() as s:
            conns = s.query(Connection).filter_by(account_id=account_id).all()
            agents = s.query(Agent).filter_by(account_id=account_id).all()
            surfaces = s.query(Surface).filter_by(account_id=account_id).all()
            return {
                "connections": [_conn_dict(c) for c in conns],
                "agents": [_agent_dict(a) for a in agents],
                "surfaces": [_surf_dict(x) for x in surfaces],
            }

    def add_connection(self, account_id: str, kind: str, tenant_id: str,
                       label: str, status: str = "active",
                       provider: str | None = None,
                       consent_version: str | None = None) -> str:
        with self.session() as s:
            c = Connection(account_id=account_id, kind=kind,
                           tenant_id=tenant_id, label=label[:120],
                           status=status, provider=provider,
                           consented_at=now() if consent_version else None,
                           consent_version=consent_version)
            s.add(c)
            s.flush()
            return c.id

    def claim_daily_turn(self, account_id: str, cap: int) -> tuple[bool, int]:
        """Count one chat turn against today's cap. Returns (allowed, used).

        Durable and shared, unlike the in-process burst limiter — a restart or
        a second gunicorn worker must not hand the account a fresh allowance,
        because every turn costs the operator real money.
        """
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.session() as s:
            row = (s.query(UsageDay)
                   .filter_by(account_id=account_id, day=day).first())
            if row is None:
                row = UsageDay(account_id=account_id, day=day, turns=0)
                s.add(row)
                s.flush()
            used = int(row.turns or 0)
            if used >= cap:
                return False, used
            row.turns = used + 1
            return True, used + 1

    def set_connection_status(self, tenant_id: str, status: str) -> None:
        with self.session() as s:
            for c in s.query(Connection).filter_by(tenant_id=tenant_id).all():
                c.status = status

    def revoke_connection(self, account_id: str, conn_id: str) -> bool:
        """Disconnect: stop new data flowing, keep what's already here."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            if c is None:
                return False
            c.status = "revoked"
            return True

    def delete_connection(self, account_id: str, conn_id: str) -> bool:
        """Remove the connection and any agents pointing at it.

        Called only AFTER the records themselves are confirmed purged, so a
        failed purge can never leave the patient with an empty hub and data
        still sitting in the engine.
        """
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            if c is None:
                return False
            for agent in s.query(Agent).filter_by(connection_id=conn_id).all():
                s.query(Surface).filter_by(agent_id=agent.id).delete()
                s.delete(agent)
            s.delete(c)
            return True

    def get_connection(self, account_id: str, conn_id: str) -> dict | None:
        """Fetch one connection scoped to its owner (never cross-account)."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            return _conn_dict(c) if c else None

    def mark_synced(self, conn_id: str, count: int) -> dict:
        """Record the end of a sync and report what changed since the last one.

        Returns {"new": int, "total": int} where `new` is the growth since the
        previous sync (0 on the first one, since there's no baseline to compare
        against and every record is arguably 'new').
        """
        with self.session() as s:
            c = s.query(Connection).filter_by(id=conn_id).first()
            if c is None:
                return {"new": 0, "total": count}
            previous = c.last_count
            c.last_count = count
            c.last_synced_at = now()
            new = 0 if previous is None else max(0, count - int(previous))
            return {"new": new, "total": count}

    def create_agent(self, account_id: str, name: str, persona: str,
                     connection_id: str, advisor: str | None = None) -> str:
        with self.session() as s:
            if not s.query(Connection).filter_by(
                    id=connection_id, account_id=account_id).first():
                raise AuthError("That connection isn't yours.")
            a = Agent(account_id=account_id, name=name[:48], persona=persona,
                      connection_id=connection_id, advisor=advisor)
            s.add(a)
            s.flush()
            return a.id

    def get_agent_context(self, account_id: str, agent_id: str) -> dict | None:
        """Return {agent, tenant, connection} for an agent the account owns.

        The Connection row is already loaded here to resolve the tenant. It
        used to be discarded, which left the chat greeting with a record count
        and no way to tell an empty chart from an import still in flight
        (#336) — so it is returned rather than fetched a second time.
        """
        with self.session() as s:
            a = s.query(Agent).filter_by(
                id=agent_id, account_id=account_id).first()
            if not a:
                return None
            conn = s.get(Connection, a.connection_id)
            if not conn:
                return None
            return {"agent": _agent_dict(a), "tenant": conn.tenant_id,
                    "connection": _conn_dict(conn)}

    def get_worker_agent_context(self, agent_id: str) -> dict | None:
        """Resolve an agent for the trusted run worker.

        Browser routes must always use :meth:`get_agent_context`, which binds
        the lookup to the signed-in account. The worker has no browser account
        session; it instead verifies this result's tenant against the tenant on
        the claimed HealthClaw run before it handles any data.
        """
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id).first()
            if not a:
                return None
            conn = s.get(Connection, a.connection_id)
            if not conn:
                return None
            return {"agent": _agent_dict(a), "tenant": conn.tenant_id,
                    "account_id": a.account_id}

    def add_surface(self, account_id: str, agent_id: str, kind: str,
                    handle: str | None, status: str = "pending") -> str:
        with self.session() as s:
            if not s.query(Agent).filter_by(
                    id=agent_id, account_id=account_id).first():
                raise AuthError("That agent isn't yours.")
            x = Surface(account_id=account_id, agent_id=agent_id, kind=kind,
                        handle=handle, status=status,
                        bound_at=now() if status == "active" else None)
            s.add(x)
            s.flush()
            return x.id

    def find_surface_by_code(self, code: str,
                             kind: str = "telegram") -> dict | None:
        with self.session() as s:
            x = (s.query(Surface)
                 .filter_by(handle=code, kind=kind, status="pending")
                 .first())
            return _surf_dict(x) | {"account_id": x.account_id} if x else None

    def find_surface_by_handle(self, handle: str,
                               kind: str = "imessage") -> dict | None:
        """Resolve an active surface by the bound external handle — used to
        route an inbound message to the right agent."""
        with self.session() as s:
            x = (s.query(Surface)
                 .filter_by(handle=handle, kind=kind, status="active")
                 .first())
            return _surf_dict(x) | {"account_id": x.account_id} if x else None

    def bind_surface(self, surface_id: str, handle: str) -> None:
        with self.session() as s:
            x = s.get(Surface, surface_id)
            if x:
                x.handle = handle
                x.status = "active"
                x.bound_at = now()


# --- detach helpers: return plain dict-ish objects usable after the session --

class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _detach(acct: Account) -> _Row:
    return _Row(id=acct.id, email=acct.email,
                email_verified_at=acct.email_verified_at)


def _conn_dict(c: Connection) -> dict:
    return {"id": c.id, "kind": c.kind, "tenant_id": c.tenant_id,
            "label": c.label, "status": c.status, "provider": c.provider,
            "connected_at": c.connected_at,
            "last_synced_at": c.last_synced_at, "last_count": c.last_count}


def _agent_dict(a: Agent) -> dict:
    return {"id": a.id, "name": a.name, "persona": a.persona,
            "advisor": a.advisor, "connection_id": a.connection_id}


def _surf_dict(x: Surface) -> dict:
    return {"id": x.id, "kind": x.kind, "handle": x.handle,
            "status": x.status, "agent_id": x.agent_id}


def _opts_to_dict(options_json: str) -> dict:
    import json
    return json.loads(options_json)


def new_binding_code() -> str:
    return base64.b32encode(secrets.token_bytes(6)).decode().rstrip("=").lower()
