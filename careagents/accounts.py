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

from sqlalchemy import func, or_, text, update
from sqlalchemy.exc import IntegrityError

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from careagents import mail
from careagents.models import (Account, ActivityDay, Agent, Connection,
                               EmailToken, Grant, ImessageHandleState,
                               ImessageLink, Passkey, RealRecordInvite,
                               Surface, UsageDay, make_engine,
                               make_session_factory, now)

logger = logging.getLogger(__name__)

CODE_TTL = 600  # 10 minutes
MAX_CODE_ATTEMPTS = 5   # burn a login code after this many wrong guesses
RESEND_COOLDOWN = 30    # seconds — don't mint a fresh code (or reset attempts)
                        # while a recent one is still in flight
CODE_MAX = 100_000_000  # 8-digit codes (~26.6 bits)
SAMPLE_LEASE_SECONDS = 60  # a sample seed that has not finished by then died


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

    def authentication_options(self, require_uv: bool = False) -> tuple[dict, str]:
        """`require_uv` asks the authenticator for user verification (face,
        fingerprint, PIN) rather than mere presence: the consent step wants
        the person, not the device."""
        opts = webauthn.generate_authentication_options(
            rp_id=self.cfg.rp_id,
            user_verification=(UserVerificationRequirement.REQUIRED if require_uv
                               else UserVerificationRequirement.PREFERRED))
        return (_opts_to_dict(webauthn.options_to_json(opts)),
                bytes_to_base64url(opts.challenge))

    def finish_authentication(self, credential: dict,
                              expected_challenge: str,
                              require_uv: bool = False) -> Account:
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
                require_user_verification=require_uv,
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

    def list_passkeys(self, account_id: str) -> list[dict]:
        with self.session() as s:
            rows = (s.query(Passkey).filter_by(account_id=account_id)
                    .order_by(Passkey.created_at.asc()).all())
            return [{"id": p.id, "name": p.name, "created_at": p.created_at}
                    for p in rows]

    # --- real-record invites (beta pathway spec section 4.2) ---------------

    @staticmethod
    def _invite_email(email: str) -> str:
        email = (email or "").strip().lower()
        if "@" not in email or len(email) > 255:
            raise AuthError("Enter a valid email address.")
        return email

    def invite_real_records(self, email: str, invited_by: str) -> bool:
        """Invite an email to connect real records. Inviting a revoked email
        again reopens it. Returns False when it was already invited.

        Stage 1 holds at most STAGE1_INVITE_CAP active invites; a new or
        reopened one past that raises ValueError. Two operators racing on
        the last place can both get in: a hand-run command, not worth a lock.
        """
        from careagents.beta import STAGE1_INVITE_CAP
        email = self._invite_email(email)
        invited_by = (invited_by or "").strip()[:255]
        if not invited_by:
            raise ValueError("invited_by is required")
        with self.session() as s:
            row = s.get(RealRecordInvite, email)
            if row is not None and row.revoked_at is None:
                return False
            active = (s.query(RealRecordInvite)
                      .filter(RealRecordInvite.revoked_at.is_(None)).count())
            if active >= STAGE1_INVITE_CAP:
                raise ValueError(
                    f"Stage 1 is full: {STAGE1_INVITE_CAP} active invites. "
                    "Revoke one first.")
            if row is None:
                s.add(RealRecordInvite(email=email, invited_at=now(),
                                       invited_by=invited_by))
                return True
            row.revoked_at = None
            row.invited_at = now()
            row.invited_by = invited_by
            return True

    def revoke_real_records_invite(self, email: str) -> bool:
        """Revoke an invite. New real connections are refused from now on;
        existing ones are left alone. Returns False when there was no live
        invite to revoke."""
        email = self._invite_email(email)
        with self.session() as s:
            row = s.get(RealRecordInvite, email)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = now()
            return True

    def real_records_invited(self, email) -> bool:
        """Does this email hold a live (not revoked) invite?"""
        email = (email or "").strip().lower()
        if not email:
            return False
        with self.session() as s:
            row = s.get(RealRecordInvite, email)
            return row is not None and row.revoked_at is None

    # --- pause (beta spec section 4.6) --------------------------------------

    def set_paused(self, email: str, paused: bool) -> bool:
        """Pause or resume one account by its email. False when no account
        has that email. Logged by account id, never by email."""
        email = (email or "").strip().lower()
        with self.session() as s:
            acct = s.query(Account).filter_by(email=email).first()
            if acct is None:
                return False
            acct.real_paused_at = now() if paused else None
            logger.info("account %s %s by operator", acct.id,
                        "paused" if paused else "resumed")
            return True

    def is_paused(self, account_id: str) -> bool:
        with self.session() as s:
            acct = s.get(Account, account_id)
            return bool(acct and acct.real_paused_at is not None)

    def real_record_invites(self) -> list[dict]:
        with self.session() as s:
            rows = s.query(RealRecordInvite).order_by(
                RealRecordInvite.invited_at).all()
            return [{"email": r.email, "invited_at": r.invited_at,
                     "invited_by": r.invited_by, "revoked_at": r.revoked_at}
                    for r in rows]

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

    def active_sample(self, account_id: str) -> dict | None:
        """The account's oldest active sample, if any. Older accounts can
        hold several; none is merged or removed (calm hub spec section 5)."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(account_id=account_id, kind="sample",
                            status="active")
                 .order_by(Connection.connected_at.asc()).first())
            return _conn_dict(c) if c else None

    def pending_connection(self, account_id: str, kind: str) -> dict | None:
        """The account's oldest connection of this kind still waiting for
        records, if any."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(account_id=account_id, kind=kind,
                            status="pending")
                 .order_by(Connection.connected_at.asc()).first())
            return _conn_dict(c) if c else None

    def claim_sample_start(self, account_id: str) -> bool:
        """Win the right to mint a tenant for this account: the sample tap
        and the Fasten connect share this lease (#847).

        A compare-and-set on the account row: one caller sets the lease and
        every overlapping caller updates zero rows. A lease older than
        SAMPLE_LEASE_SECONDS counts as abandoned, so a crash mid-seed cannot
        lock the sample out for good.
        """
        t = now()
        with self.session() as s:
            won = (s.query(Account)
                   .filter(Account.id == account_id,
                           or_(Account.sample_claim_at.is_(None),
                               Account.sample_claim_at
                               < t - SAMPLE_LEASE_SECONDS))
                   .update({"sample_claim_at": t},
                           synchronize_session=False))
            return won == 1

    def release_sample_start(self, account_id: str) -> None:
        with self.session() as s:
            (s.query(Account).filter(Account.id == account_id)
             .update({"sample_claim_at": None}, synchronize_session=False))

    def claim_daily_turn(self, account_id: str, cap: int) -> tuple[bool, int]:
        """Count one chat turn against today's cap. Returns (allowed, used).

        Durable and shared, unlike the in-process burst limiter — a restart or
        a second gunicorn worker must not hand the account a fresh allowance,
        because every turn costs the operator real money.

        Atomic (#862 review F5): the worker charges from several slots at
        once, and a read-then-write let two of them pass on one count. One
        conditional UPDATE both checks and charges, so it either adds one
        under the cap or changes nothing. With no row yet, insert one; the
        unique (account_id, day) turns a racing insert into a retry of the
        UPDATE, the same shape as count_activity.
        """
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        turns = func.coalesce(UsageDay.turns, 0)
        charge = (update(UsageDay)
                  .where(UsageDay.account_id == account_id,
                         UsageDay.day == day, turns < cap)
                  .values(turns=turns + 1)
                  .execution_options(synchronize_session=False))
        for _ in range(4):
            with self.session() as s:
                if s.execute(charge).rowcount:
                    # Our row lock holds until commit: this reads our charge.
                    return True, self._turns_today(s, account_id, day)
                if s.query(UsageDay.id).filter_by(
                        account_id=account_id, day=day).first():
                    used = self._turns_today(s, account_id, day)
                    if used >= cap:
                        return False, used
                    # Under the cap: a peer inserted today's row after our
                    # UPDATE looked and found none (#862 security G1).
                    # Charge it, rather than refuse a turn the cap allows.
                    continue
            if cap <= 0:
                return False, 0
            try:
                with self.session() as s:
                    s.add(UsageDay(account_id=account_id, day=day, turns=1))
                return True, 1
            except IntegrityError:
                continue        # a peer made today's row: charge it instead
        raise RuntimeError("could not charge today's turn")

    @staticmethod
    def _turns_today(s, account_id: str, day: str) -> int:
        return int(s.query(func.coalesce(func.sum(UsageDay.turns), 0))
                   .filter(UsageDay.account_id == account_id,
                           UsageDay.day == day).scalar())

    def daily_turns_used(self, account_id: str) -> int:
        """Today's charged turns, read only. Admission asks this; the worker
        charges with claim_daily_turn where the model is called. A sum, so
        it reads every row even on a table not yet given the constraint."""
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.session() as s:
            return self._turns_today(s, account_id, day)

    _ACTIVITY_FIELDS = ("asked", "approved")

    def count_activity(self, account_id: str, field: str) -> None:
        """Add one to today's `asked` or `approved` for this account (beta
        spec 4.5). Increment first, insert if there was no row, and on a
        racing insert increment the row the other writer made."""
        if field not in self._ACTIVITY_FIELDS:
            raise ValueError(f"unknown activity field {field!r}")
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        col = getattr(ActivityDay, field)
        stmt = (update(ActivityDay)
                .where(ActivityDay.account_id == account_id,
                       ActivityDay.day == day)
                .values({field: col + 1}))
        with self.session() as s:
            if s.execute(stmt).rowcount:
                return
        try:
            with self.session() as s:
                s.add(ActivityDay(account_id=account_id, day=day,
                                  **{field: 1}))
        except IntegrityError:
            with self.session() as s:
                s.execute(stmt)

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
            # A grant outlives its connection as a record of what was shared
            # and whether it was taken back; detach it rather than lose it
            # (and rather than let the foreign key refuse the delete on
            # Postgres, which SQLite would never have shown).
            for g in s.query(Grant).filter_by(connection_id=conn_id).all():
                g.connection_id = None
            s.delete(c)
            return True

    def delete_account(self, account_id: str) -> bool:
        """Remove the account and every row keyed to it (#554).

        Called only AFTER every connection's tenant is confirmed purged at
        HealthClaw, for the same reason delete_connection is: a failed purge
        must never leave records in the engine behind a vanished account.
        Grants go too — with the account gone there is nobody they describe
        a consent for, and HealthClaw keeps the PHI-free audit trail of what
        was shared and revoked. Email codes for the address are removed so
        one in a mailbox cannot mint a session for an account that is gone.
        """
        with self.session() as s:
            acct = s.get(Account, account_id)
            if acct is None:
                return False
            for model in (Surface, Agent, Grant, Connection, Passkey,
                          UsageDay, ActivityDay):
                s.query(model).filter_by(account_id=account_id).delete()
            s.query(EmailToken).filter_by(email=acct.email).delete()
            # The invite is keyed by the same address (beta spec 4.2). The
            # operator can invite again if the person comes back.
            s.query(RealRecordInvite).filter_by(email=acct.email).delete()
            s.delete(acct)
            return True

    # --- grants: consents given to third-party agents (spec §13.4) ---------

    def add_grant(self, account_id: str, connection_id: str | None,
                  tenant_id: str, client_id: str, client_name: str,
                  scopes: str, consent_id: str,
                  redirect_host: str | None = None) -> str:
        with self.session() as s:
            g = Grant(account_id=account_id, connection_id=connection_id,
                      tenant_id=tenant_id, client_id=client_id[:64],
                      client_name=(client_name or "An agent")[:120],
                      redirect_host=redirect_host[:255] if redirect_host else None,
                      scopes=scopes[:255], consent_id=consent_id)
            s.add(g)
            s.flush()
            return g.id

    def list_grants(self, account_id: str) -> list[dict]:
        with self.session() as s:
            rows = (s.query(Grant).filter_by(account_id=account_id)
                    .order_by(Grant.granted_at.desc()).all())
            return [_grant_dict(g) for g in rows]

    def grants_for_connection(self, account_id: str, conn_id: str,
                              active_only: bool = True) -> list[dict]:
        with self.session() as s:
            q = s.query(Grant).filter_by(account_id=account_id, connection_id=conn_id)
            if active_only:
                q = q.filter(Grant.revoked_at.is_(None))
            return [_grant_dict(g) for g in q.all()]

    def get_grant(self, account_id: str, grant_id: str) -> dict | None:
        with self.session() as s:
            g = s.query(Grant).filter_by(id=grant_id, account_id=account_id).first()
            return _grant_dict(g) if g else None

    def mark_grant_revoked(self, account_id: str, grant_id: str) -> bool:
        with self.session() as s:
            g = s.query(Grant).filter_by(id=grant_id, account_id=account_id).first()
            if g is None:
                return False
            if g.revoked_at is None:
                g.revoked_at = now()
            return True

    def get_connection(self, account_id: str, conn_id: str) -> dict | None:
        """Fetch one connection scoped to its owner (never cross-account)."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            return _conn_dict(c) if c else None

    #: Connections a terms prompt skips: revoked ones are gone, and pending
    #: ones are still connecting, so they are asked once they settle.
    RECONSENT_SKIPS = ("revoked", "pending")

    def record_consent(self, account_id: str, conn_id: str,
                       version: str) -> bool:
        """Accept the current terms, starting from one of this account's real
        connections. One accept covers every real connection on the account
        that is neither revoked nor still connecting (beta spec 4.3). Scoped
        by the account: another account's connection is never touched.

        Stamps consent_version and reconsented_at. consented_at stays the
        first connect, so the weekly number does not move.
        """
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(id=conn_id, account_id=account_id).first())
            if c is None or c.kind == "sample" or c.status == "revoked":
                return False
            t = now()
            rows = (s.query(Connection)
                    .filter(Connection.account_id == account_id,
                            Connection.kind != "sample",
                            Connection.status.notin_(self.RECONSENT_SKIPS))
                    .all())
            for row in {r.id: r for r in [c, *rows]}.values():
                if row.consent_version != version:
                    row.consent_version = version
                    row.reconsented_at = t
            return True

    def stale_consent(self, connections: list[dict],
                      version: str) -> list[dict]:
        """The account's real connections a terms prompt should cover."""
        return [c for c in connections
                if c["kind"] != "sample"
                and c["status"] not in self.RECONSENT_SKIPS
                and c.get("consent_version") != version]

    def mark_synced(self, conn_id: str, count: int,
                    uncounted: int | None = None) -> dict:
        """Record the end of a sync and report what changed since the last one.

        Returns {"new": int, "total": int} where `new` is the growth since the
        previous sync (0 on the first one, since there's no baseline to compare
        against and every record is arguably 'new').

        `uncounted` baselines the types the count excludes (#226) on the same
        terms. Passing None leaves the stored baseline untouched rather than
        clearing it — a caller that could not measure documents must not erase
        the last figure that was measured.
        """
        with self.session() as s:
            c = s.query(Connection).filter_by(id=conn_id).first()
            if c is None:
                return {"new": 0, "total": count}
            previous = c.last_count
            c.last_count = count
            if uncounted is not None:
                c.last_uncounted = uncounted
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

    def ensure_first_agent(self, account_id: str,
                           connection_id: str) -> str | None:
        """The account's first assistant, created once (spec section 5).

        Returns the new agent id, or None when nothing was created. The
        connection must be the account's and active. `first_agent_at` is a
        compare-and-set on the account row, so a second callback, or one
        racing this one, updates zero rows. An account that already has
        agents is stamped and left alone.
        """
        with self.session() as s:
            conn = (s.query(Connection)
                    .filter_by(id=connection_id, account_id=account_id,
                               status="active").first())
            if conn is None:
                return None
            won = (s.query(Account)
                   .filter(Account.id == account_id,
                           Account.first_agent_at.is_(None))
                   .update({"first_agent_at": now()},
                           synchronize_session=False))
            if won != 1:
                return None
            if s.query(Agent).filter_by(account_id=account_id).first():
                return None
            a = Agent(account_id=account_id, connection_id=connection_id,
                      name="Juniper", persona="calm", advisor=None)
            s.add(a)
            s.flush()
            return a.id

    def activate_connection(self, tenant_id: str) -> list[str]:
        """Records landed on this tenant: mark it active, and give each
        owning account its first assistant if it has none. Safe to repeat."""
        self.set_connection_status(tenant_id, "active")
        with self.session() as s:
            owners = [(c.account_id, c.id) for c in
                      s.query(Connection).filter_by(tenant_id=tenant_id)]
        made = [self.ensure_first_agent(a, c) for a, c in owners]
        return [x for x in made if x]

    def agent_for_connection(self, account_id: str,
                             connection_id: str) -> dict | None:
        with self.session() as s:
            a = (s.query(Agent)
                 .filter_by(account_id=account_id, connection_id=connection_id)
                 .order_by(Agent.created_at.asc()).first())
            return _agent_dict(a) if a else None

    def rename_agent(self, account_id: str, agent_id: str, name: str) -> bool:
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                return False
            a.name = name[:48]
            return True

    def move_agent(self, account_id: str, agent_id: str,
                   connection_id: str) -> None:
        """Point an agent at another of the account's ACTIVE connections.

        Raises AuthError for a foreign agent, a foreign connection, or one
        that is not active: the ownership rule create_agent applies, plus
        the revoked check, since a revoked connection is not a pathway to
        a tenant (#215).
        """
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                raise AuthError("That assistant isn't yours.")
            c = (s.query(Connection)
                 .filter_by(id=connection_id, account_id=account_id,
                            status="active").first())
            if c is None:
                raise AuthError("Those records aren't available.")
            a.connection_id = c.id

    def delete_agent(self, account_id: str, agent_id: str) -> bool:
        """Remove the agent and its surfaces. Its conversation stays in
        HealthClaw until the connection is deleted (spec section 5)."""
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                return False
            s.query(Surface).filter_by(agent_id=agent_id).delete()
            s.delete(a)
            return True

    def switch_prompted_at(self, account_id: str) -> float | None:
        with self.session() as s:
            acct = s.get(Account, account_id)
            return acct.switch_prompted_at if acct else None

    def stamp_switch_prompt(self, account_id: str) -> None:
        with self.session() as s:
            acct = s.get(Account, account_id)
            if acct is not None and acct.switch_prompted_at is None:
                acct.switch_prompted_at = now()

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
            # The connection and the pause flag let the worker refuse a turn
            # before it reads anything (beta spec 4.3 and 4.6).
            acct = s.get(Account, a.account_id)
            return {"agent": _agent_dict(a), "tenant": conn.tenant_id,
                    "account_id": a.account_id,
                    "connection": _conn_dict(conn),
                    "paused": bool(acct and acct.real_paused_at is not None)}

    def add_surface(self, account_id: str, agent_id: str, kind: str,
                    handle: str | None, status: str = "pending") -> str:
        from careagents.imessage import CODE_TTL_SECONDS
        with self.session() as s:
            if not s.query(Agent).filter_by(
                    id=agent_id, account_id=account_id).first():
                raise AuthError("That agent isn't yours.")
            x = Surface(account_id=account_id, agent_id=agent_id, kind=kind,
                        handle=handle, status=status,
                        bound_at=now() if status == "active" else None,
                        code_exp=(now() + CODE_TTL_SECONDS
                                  if status == "pending" else None))
            s.add(x)
            s.flush()
            return x.id

    def find_surface_by_code(self, code: str,
                             kind: str = "telegram") -> dict | None:
        with self.session() as s:
            q = s.query(Surface).filter_by(handle=code, kind=kind,
                                           status="pending")
            if kind == "imessage":
                # Codes expire; a row from before the column reads as expired.
                q = q.filter(Surface.code_exp.isnot(None),
                             Surface.code_exp > now())
            x = q.first()
            return _surf_dict(x) | {"account_id": x.account_id} if x else None

    def find_surface_by_handle(self, handle: str, kind: str = "imessage",
                               also: str | None = None) -> dict | None:
        """Resolve an active surface by the bound external handle — used to
        route an inbound message to the right agent. `also` is the handle as
        it arrived, for a row bound before handles were normalized."""
        names = {h for h in (handle, also) if h}
        if not names:
            return None
        with self.session() as s:
            x = (s.query(Surface)
                 .filter(Surface.handle.in_(names), Surface.kind == kind,
                         Surface.status == "active")
                 .order_by(Surface.bound_at.desc())
                 .first())
            return _surf_dict(x) | {"account_id": x.account_id} if x else None

    def bind_surface(self, surface_id: str, handle: str) -> None:
        with self.session() as s:
            x = s.get(Surface, surface_id)
            if x:
                x.handle = handle
                x.status = "active"
                x.bound_at = now()

    # --- iMessage: one handle, one account ----------------------------------
    # Handles arrive normalized (careagents.imessage.normalize_handle).

    def bind_imessage_handle(self, account_id: str, handle: str,
                             pending_surface_id: str | None = None,
                             welcome: bool = False,
                             also: str | None = None) -> str:
        """Bind a handle to an account: "connected" or "taken".

        A handle is bound to at most one account. Bound elsewhere, nothing
        changes until that binding is undone (STOP, or disconnect on the
        web). Bound here already, the older binding is replaced, so a new
        pairing code moves the handle to that code's assistant. `also` is
        the handle as it arrived, for a row bound before normalization.

        The check below is a read; the partial unique index on active
        iMessage handles (models._ensure_imessage_handle_unique) is what
        holds when two binds race, and its refusal reads as "taken".
        """
        try:
            return self._bind_imessage_handle(
                account_id, handle, pending_surface_id, welcome, also)
        except IntegrityError:
            return "taken"

    def _bind_imessage_handle(self, account_id, handle, pending_surface_id,
                              welcome, also) -> str:
        names = {h for h in (handle, also) if h}
        with self.session() as s:
            bound = (s.query(Surface)
                     .filter(Surface.kind == "imessage",
                             Surface.handle.in_(names),
                             Surface.status == "active").all())
            if any(x.account_id != account_id for x in bound):
                return "taken"
            if pending_surface_id:
                x = s.get(Surface, pending_surface_id)
                if x is None or x.account_id != account_id:
                    return "taken"
            else:
                first = (s.query(Agent).filter_by(account_id=account_id)
                         .order_by(Agent.id).first())
                x = Surface(account_id=account_id, kind="imessage",
                            agent_id=first.id if first else None)
                s.add(x)
            for old in bound:
                s.delete(old)
            # Deleted before the new row takes the handle, or the unique
            # index sees both at once.
            s.flush()
            x.handle = handle
            x.status = "active"
            x.bound_at = now()
            x.code_exp = None
            x.welcome_due = 1 if welcome else 0
            st = self._handle_state(s, handle)
            st.opted_out_at = None
            return "connected"

    def imessage_agent_context(self, surface: dict) -> dict | None:
        """The agent a bound handle talks to. A handle bound by the sign-in
        link before the account had an assistant gets the account's first
        one, once it exists."""
        agent_id = surface.get("agent_id")
        if not agent_id:
            with self.session() as s:
                first = (s.query(Agent)
                         .filter_by(account_id=surface["account_id"])
                         .order_by(Agent.id).first())
                if first is None:
                    return None
                agent_id = first.id
                x = s.get(Surface, surface["id"])
                if x is not None:
                    x.agent_id = agent_id
            surface["agent_id"] = agent_id
        return self.get_agent_context(surface["account_id"], agent_id)

    def take_imessage_welcome(self, surface_id: str) -> bool:
        """True once, if this surface's next reply should open with the
        welcome. A conditional update, so two racing turns welcome once."""
        with self.session() as s:
            res = s.execute(update(Surface)
                            .where(Surface.id == surface_id,
                                   Surface.welcome_due == 1)
                            .values(welcome_due=0))
            return res.rowcount == 1

    def disconnect_imessage(self, account_id: str,
                            surface_id: str | None = None) -> int:
        """Remove one iMessage binding on the account (`surface_id`), or
        every binding and pending code. Each freed handle gets its link
        allowance back, so its next text is answered with a fresh link
        rather than silence."""
        with self.session() as s:
            q = s.query(Surface).filter_by(account_id=account_id,
                                           kind="imessage")
            if surface_id is not None:
                q = q.filter_by(id=surface_id, status="active")
            rows = q.all()
            for x in rows:
                if x.status == "active" and x.handle:
                    st = s.get(ImessageHandleState, _handle_key(x.handle))
                    if st is not None:
                        st.link_count, st.link_window_start = 0, None
                s.delete(x)
            return len(rows)

    def _handle_state(self, s, handle: str) -> ImessageHandleState:
        key = _handle_key(handle)
        st = s.get(ImessageHandleState, key)
        if st is None:
            st = ImessageHandleState(handle_key=key, fail_count=0,
                                     link_count=0)
            s.add(st)
        return st

    def imessage_stop(self, handle: str, also: str | None = None) -> bool:
        """Unbind the handle (and the raw spelling `also`, for a row bound
        before normalization), void its unused sign-in links, and remember
        the STOP. True if this is news: not already opted out."""
        names = list({h for h in (handle, also) if h})
        with self.session() as s:
            s.query(Surface).filter(
                Surface.kind == "imessage", Surface.handle.in_(names),
                Surface.status == "active").delete(synchronize_session=False)
            s.query(ImessageLink).filter(
                ImessageLink.handle.in_(names),
                ImessageLink.used_at.is_(None)).update(
                    {"used_at": now()}, synchronize_session=False)
            st = self._handle_state(s, handle)
            first = st.opted_out_at is None
            st.opted_out_at = st.opted_out_at or now()
            return first

    def imessage_opted_out(self, handle: str) -> bool:
        with self.session() as s:
            st = s.get(ImessageHandleState, _handle_key(handle))
            return bool(st and st.opted_out_at)

    def imessage_opt_in(self, handle: str) -> None:
        with self.session() as s:
            st = s.get(ImessageHandleState, _handle_key(handle))
            if st is not None:
                st.opted_out_at = None

    def imessage_bind_locked(self, handle: str) -> bool:
        from careagents.imessage import BIND_ATTEMPTS, WINDOW_SECONDS
        with self.session() as s:
            st = s.get(ImessageHandleState, _handle_key(handle))
            return bool(st and st.fail_window_start
                        and now() - st.fail_window_start < WINDOW_SECONDS
                        and (st.fail_count or 0) >= BIND_ATTEMPTS)

    def imessage_note_bind_failure(self, handle: str) -> None:
        from careagents.imessage import WINDOW_SECONDS
        with self.session() as s:
            st = self._handle_state(s, handle)
            if (not st.fail_window_start
                    or now() - st.fail_window_start >= WINDOW_SECONDS):
                st.fail_window_start, st.fail_count = now(), 0
            st.fail_count = (st.fail_count or 0) + 1

    def issue_imessage_link(self, handle: str) -> str | None:
        """Mint a one-time sign-in link for `handle`; the token is returned
        once and only its hash is kept. None past the window's allowance."""
        from careagents.imessage import (LINK_TTL_SECONDS, LINKS_PER_WINDOW,
                                         WINDOW_SECONDS, hash_token,
                                         new_link_token)
        with self.session() as s:
            st = self._handle_state(s, handle)
            if (not st.link_window_start
                    or now() - st.link_window_start >= WINDOW_SECONDS):
                st.link_window_start, st.link_count = now(), 0
            if (st.link_count or 0) >= LINKS_PER_WINDOW:
                return None
            st.link_count = (st.link_count or 0) + 1
            token = new_link_token()
            s.add(ImessageLink(token_hash=hash_token(token), handle=handle,
                               exp=now() + LINK_TTL_SECONDS))
            return token

    def peek_imessage_link(self, token: str) -> str | None:
        """The id of a live (unused, unexpired) link, without spending it."""
        from careagents.imessage import hash_token
        with self.session() as s:
            link = (s.query(ImessageLink)
                    .filter_by(token_hash=hash_token(token))
                    .filter(ImessageLink.used_at.is_(None),
                            ImessageLink.exp > now()).first())
            return link.id if link else None

    def void_imessage_link(self, link_id: str) -> None:
        """Spend a link without binding anything ("No, this isn't mine")."""
        with self.session() as s:
            s.execute(update(ImessageLink)
                      .where(ImessageLink.id == link_id,
                             ImessageLink.used_at.is_(None))
                      .values(used_at=now()))

    def imessage_link_handle(self, link_id: str) -> str | None:
        """The handle a live link would bind, for the confirm page."""
        with self.session() as s:
            link = s.get(ImessageLink, link_id)
            if link is None or link.used_at is not None or link.exp <= now():
                return None
            return link.handle

    def claim_imessage_link(self, link_id: str, account_id: str) -> str:
        """Spend a link for a signed-in account and bind its handle:
        "connected", "taken" or "expired". Spent exactly once."""
        with self.session() as s:
            res = s.execute(update(ImessageLink)
                            .where(ImessageLink.id == link_id,
                                   ImessageLink.used_at.is_(None),
                                   ImessageLink.exp > now())
                            .values(used_at=now()))
            if res.rowcount != 1:
                return "expired"
            handle = s.get(ImessageLink, link_id).handle
        return self.bind_imessage_handle(account_id, handle, welcome=True)


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
            "last_synced_at": c.last_synced_at, "last_count": c.last_count,
            "last_uncounted": c.last_uncounted,
            "consent_version": c.consent_version}


def _grant_dict(g: Grant) -> dict:
    return {"id": g.id, "connection_id": g.connection_id,
            "tenant_id": g.tenant_id, "client_id": g.client_id,
            "client_name": g.client_name, "redirect_host": g.redirect_host,
            "scopes": g.scopes,
            "consent_id": g.consent_id, "granted_at": g.granted_at,
            "revoked_at": g.revoked_at,
            "status": "revoked" if g.revoked_at else "active"}


def _agent_dict(a: Agent) -> dict:
    return {"id": a.id, "name": a.name, "persona": a.persona,
            "advisor": a.advisor, "connection_id": a.connection_id}


def _surf_dict(x: Surface) -> dict:
    return {"id": x.id, "kind": x.kind, "handle": x.handle,
            "status": x.status, "agent_id": x.agent_id}


def _opts_to_dict(options_json: str) -> dict:
    import json
    return json.loads(options_json)


def secret_matches(provided: str, expected: str) -> bool:
    """A shared secret from a request header, compared in constant time.

    Both sides are hashed here first, so `compare_digest` only ever meets
    two hexdigests this process computed: no caller spelling (non-ASCII, a
    lone surrogate) can raise inside it (#557). An empty expected secret
    matches nothing.
    """
    if not expected:
        return False

    def digest(value: str) -> str:
        return hashlib.sha256(
            str(value).encode("utf-8", "surrogatepass")).hexdigest()
    return hmac.compare_digest(digest(provided), digest(expected))


def _handle_key(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def new_binding_code() -> str:
    return base64.b32encode(secrets.token_bytes(6)).decode().rstrip("=").lower()
