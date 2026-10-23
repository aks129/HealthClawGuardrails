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
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from careagents import mail
# Its table goes in Base.metadata before any engine is built, as below.
from careagents import checkins
# Also puts the beta, invite and funnel tables in Base.metadata before any
# engine is built.
from careagents import beta_signup, funnel, invites, signins
from careagents.models import (NICKNAME_MAX, RELATIONSHIPS, Account,
                               ActivityDay, Agent, Connection, EmailToken,
                               Grant, ImessageHandleState, ImessageLink,
                               Passkey, Person, RealRecordInvite, RefundDay,
                               SendblueMessage, Surface, UsageDay,
                               VoiceUsageDay, make_engine,
                               make_session_factory, now)
from careagents.models import REFUNDS_PER_DAY

logger = logging.getLogger(__name__)


class _NothingToRefund(Exception):
    """No charged turn to give back: rolls the refund's transaction back."""

CODE_TTL = 600  # 10 minutes
MAX_CODE_ATTEMPTS = 5   # burn a login code after this many wrong guesses
RESEND_COOLDOWN = 30    # seconds — don't mint a fresh code (or reset attempts)
                        # while a recent one is still in flight
CODE_MAX = 100_000_000  # 8-digit codes (~26.6 bits)
SAMPLE_LEASE_SECONDS = 60  # a sample seed that has not finished by then died
# Per-address caps on sign-in codes, as rolling windows over when each code
# was issued. Every code counts, burned and used ones included: a burned code
# is one whose guesses were spent, so leaving it out would hand back a fresh
# set of guesses at once.
CODES_PER_HOUR = 10
CODES_PER_DAY = 20
WRONG_GUESSES_PER_DAY = 25  # across all of an address's codes
HOUR = 3600
DAY = 86_400
#: Postgres advisory-lock class for one address's codes; the second key is
#: derived from the address (_lock_codes). Distinct from _INVITE_LOCK_KEY.
_CODES_LOCK_KEY = 9_280_002
#: People someone can add to care for. Each mints and seeds a sample tenant,
#: so the number is bounded.
MAX_OTHER_PEOPLE = 4


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

    def _lock_codes(self, s, email: str) -> None:
        """Make reading an address's codes and writing after them one step.

        Racing requests otherwise all read the same count and all pass a cap.
        Postgres takes an advisory lock on the address's inbox (mailbox) for
        the transaction, so variants of one inbox wait for each other. SQLite
        takes its write lock when a write statement starts, so a write that
        changes nothing goes first and a racing request waits for the commit
        before it reads."""
        if self.engine.dialect.name == "postgresql":
            key = beta_signup.mailbox(email)
            k = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4],
                               "big", signed=True)
            # The two-key form takes two 32-bit integers.
            s.execute(text("SELECT pg_advisory_xact_lock("
                           "CAST(:c AS integer), CAST(:k AS integer))"),
                      {"c": _CODES_LOCK_KEY, "k": k})
        else:
            s.execute(text("UPDATE ca_email_tokens SET used = used "
                           "WHERE 1 = 0"))

    @staticmethod
    def _same_inbox(email: str):
        """Every code for the inbox the address delivers to: one Gmail inbox
        is reached by plus-tags, dots and googlemail, and they share its
        caps. The typed address also matches codes issued before `mailbox`
        was recorded."""
        return or_(EmailToken.mailbox == beta_signup.mailbox(email),
                   EmailToken.email == email)

    @classmethod
    def _issued_within(cls, s, email: str, window: int) -> list:
        """Every code issued for the address's inbox in the last `window`
        seconds, oldest first. exp is issue time + CODE_TTL; there is no
        issue column."""
        return (s.query(EmailToken)
                .filter(cls._same_inbox(email))
                .filter(EmailToken.exp >= now() - window + CODE_TTL)
                .order_by(EmailToken.exp.asc()).all())

    @staticmethod
    def _code_wait(day: list) -> float:
        """Seconds until the address may be sent another code; <= 0 is now.

        `day` is _issued_within(DAY). A cap of N over a window frees up when
        the code that brought the count to N leaves that window."""
        t = now()
        issued = [tok.exp - CODE_TTL for tok in day]
        waits = [0.0]
        if issued:
            waits.append(issued[-1] + RESEND_COOLDOWN - t)
        hour = [at for at in issued if at >= t - HOUR]
        if len(hour) >= CODES_PER_HOUR:
            waits.append(hour[len(hour) - CODES_PER_HOUR] + HOUR - t)
        if len(issued) >= CODES_PER_DAY:
            waits.append(issued[len(issued) - CODES_PER_DAY] + DAY - t)
        guesses = sum(tok.attempts or 0 for tok in day)
        for tok, at in zip(day, issued):
            if guesses < WRONG_GUESSES_PER_DAY:
                break
            # This code's wrong guesses stop counting when it leaves the
            # window.
            guesses -= tok.attempts or 0
            waits.append(at + DAY - t)
        return max(waits)

    def start_email_code(self, email: str, purpose: str = "verify") -> int:
        """Mint and send a one-time code.

        Returns 0 when a code was actually sent, otherwise the whole seconds
        until another may be requested: the resend wait, or a per-address cap
        on codes or on wrong guesses. A wait is not a failure, but it is not a
        send either, and the caller must not report one it never observed
        (#262). The returned wait turns on ca_email_tokens alone, so the
        answer is the same whether or not the address has an account.

        ca_accounts is read only to choose the email's wording: "login" for
        an address that has an account, `purpose` (a new account) otherwise.
        The mail goes to the inbox alone, whose owner already knows, and the
        account's existence never reaches the HTTP answer, which stays
        byte-identical (tests/test_careagents_walk_10_20_security.py).
        """
        email = email.strip().lower()
        if "@" not in email or len(email) > 255:
            raise AuthError("Enter a valid email address.")
        code = None
        with self.session() as s:
            self._lock_codes(s, email)
            # Any code issued in the window holds the next one back: unused,
            # used or burned. Counting only unused codes let a burned code be
            # replaced at once, which reset the per-code attempt count.
            wait = self._code_wait(self._issued_within(s, email, DAY))
            if wait > 0:
                # Round up: never tell someone to retry a moment early.
                return max(1, math.ceil(wait))
            # One live code per inbox: retire any prior unused codes, the
            # inbox's other variants' included, so an attacker can't
            # accumulate many simultaneously-valid guesses.
            (s.query(EmailToken)
             .filter(self._same_inbox(email), EmailToken.used.is_(False))
             .update({"used": True}, synchronize_session=False))
            code = f"{secrets.randbelow(CODE_MAX):08d}"
            if s.query(Account.id).filter_by(email=email).first():
                purpose = "login"
            # Sent to the address as typed; counted against its inbox.
            tok = EmailToken(email=email, code_hash=self._hash(code),
                             purpose=purpose, exp=now() + CODE_TTL,
                             mailbox=beta_signup.mailbox(email))
            s.add(tok)
            s.flush()
            token_id = tok.id
        # Three outcomes, and each one leaves the just-minted code in a
        # different place. Compared by identity against the named states, never
        # truthiness-tested — every state is a truthy string precisely so that
        # a `if not send_code(...)` cannot be written here again.
        outcome = mail.send_code(self.cfg, email, code, purpose)
        if outcome == mail.NOT_SENT:
            # Remove the code we just minted. It was never delivered, and
            # leaving it would make the resend wait above swallow the person's
            # retry — reporting "sent" without sending anything. Deleted, not
            # burned: a burned code counts toward the wait and the caps, and
            # this one never left.
            with self.session() as s:
                s.query(EmailToken).filter_by(id=token_id).delete()
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
        # SENT: no wait to report. The early return above is the only path
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
        made = False
        with self.session() as s:
            # Parallel guesses must not each read the same attempt count.
            self._lock_codes(s, email)
            # Fetch the single live code by email (not by hash) so a wrong
            # guess is counted against it and the code can be burned.
            tok = (s.query(EmailToken)
                   .filter_by(email=email, used=False)
                   .filter(EmailToken.exp >= time.time())
                   .order_by(EmailToken.exp.desc()).first())
            if tok is None:
                error = "That code is wrong or expired."
            elif (sum(t.attempts or 0
                      for t in self._issued_within(s, email, DAY))
                  >= WRONG_GUESSES_PER_DAY):
                # The day's wrong guesses, across every code, are spent: no
                # code is accepted, the right one included, until the oldest
                # of them leaves the window.
                tok.used = True
                error = "Too many attempts — request a new code."
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
                    s.flush()
                    # Every account has its self person from the start.
                    s.add(Person(account_id=acct.id, kind="self"))
                    made = True
                elif acct.email_verified_at is None:
                    acct.email_verified_at = now()
                acct.last_login_at = now()
                acct.last_login_method = "email_code"
                s.flush()
                result = _detach(acct)
                # Said after the code, never before it: the code request's
                # answer must not tell an account apart (10-20 walk).
                result.new_account = made
        if error:
            raise AuthError(error)
        if made:
            funnel.safely(funnel.new_account, self.session, result.id)
        funnel.safely(signins.count, self.session, result.id, "email_code")
        return result

    def has_connections(self, account_id: str) -> bool:
        """Has this account ever connected anything, sample included?"""
        with self.session() as s:
            return s.query(Connection).filter_by(
                account_id=account_id).first() is not None

    def has_real_connection(self, account_id: str) -> bool:
        """Any connection that is not the sample, in any state."""
        with self.session() as s:
            return s.query(Connection).filter(
                Connection.account_id == account_id,
                Connection.kind != "sample").first() is not None

    def first_run_account(self, email: str,
                          sign_in: bool = True) -> Account | None:
        """The account a confirmed beta request signs in to, or None.

        The confirm link proves the mailbox, as an email code does, so the
        address gets the account a code would give it: made now if there
        is none, verified, its sign-in stamped. None when the account
        already has a connection: a forwarded link must never open records
        that are already there, so that account signs in the usual way.

        `sign_in=False` when this browser is already signed in as the
        address (app.py `_first_run` keeps that session): no sign-in
        happened, so none is stamped or counted."""
        email = email.strip().lower()
        with self.session() as s:
            acct = s.query(Account).filter_by(email=email).first()
            if acct is not None and s.query(Connection).filter_by(
                    account_id=acct.id).first() is not None:
                return None
            made = acct is None
            if acct is None:
                acct = Account(email=email, email_verified_at=now())
                s.add(acct)
                s.flush()
                s.add(Person(account_id=acct.id, kind="self"))
            elif acct.email_verified_at is None:
                acct.email_verified_at = now()
            if sign_in or made:
                acct.last_login_at = now()
                acct.last_login_method = "signup_confirm"
            s.flush()
            result = _detach(acct)
        if made:
            funnel.safely(funnel.new_account, self.session, result.id)
        if sign_in or made:
            funnel.safely(signins.count, self.session, result.id,
                          "signup_confirm")
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
                              require_uv: bool = False,
                              method: str | None = None) -> Account:
        """Verify a passkey assertion. `method` is the sign-in it is
        counted as (careagents/signins.py), passed by the sign-in route.
        None, the default, counts nothing: the consent screen's check makes
        no session."""
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
            if method:
                acct.last_login_at = now()
                acct.last_login_method = method
            s.flush()
            result = _detach(acct)
        if method:
            funnel.safely(signins.count, self.session, result.id, method)
        return result

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
    def _invite_email(email: str) -> tuple[str, str]:
        """(the inbox an invite is keyed by, the address as typed). See
        `invites.invite_key`."""
        email = (email or "").strip().lower()
        if "@" not in email or len(email) > 255:
            raise AuthError("Enter a valid email address.")
        key = invites.invite_key(email)
        if key.startswith("@") or len(key) > 255:
            raise AuthError("Enter a valid email address.")
        return key, email

    #: Postgres advisory-lock key that serialises stage 1 invite writes, so
    #: the count of live invites and the write that depends on it are one
    #: step. Distinct from the migration keys in models.py.
    _INVITE_LOCK_KEY = 9_280_001

    def _write_invite(self, email: str, display: str, invited_by: str,
                      reopen: bool) -> str:
        """Create (or, with `reopen`, reopen) an invite only while fewer than
        STAGE1_INVITE_CAP are live, as one atomic step (#928 F1). Returns
        "created", "reopened", "live" (already live, unchanged), "revoked"
        (revoked and left so), "full", or "alias-live" / "alias-revoked":
        the inbox (`invites.inbox_key`) already holds an invite under
        another address, and nothing is written (#993 F1).

        The cap sits inside the writing statement. SQLite takes its write
        lock when that statement starts, so the count it reads cannot move
        under it; Postgres first takes an advisory lock for the
        transaction, and each statement after it reads committed rows.
        This is the path for the operator's command and for a link join."""
        from careagents.beta import STAGE1_INVITE_CAP
        live = ("(SELECT COUNT(*) FROM ca_real_record_invites "
                "WHERE revoked_at IS NULL)")
        args = {"e": email, "d": display, "t": now(), "b": invited_by,
                "cap": STAGE1_INVITE_CAP}
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                             {"k": self._INVITE_LOCK_KEY})
            else:
                # SQLite: take the write lock before reading the rows.
                conn.execute(text("UPDATE ca_real_record_invites "
                                  "SET email = email WHERE 1 = 0"))
            alias = invites.alias_invite(conn.execute(text(
                "SELECT email, revoked_at FROM ca_real_record_invites")),
                display)
            if alias is not None:
                return f"alias-{alias}"
            if conn.execute(text(
                    "INSERT INTO ca_real_record_invites "
                    "(email, display_email, invited_at, invited_by) "
                    "SELECT :e, :d, :t, :b WHERE NOT EXISTS ("
                    "SELECT 1 FROM ca_real_record_invites WHERE email = :e) "
                    f"AND {live} < :cap"), args).rowcount:
                return "created"
            if reopen and conn.execute(text(
                    "UPDATE ca_real_record_invites SET revoked_at = NULL, "
                    "invited_at = :t, invited_by = :b, display_email = :d "
                    "WHERE email = :e AND revoked_at IS NOT NULL "
                    f"AND {live} < :cap"), args).rowcount:
                return "reopened"
            row = conn.execute(text(
                "SELECT revoked_at FROM ca_real_record_invites "
                "WHERE email = :e"), {"e": email}).first()
        if row is not None and row[0] is None:
            return "live"
        if row is not None and not reopen:
            return "revoked"
        return "full"

    def invite_real_records(self, email: str, invited_by: str) -> bool:
        """Invite an email to connect real records (the operator's command).
        Inviting a revoked email again reopens it. Returns False when it was
        already invited.

        Stage 1 holds at most STAGE1_INVITE_CAP active invites; a new or
        reopened one past that raises ValueError. Atomic: racing callers
        cannot pass the cap (#928 F1).
        """
        from careagents.beta import STAGE1_INVITE_CAP
        key, display = self._invite_email(email)
        invited_by = (invited_by or "").strip()[:255]
        if not invited_by:
            raise ValueError("invited_by is required")
        outcome = self._write_invite(key, display, invited_by, reopen=True)
        if outcome.startswith("alias-"):
            raise ValueError(
                "Another address for the same inbox already holds an invite "
                f"({outcome[6:]}). Use that address, or revoke it with "
                "`flask invites revoke`.")
        if outcome == "full":
            raise ValueError(
                f"Stage 1 is full: {STAGE1_INVITE_CAP} active invites. "
                "Revoke one first.")
        return outcome != "live"

    def grant_join_invite(self, email: str, invited_by: str) -> str:
        """A fresh invite for someone who joined through a link, or nothing
        (#928 F2): an invite that exists in any state, live or revoked by
        the operator, is left exactly as it is, `invited_by` included.
        Returns "created", "live", "revoked" or "full"; an invite on the
        same inbox under another address counts as "live" or "revoked"."""
        key, display = self._invite_email(email)
        outcome = self._write_invite(key, display, (invited_by or "")[:255],
                                     reopen=False)
        return outcome[6:] if outcome.startswith("alias-") else outcome

    def revoke_real_records_invite(self, email: str) -> bool:
        """Revoke an invite. New real connections are refused from now on;
        existing ones are left alone. Returns False when there was no live
        invite to revoke. Any alias revokes every live invite on the inbox,
        counted wide (`invites.inbox_key`, #993 F1)."""
        self._invite_email(email)
        inbox = invites.inbox_key(email)
        revoked = False
        with self.session() as s:
            for row in s.query(RealRecordInvite).filter(
                    RealRecordInvite.revoked_at.is_(None)):
                if invites.inbox_key(row.email) == inbox:
                    row.revoked_at = now()
                    revoked = True
        return revoked

    def real_records_invited(self, email) -> bool:
        """Does this email hold a live invite? Granted on the narrow key
        (`invites.invite_key`), and refused when any invite on the wider
        inbox (`invites.inbox_key`) is revoked (#993 F1). No address, no
        invite."""
        key = invites.invite_key(email)
        if not key or (email or "").strip().endswith("."):
            # A trailing-dot domain is folded for storing and revoking an
            # invite, never to grant one: the gate fails closed (#993 F2).
            return False
        with self.session() as s:
            row = s.get(RealRecordInvite, key)
            if row is None or row.revoked_at is not None:
                return False
            rows = s.query(RealRecordInvite.email,
                           RealRecordInvite.revoked_at).all()
        return invites.alias_invite(rows, email) != "revoked"

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
            people = (s.query(Person).filter_by(account_id=account_id)
                      .order_by(Person.created_at.asc()).all())
            return {
                "connections": [_conn_dict(c) for c in conns],
                "agents": [_agent_dict(a) for a in agents],
                "surfaces": [_surf_dict(x) for x in surfaces],
                "people": [_person_dict(p) for p in people],
            }

    # --- people (one assistant per person spec, section 2) ------------------

    @staticmethod
    def _self_person_id(s, account_id: str) -> str:
        """The account's self person, made if an older path left it out.

        Sign-up and the boot migration both make it; this covers a row
        written by a process still on the code from before. A racing peer
        that makes it first trips the one-self index, and its row is used.
        """
        found = (s.query(Person.id)
                 .filter_by(account_id=account_id, kind="self").scalar())
        if found:
            return found
        try:
            with s.begin_nested():
                p = Person(account_id=account_id, kind="self")
                s.add(p)
                s.flush()
                return p.id
        except IntegrityError:
            return (s.query(Person.id)
                    .filter_by(account_id=account_id, kind="self").scalar())

    def add_person(self, account_id: str, relation: str,
                   nickname: str | None = None) -> str:
        """Someone the account holder cares for: a relationship and an
        optional short nickname, nothing else (D4). No authority is stamped:
        their records are made-up until owner decision D1.

        Raises ValueError for anything outside that shape, and AuthError
        past MAX_OTHER_PEOPLE.
        """
        if relation not in RELATIONSHIPS:
            raise ValueError("relationship")
        if nickname is not None:
            if not isinstance(nickname, str):
                raise ValueError("nickname")
            nickname = nickname.strip() or None
        if nickname is not None and (len(nickname) > NICKNAME_MAX
                                     or not nickname.isprintable()):
            raise ValueError("nickname")
        with self.session() as s:
            others = (s.query(Person)
                      .filter_by(account_id=account_id, kind="other").count())
            if others >= MAX_OTHER_PEOPLE:
                raise AuthError("That's as many people as one account can "
                                "add for now.")
            p = Person(account_id=account_id, kind="other", relation=relation,
                       nickname=nickname)
            s.add(p)
            s.flush()
            return p.id

    def remove_empty_person(self, account_id: str, person_id: str) -> None:
        """Undo an add whose records never arrived. Never the self person,
        and never one anything points at."""
        with self.session() as s:
            self._drop_empty_other(s, account_id, person_id)

    def agent_for_person_of(self, account_id: str,
                            connection_id: str) -> str | None:
        """The assistant the person behind this connection already has, or
        None (also for a connection that is not the account's)."""
        with self.session() as s:
            conn = s.query(Connection).filter_by(
                id=connection_id, account_id=account_id).first()
            if conn is None or conn.person_id is None:
                return None
            a = (s.query(Agent)
                 .filter_by(account_id=account_id, person_id=conn.person_id)
                 .order_by(Agent.created_at.asc()).first())
            return a.id if a else None

    @staticmethod
    def _drop_empty_other(s, account_id: str, person_id: str | None) -> None:
        """Remove someone the holder cares for once nothing points at
        them. Never the self person."""
        if not person_id:
            return
        p = (s.query(Person)
             .filter_by(id=person_id, account_id=account_id, kind="other")
             .first())
        if p is None:
            return
        if (s.query(Connection).filter_by(person_id=person_id).first()
                or s.query(Agent).filter_by(person_id=person_id).first()):
            return
        s.delete(p)

    def other_person_connection_ids(self, account_id: str) -> set[str]:
        """Connections that hold someone else's records, left out of what
        a third-party app can be granted in the beta (spec 6.3)."""
        with self.session() as s:
            return {cid for (cid,) in
                    s.query(Connection.id)
                    .join(Person, Person.id == Connection.person_id)
                    .filter(Connection.account_id == account_id,
                            Person.kind != "self")}

    def assistant_counts(self) -> dict:
        """How many accounts hold no, one, or several assistants. Counts
        only, for the operator (spec section 4)."""
        with self.session() as s:
            per = (s.query(Agent.account_id, func.count(Agent.id))
                   .group_by(Agent.account_id).all())
            total = s.query(func.count(Account.id)).scalar() or 0
        one = sum(1 for _, n in per if n == 1)
        several = sum(1 for _, n in per if n > 1)
        return {"accounts": total, "none": total - one - several,
                "one": one, "several": several}

    def add_connection(self, account_id: str, kind: str, tenant_id: str,
                       label: str, status: str = "active",
                       provider: str | None = None,
                       consent_version: str | None = None,
                       person_id: str | None = None) -> str:
        """Record a connection for one person: the account holder unless
        `person_id` names someone else on this account."""
        with self.session() as s:
            if person_id is None:
                person_id = self._self_person_id(s, account_id)
            elif not s.query(Person).filter_by(
                    id=person_id, account_id=account_id).first():
                raise AuthError("That person isn't on your account.")
            c = Connection(account_id=account_id, kind=kind,
                           tenant_id=tenant_id, label=label[:120],
                           status=status, provider=provider,
                           consented_at=now() if consent_version else None,
                           consent_version=consent_version,
                           person_id=person_id)
            s.add(c)
            s.flush()
            return c.id

    def active_sample(self, account_id: str) -> dict | None:
        """The account's oldest active sample, if any. Older accounts can
        hold several; none is merged or removed (calm hub spec section 5).
        The holder's own: someone they care for has a sample of their own."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(account_id=account_id, kind="sample",
                            status="active",
                            person_id=self._self_person_id(s, account_id))
                 .order_by(Connection.connected_at.asc()).first())
            return _conn_dict(c) if c else None

    def pending_connection(self, account_id: str, kind: str,
                           status: str = "pending") -> dict | None:
        """The account's oldest connection of this kind still waiting for
        records, if any. `direct` waits as "empty" rather than "pending".
        The holder's own."""
        with self.session() as s:
            c = (s.query(Connection)
                 .filter_by(account_id=account_id, kind=kind,
                            status=status,
                            person_id=self._self_person_id(s, account_id))
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

    def claim_daily_turn(self, account_id: str, cap: int,
                         day: str | None = None) -> tuple[bool, int]:
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

        `day` is today's UTC day by default. The worker passes the day it
        records on the run, so a refund gives back that day's turn and
        never a later day's (refund_daily_turn).
        """
        from datetime import datetime, timezone
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    def refund_daily_turn(self, account_id: str, day: str) -> bool:
        """Give back one turn charged on `day`, for a turn that failed on
        our side. Returns whether a turn was given back.

        At most REFUNDS_PER_DAY a day: a failure still cost a model call.
        One transaction takes one of the day's refunds (a conditional
        UPDATE under the limit, or the day's first row) and takes one turn
        off (a conditional UPDATE above zero). If either finds nothing, the
        whole of it rolls back, so a refund never makes a negative count,
        never passes the limit, and never spends a refund it did not give.
        A racing first row is retried, as claim_daily_turn does.
        Exactly-once per run is the caller's (worker.py marks the run
        before it refunds)."""
        refunds = func.coalesce(RefundDay.count, 0)
        take = (update(RefundDay)
                .where(RefundDay.account_id == account_id,
                       RefundDay.day == day, refunds < REFUNDS_PER_DAY)
                .values(count=refunds + 1)
                .execution_options(synchronize_session=False))
        for _ in range(3):
            try:
                with self.session() as s:
                    if not s.execute(take).rowcount:
                        if s.query(RefundDay.id).filter_by(
                                account_id=account_id, day=day).first():
                            return False            # the day's refunds spent
                        s.add(RefundDay(account_id=account_id, day=day,
                                        count=1))
                        s.flush()
                    row = (s.query(UsageDay.id)
                           .filter(UsageDay.account_id == account_id,
                                   UsageDay.day == day, UsageDay.turns > 0)
                           .first())
                    if row is None or not s.execute(
                            update(UsageDay)
                            .where(UsageDay.id == row.id, UsageDay.turns > 0)
                            .values(turns=UsageDay.turns - 1)
                            .execution_options(
                                synchronize_session=False)).rowcount:
                        raise _NothingToRefund
                    return True
            except _NothingToRefund:
                return False
            except IntegrityError:
                continue        # a peer made the day's refund row first
        return False

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
        """Move every live row on the tenant. A revoked row is never moved:
        only the person disconnects, and records landing (a poll, an upload,
        the chat page settling) must not undo it (security review of #904,
        F3). Revoking goes through revoke_connection, one row at a time."""
        with self.session() as s:
            for c in (s.query(Connection)
                      .filter(Connection.tenant_id == tenant_id,
                              Connection.status != "revoked").all()):
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
            people = {c.person_id}
            for agent in s.query(Agent).filter_by(connection_id=conn_id).all():
                _drop_surfaces(s, s.query(Surface).filter_by(
                    agent_id=agent.id).all())
                people.add(agent.person_id)
                s.delete(agent)
            # A grant outlives its connection as a record of what was shared
            # and whether it was taken back; detach it rather than lose it
            # (and rather than let the foreign key refuse the delete on
            # Postgres, which SQLite would never have shown).
            for g in s.query(Grant).filter_by(connection_id=conn_id).all():
                g.connection_id = None
            s.delete(c)
            s.flush()
            for person_id in people:
                self._release_person_agent(s, person_id)
                self._drop_empty_other(s, account_id, person_id)
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
            # Sign-in links texted to the account's messaging numbers carry
            # the number itself, so they go too. The per-number STOP flag
            # and counters stay: keyed by a hash, not the number, and a STOP
            # must outlive the account that sent it.
            _drop_surfaces(s, s.query(Surface).filter_by(
                account_id=account_id).all())
            # People last: connections and agents point at them. Every
            # table with a foreign key to ca_accounts is listed here
            # (tests/test_careagents_account_delete_voice.py enumerates them).
            for model in (Surface, Agent, Grant, Connection, Passkey,
                          UsageDay, RefundDay, ActivityDay, VoiceUsageDay,
                          signins.SignInDay, Person):
                s.query(model).filter_by(account_id=account_id).delete()
            # Its invite link (a foreign key here) and funnel row go too.
            invites.delete_for_account(s, acct)
            funnel.delete_for_account(s, account_id)
            checkins.delete_for_account(s, account_id)
            s.query(EmailToken).filter_by(email=acct.email).delete()
            # The invite is keyed by the same inbox (beta spec 4.2). The
            # operator can invite again if the person comes back.
            s.query(RealRecordInvite).filter_by(
                email=invites.invite_key(acct.email)).delete()
            # So is a beta request (beta onboarding brief section 5),
            # matched by mailbox since a request keeps its domain in ASCII;
            # its iMessage spot goes to the oldest waitlisted request.
            beta_signup.delete_for_account(s, acct.email)
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

    @staticmethod
    def _claim_person_agent(s, person_id: str) -> bool:
        """Win the right to give this person their assistant: a
        compare-and-set on `agent_at`, as ensure_first_agent's is on the
        account. A second request, or one racing this one, updates zero
        rows. The one place one-assistant-per-person is decided."""
        return (s.query(Person)
                .filter(Person.id == person_id, Person.agent_at.is_(None))
                .update({"agent_at": now()},
                        synchronize_session=False)) == 1

    @staticmethod
    def _release_person_agent(s, person_id: str | None) -> None:
        """The person's last assistant is gone: they may have one again."""
        if person_id and not s.query(Agent).filter_by(
                person_id=person_id).first():
            (s.query(Person).filter(Person.id == person_id)
             .update({"agent_at": None}, synchronize_session=False))

    def create_agent(self, account_id: str, name: str, persona: str,
                     connection_id: str, advisor: str | None = None) -> str:
        """The assistant for the person whose records this connection
        holds: made if they have none, else the one they have (one
        assistant per person spec, section 3). Never a second one."""
        with self.session() as s:
            conn = s.query(Connection).filter_by(
                id=connection_id, account_id=account_id).first()
            if not conn:
                raise AuthError("That connection isn't yours.")
            person_id = conn.person_id or self._self_person_id(s, account_id)
            if not self._claim_person_agent(s, person_id):
                have = (s.query(Agent)
                        .filter_by(account_id=account_id, person_id=person_id)
                        .order_by(Agent.created_at.asc()).first())
                if have is None:
                    raise AuthError("That person already has an assistant.")
                return have.id
            a = Agent(account_id=account_id, name=name[:48], persona=persona,
                      connection_id=connection_id, advisor=advisor,
                      routines="", person_id=person_id)
            s.add(a)
            s.flush()
            return a.id

    def set_agent_settings(self, account_id: str, agent_id: str,
                           persona: str, routines: list[str]) -> bool:
        """Tone and routines, already checked against the fixed sets by
        the caller. Neither changes what the assistant can read."""
        with self.session() as s:
            a = s.query(Agent).filter_by(id=agent_id,
                                         account_id=account_id).first()
            if a is None:
                return False
            a.persona = persona
            a.routines = ",".join(routines)
            return True

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
            # The holder's own records only. Someone they care for is given
            # an assistant when they are added, never by a callback.
            me = self._self_person_id(s, account_id)
            if (conn.person_id or me) != me:
                return None
            won = (s.query(Account)
                   .filter(Account.id == account_id,
                           Account.first_agent_at.is_(None))
                   .update({"first_agent_at": now()},
                           synchronize_session=False))
            if won != 1:
                return None
            if not self._claim_person_agent(s, me):
                return None
            a = Agent(account_id=account_id, connection_id=connection_id,
                      name="Juniper", persona="calm", advisor=None,
                      routines="", person_id=me)
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
            # An assistant reads its own person's records, never someone
            # else's: "Your assistant" must not answer about Mom (spec 9).
            if c.person_id != a.person_id:
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
            _drop_surfaces(s, s.query(Surface).filter_by(
                agent_id=agent_id).all())
            s.delete(a)
            s.flush()
            self._release_person_agent(s, a.person_id)
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
            # Whose records, for the prompt and the sample line (FHI-44):
            # the kind and relationship only. The nickname is the holder's
            # typed text and stays out of the worker altogether.
            p = (s.query(Person).filter_by(id=a.person_id,
                                            account_id=a.account_id).first()
                 if a.person_id else None)
            return {"agent": _agent_dict(a), "tenant": conn.tenant_id,
                    "account_id": a.account_id,
                    # For flags kept by account email (CARE_ACTIONS_SMS).
                    "email": acct.email if acct else "",
                    "connection": _conn_dict(conn),
                    "person": ({"kind": p.kind, "relationship": p.relation}
                               if p else {"kind": "self"}),
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
            return _surf_dict(x) | _reverify_fields(x) if x else None

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
                             also: str | None = None,
                             issued_at: float | None = None) -> str:
        """Bind a handle to an account: "connected", "confirmed", "taken"
        or "expired". "confirmed" is the sign-in link re-confirming a handle
        already bound here (#871): nothing new was connected.

        A STOP always wins (#1005). `issued_at` is when the sign-in link
        being claimed was minted: an opt-out at or after it refuses the
        bind as "expired" and stays. The handle's state row is locked
        first, here and in imessage_stop, so a STOP racing the bind on
        Postgres either lands before it and is seen, or waits and then
        unbinds what it bound.

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
                account_id, handle, pending_surface_id, welcome, also,
                issued_at)
        except IntegrityError:
            return "taken"
        except (StaleDataError, ObjectDeletedError):
            # A delete that does not take the handle's lock (an account,
            # agent or connection going) removed a row this bind was
            # changing (#1004): nothing was bound. Text again for a fresh
            # link. STOP and disconnect wait on the lock instead (#1005).
            # Those deletes can still deadlock against a bind on Postgres;
            # that OperationalError is not caught here and the request
            # fails with nothing bound.
            return "expired"

    def _bind_imessage_handle(self, account_id, handle, pending_surface_id,
                              welcome, also, issued_at) -> str:
        names = {h for h in (handle, also) if h}
        with self.session() as s:
            st = self._locked_handle_state(s, handle)
            if (issued_at is not None and st.opted_out_at is not None
                    and st.opted_out_at >= issued_at):
                return "expired"
            bound = (s.query(Surface)
                     .filter(Surface.kind == "imessage",
                             Surface.handle.in_(names),
                             Surface.status == "active").all())
            if any(x.account_id != account_id for x in bound):
                return "taken"
            if bound and not pending_surface_id:
                # Bound here already: the sign-in link re-confirms (#871).
                # Refreshed in place, so the handle keeps its assistant.
                for x in bound:
                    x.verified_at = x.last_inbound_at = now()
                    x.reverify_notified_at = None
                return "confirmed"
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
            _drop_surfaces(s, bound)
            # Deleted before the new row takes the handle, or the unique
            # index sees both at once.
            s.flush()
            x.handle = handle
            x.status = "active"
            x.bound_at = now()
            x.code_exp = None
            x.welcome_due = 1 if welcome else 0
            x.verified_at = x.last_inbound_at = now()
            x.reverify_notified_at = None
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

    def imessage_note_inbound(self, surface_id: str) -> None:
        """A bound handle's text was let through: it is not silent."""
        with self.session() as s:
            s.execute(update(Surface).where(Surface.id == surface_id)
                      .values(last_inbound_at=now()))

    def verified_phone(self, account_id: str,
                       reverify_seconds: float) -> str | None:
        """The account's own phone number as E.164, or None: from an active
        iMessage binding whose handle is a number (an Apple ID email does
        not count) and is not due to re-confirm. Keyed on the account, not
        an agent. Two different numbers is no number: which one the person
        meant is not ours to guess."""
        from careagents.imessage import normalize_handle, reverify_due
        with self.session() as s:
            rows = (s.query(Surface)
                    .filter(Surface.account_id == account_id,
                            Surface.kind == "imessage",
                            Surface.status == "active").all())
            surfaces = [_surf_dict(x) | _reverify_fields(x) for x in rows]
        numbers = set()
        for x in surfaces:
            handle = normalize_handle(x.get("handle"))
            if not handle or "@" in handle:
                continue
            if reverify_due(x, now(), reverify_seconds):
                continue
            numbers.add(handle)
        return numbers.pop() if len(numbers) == 1 else None

    def imessage_mark_reverify(self, surface_id: str,
                               verified_at: float | None) -> bool:
        """True once per re-verification: the first time it is required.
        A conditional update, so two racing texts email the owner once.
        `verified_at` is the value read with the surface: if a confirm has
        changed it since, nothing is set (compare-and-set)."""
        same = (Surface.verified_at.is_(None) if verified_at is None
                else Surface.verified_at == verified_at)
        with self.session() as s:
            res = s.execute(update(Surface)
                            .where(Surface.id == surface_id, same,
                                   Surface.reverify_notified_at.is_(None))
                            .values(reverify_notified_at=now()))
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
            # State rows locked before the surfaces are deleted, in the
            # order a bind takes them, so the two cannot deadlock (#1005).
            # The rows are read again once the locks are held: a bind that
            # held one may have replaced the row first read.
            locked: dict = {}
            rows = q.all()
            while True:
                handles = sorted({x.handle for x in rows
                                  if x.status == "active" and x.handle}
                                 - locked.keys())
                if not handles:
                    break
                for h in handles:
                    locked[h] = self._locked_handle_state(s, h, create=False)
                rows = q.populate_existing().all()
            for x in rows:
                st = locked.get(x.handle) if x.status == "active" else None
                if st is not None:
                    st.link_count, st.link_window_start = 0, None
            _drop_surfaces(s, rows)
            return len(rows)

    def _handle_state(self, s, handle: str) -> ImessageHandleState:
        key = _handle_key(handle)
        st = s.get(ImessageHandleState, key)
        if st is None:
            st = ImessageHandleState(handle_key=key, fail_count=0,
                                     link_count=0)
            s.add(st)
        return st

    def _locked_handle_state(self, s, handle: str,
                             create: bool = True) -> ImessageHandleState | None:
        """The handle's state row, locked for this transaction, and read
        fresh, so an opt-out committed while the lock was awaited is seen
        (#1005). Postgres locks the row (FOR UPDATE). SQLite drops FOR
        UPDATE, so a write that changes nothing takes its database write
        lock first, and a racing writer waits for the commit before it
        reads (as _lock_codes does).

        The first-ever row is inserted in a savepoint: a racing insert of
        the same handle waits on it, fails on the primary key, and then
        reads and locks the row the other one committed."""
        key = _handle_key(handle)
        if self.engine.dialect.name == "sqlite":
            s.execute(update(ImessageHandleState)
                      .where(ImessageHandleState.handle_key == key)
                      .values(handle_key=ImessageHandleState.handle_key))

        def read():
            return (s.query(ImessageHandleState).filter_by(handle_key=key)
                    .with_for_update().populate_existing().first())
        st = read()
        if st is None and create:
            try:
                with s.begin_nested():
                    st = ImessageHandleState(handle_key=key, fail_count=0,
                                             link_count=0)
                    s.add(st)
            except IntegrityError:
                st = read()
        return st

    def imessage_stop(self, handle: str, also: str | None = None) -> bool:
        """Unbind the handle (and the raw spelling `also`, for a row bound
        before normalization), delete its sign-in links, and remember
        the STOP. True if this is news: not already opted out."""
        names = list({h for h in (handle, also) if h})
        with self.session() as s:
            # Locked first, as a bind does (#1005): a bind already holding
            # it commits, and the deletes below then see what it bound.
            st = self._locked_handle_state(s, handle)
            _drop_surfaces(s, s.query(Surface).filter(
                Surface.kind == "imessage", Surface.handle.in_(names),
                Surface.status == "active").all())
            # Deleted, not stamped: a link row holds the number, and a
            # missing link is refused like a used one (#1004).
            _drop_links(s, names)
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
            # The state row first, as STOP takes it, then the links: in the
            # other order the two deadlock (#1005).
            st = self._locked_handle_state(s, handle)
            _prune_links(s)
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
        """Spend a link without binding anything ("No, this isn't mine").
        Deleted, since it holds a number nobody bound (#1004); a missing
        link is refused like a used one."""
        with self.session() as s:
            s.query(ImessageLink).filter(
                ImessageLink.id == link_id,
                ImessageLink.used_at.is_(None)).delete(
                    synchronize_session=False)

    def prune_imessage_links(self) -> int:
        """Delete used and expired sign-in links whose number is not bound
        (#1004). Idempotent; runs on every link issue, and once at app
        start (careagents/app.py create_app) for rows made before."""
        with self.session() as s:
            return _prune_links(s)

    def imessage_link_handle(self, link_id: str) -> str | None:
        """The handle a live link would bind, for the confirm page."""
        with self.session() as s:
            link = s.get(ImessageLink, link_id)
            if link is None or link.used_at is not None or link.exp <= now():
                return None
            return link.handle

    def claim_imessage_link(self, link_id: str, account_id: str) -> str:
        """Spend a link for a signed-in account and bind its handle:
        "connected", "confirmed", "taken" or "expired". Spent exactly
        once."""
        with self.session() as s:
            link = s.get(ImessageLink, link_id)
            if link is None:
                return "expired"
            handle, issued_at = link.handle, link.created_at
            res = s.execute(update(ImessageLink)
                            .where(ImessageLink.id == link_id,
                                   ImessageLink.used_at.is_(None),
                                   ImessageLink.exp > now())
                            .values(used_at=now()))
            if res.rowcount != 1:
                return "expired"
        return self.bind_imessage_handle(account_id, handle, welcome=True,
                                         issued_at=issued_at)

    def get_surface(self, surface_id: str) -> dict | None:
        """An active surface by id, with its account, or None."""
        with self.session() as s:
            x = s.get(Surface, surface_id)
            if x is None or x.status != "active":
                return None
            return _surf_dict(x) | _reverify_fields(x)

    # --- Sendblue: seen-once inbound texts and owed answers ------------------
    # Pointers only (models.SendblueMessage). `key` is a hash of Sendblue's
    # message_handle, made by the caller.

    def sendblue_claim_inbound(self, key: str) -> bool:
        """True the first time `key` is seen; False for a webhook retry."""
        try:
            with self.session() as s:
                s.add(SendblueMessage(key_hash=key))
            return True
        except IntegrityError:
            return False

    def sendblue_release_inbound(self, key: str) -> None:
        """Forget `key`, so Sendblue's retry is handled afresh."""
        with self.session() as s:
            s.query(SendblueMessage).filter_by(key_hash=key).delete()

    def sendblue_track_run(self, key: str, surface_id: str,
                           run_id: str) -> None:
        with self.session() as s:
            x = s.query(SendblueMessage).filter_by(key_hash=key).first()
            if x is not None:
                x.surface_id, x.run_id = surface_id, run_id

    def sendblue_pending(self, limit: int = 50) -> list[dict]:
        """Owed answers, oldest first."""
        with self.session() as s:
            rows = (s.query(SendblueMessage)
                    .filter(SendblueMessage.run_id.isnot(None),
                            SendblueMessage.delivered_at.is_(None))
                    .order_by(SendblueMessage.created_at)
                    .limit(limit).all())
            return [{"id": x.id, "surface_id": x.surface_id,
                     "run_id": x.run_id, "created_at": x.created_at}
                    for x in rows]

    def sendblue_mark_delivered(self, row_id: str, outcome: str) -> bool:
        """Close an owed answer. True for the one caller that closed it, so
        two worker processes never both send it."""
        with self.session() as s:
            res = s.execute(update(SendblueMessage)
                            .where(SendblueMessage.id == row_id,
                                   SendblueMessage.delivered_at.is_(None))
                            .values(delivered_at=now(), outcome=outcome))
            return res.rowcount == 1

    def sendblue_set_outcome(self, row_id: str, outcome: str) -> None:
        with self.session() as s:
            x = s.get(SendblueMessage, row_id)
            if x is not None:
                x.outcome = outcome


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
            "consent_version": c.consent_version,
            "person_id": c.person_id}


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
            "advisor": a.advisor, "connection_id": a.connection_id,
            "routines": a.routines, "person_id": a.person_id,
            "created_at": a.created_at}


def _person_dict(p: Person) -> dict:
    return {"id": p.id, "kind": p.kind, "relationship": p.relation,
            "nickname": p.nickname}


def _surf_dict(x: Surface) -> dict:
    return {"id": x.id, "kind": x.kind, "handle": x.handle,
            "status": x.status, "agent_id": x.agent_id}


def _reverify_fields(x: Surface) -> dict:
    """An active iMessage surface's account and re-verification clocks
    (#871), for careagents.imessage.reverify_due."""
    return {"account_id": x.account_id, "bound_at": x.bound_at,
            "verified_at": x.verified_at,
            "last_inbound_at": x.last_inbound_at,
            "reverify_notified_at": x.reverify_notified_at}


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


def _drop_links(s, handles) -> None:
    """Delete every sign-in link texted to `handles`: each row holds the
    number itself (#1004)."""
    handles = [h for h in handles if h]
    if handles:
        s.query(ImessageLink).filter(ImessageLink.handle.in_(handles)).delete(
            synchronize_session=False)


def _drop_surfaces(s, rows) -> None:
    """Delete surfaces, and with an iMessage one the links texted to its
    number. Every path that deletes or replaces an iMessage surface comes
    here, or account delete can no longer find the number (#1004)."""
    _drop_links(s, [x.handle for x in rows if x.kind == "imessage"])
    for x in rows:
        s.delete(x)
    # Now, before the caller deletes the agent: Surface has no relationship
    # to Agent, so the flush would not order the two, and Postgres enforces
    # the foreign key.
    s.flush()


def _prune_links(s) -> int:
    """Delete used or expired links whose number is not bound now. A bound
    number's spent link goes when the binding does (_drop_surfaces)."""
    bound = s.query(Surface.handle).filter(Surface.kind == "imessage",
                                           Surface.status == "active",
                                           Surface.handle.isnot(None))
    return s.query(ImessageLink).filter(
        or_(ImessageLink.used_at.isnot(None), ImessageLink.exp <= now()),
        ImessageLink.handle.notin_(bound)).delete(synchronize_session=False)


def new_binding_code() -> str:
    return base64.b32encode(secrets.token_bytes(6)).decode().rstrip("=").lower()
