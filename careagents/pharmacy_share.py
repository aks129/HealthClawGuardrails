"""Share records with a pharmacy, by text (RX Genie handoff).

Behind CARE_PHARMACY_SHARE, off by default; off, nothing here is wired and
the words below reach the assistant like any other text.

1. The person texts PHARMACY. We answer with one fixed question naming the
   pharmacy and what would be shared, and keep the offer for
   OFFER_TTL_SECONDS (a PharmacyShare row: the consent record).
2. They reply YES. The offer is answered once, by a conditional update, so
   a second YES does nothing. The engine makes a one-use SMART Health Link
   of their records (r6/ktc.py, deliver=link): the records stay in the
   engine, encrypted, and only the shlink comes back.
3. The shlink is POSTed to the pharmacy's intake URL as
   {"shlink", "purpose", "consent"}. The pharmacy fetches the file from the
   engine once, within the hour.

NO, or no answer within the window, shares nothing. A bare YES or NO with
no open offer goes to the assistant as before.

What never happens: the link or any record goes by text (the replies are
the fixed sentences below); the link is logged, stored or put in an error
(it carries the key); the engine's or the pharmacy's words reach the
person. Logs carry status codes and fixed outcome codes only.

The decision is the person's word, read here, never a model's tool call.
The engine refuses a link of records without the HTEST test-data marker
until KTC_LINK_REAL_RECORDS is set there: the text line is not under a BAA
yet, so for now only made-up records can be shared this way.
"""

from __future__ import annotations

import logging
import time
from typing import Callable
from urllib.parse import urlsplit

import requests
from sqlalchemy import update

from careagents.healthclaw import HealthClawError, acting_for
from careagents.models import PharmacyShare

logger = logging.getLogger(__name__)

#: What is shared: the engine's ktc set (r6/ktc.py KTC_TYPES). Medicines,
#: allergies, conditions, immunizations and insurance, plus the Patient the
#: pharmacy matches on.
SHARE_TYPES = ("AllergyIntolerance", "Condition", "Coverage",
               "Immunization", "MedicationRequest", "Patient")

#: How long a PHARMACY offer waits for YES.
OFFER_TTL_SECONDS = 10 * 60

#: The intake POST's own budget. The pharmacy fetches the file inside it.
INTAKE_TIMEOUT_SECONDS = 20

#: Sent with the link; fixed words, the shape RX Genie's own demo sends.
PURPOSE = "Pharmacy visit"
CONSENT = "Patient replied YES by text"
#: Stored on the consent row.
PURPOSE_CODE = "pharmacy-visit"


def offer_text(pharmacy: str) -> str:
    return (f"Do you want me to send your medicines, allergies, conditions, "
            f"immunizations and insurance to {pharmacy} for your visit? "
            "They go straight to the pharmacy, never by text, and the "
            "pharmacy can open them once, within an hour. Reply YES to "
            "share or NO to skip. This offer ends in 10 minutes.")


def sent_text(pharmacy: str) -> str:
    return (f"Done. {pharmacy} has your records for your visit. Nothing "
            "else was shared.")


DECLINED_TEXT = "OK, nothing was shared."

NOT_LIVE_TEXT = ("This assistant's records are disconnected, so there is "
                 "nothing to share.")

PAUSED_TEXT = ("Your real records are paused, so I can't share them. "
               "Nothing was sent.")

SAMPLE_TEXT = ("The sample records can't be shared with a pharmacy. "
               "Nothing was sent.")

TEST_ONLY_TEXT = ("For now I can share only test records with a pharmacy, "
                  "so nothing was sent.")

NO_RECORDS_TEXT = ("I couldn't find one set of records to share, so "
                   "nothing was sent.")

FAILED_TEXT = ("I couldn't send your records to the pharmacy just now. "
               "Please try again later, or bring your medicine list and "
               "insurance card.")


def word(text: str) -> str | None:
    """"pharmacy", "yes" or "no" when that is the whole message (case and a
    trailing . or ! ignored), else None."""
    w = (text or "").strip().rstrip(".!").strip().lower()
    return w if w in ("pharmacy", "yes", "no") else None


class Share:
    """The PHARMACY / YES / NO flow. `http` and `clock` are for tests."""

    def __init__(self, cfg, svc, hc, http=None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.svc = svc
        self.hc = hc
        self.http = http or requests.Session()
        self.clock = clock

    @property
    def host(self) -> str:
        return urlsplit(self.cfg.pharmacy_intake_url).hostname or ""

    # --- the hook imessage.handle_inbound calls -----------------------------

    def __call__(self, surface: dict, ctx: dict, text: str) -> str | None:
        """A sentence to answer, or None to let the text reach the
        assistant."""
        w = word(text)
        if w is None:
            return None
        if w == "pharmacy":
            return self.offer(surface, ctx)
        row = self._answer(surface, ctx, w)
        if row is None:
            return None
        if w == "no":
            return DECLINED_TEXT
        return self.share(row, ctx)

    # --- steps ----------------------------------------------------------------

    def _refusal(self, ctx: dict, account_id: str
                 ) -> tuple[str, str] | None:
        conn = ctx.get("connection") or {}
        if conn.get("status") != "active":
            return "refused", NOT_LIVE_TEXT
        if conn.get("kind") == "sample":
            return "refused", SAMPLE_TEXT
        if self.svc.is_paused(account_id):
            return "refused", PAUSED_TEXT
        return None

    def offer(self, surface: dict, ctx: dict) -> str:
        refused = self._refusal(ctx, surface["account_id"])
        if refused:
            return refused[1]
        now = self.clock()
        with self.svc.session() as s:
            # One open offer per phone: an older one is closed unanswered.
            s.execute(update(PharmacyShare)
                      .where(PharmacyShare.surface_id == surface["id"],
                             PharmacyShare.answered_at.is_(None))
                      .values(answered_at=now, outcome="replaced",
                              finished_at=now))
            s.add(PharmacyShare(
                account_id=surface["account_id"],
                agent_id=ctx["agent"]["id"],
                connection_id=(ctx.get("connection") or {}).get("id"),
                surface_id=surface["id"], pharmacy_host=self.host,
                purpose=PURPOSE_CODE, asked_at=now,
                expires_at=now + OFFER_TTL_SECONDS))
        logger.info("pharmacy share offered (agent %s)", ctx["agent"]["id"])
        return offer_text(self.cfg.pharmacy_name)

    def _answer(self, surface: dict, ctx: dict, w: str) -> dict | None:
        """Take the phone's open offer with this answer, once. None when
        there is no open, unexpired offer for this phone and agent."""
        now = self.clock()
        with self.svc.session() as s:
            row = (s.query(PharmacyShare)
                   .filter(PharmacyShare.surface_id == surface["id"],
                           PharmacyShare.agent_id == ctx["agent"]["id"],
                           PharmacyShare.answered_at.is_(None),
                           PharmacyShare.expires_at > now)
                   .order_by(PharmacyShare.asked_at.desc()).first())
            if row is None:
                return None
            won = s.execute(update(PharmacyShare)
                            .where(PharmacyShare.id == row.id,
                                   PharmacyShare.answered_at.is_(None))
                            .values(answered_at=now, decision=w))
            if won.rowcount != 1:
                return None
            if w == "no":
                s.execute(update(PharmacyShare)
                          .where(PharmacyShare.id == row.id)
                          .values(outcome="declined", finished_at=now))
            return {"id": row.id, "account_id": row.account_id}

    def _finish(self, share_id: str, outcome: str) -> None:
        with self.svc.session() as s:
            s.execute(update(PharmacyShare)
                      .where(PharmacyShare.id == share_id)
                      .values(outcome=outcome, finished_at=self.clock()))
        logger.info("pharmacy share %s: %s", share_id, outcome)

    def share(self, row: dict, ctx: dict) -> str:
        """Make the link and hand it to the pharmacy. Every exit records an
        outcome code and answers a fixed sentence."""
        refused = self._refusal(ctx, row["account_id"])
        if refused:
            self._finish(row["id"], refused[0])
            return refused[1]
        try:
            with acting_for(row["account_id"]):
                link = self.hc.ktc_share_link(ctx["tenant"],
                                              list(SHARE_TYPES))
        except HealthClawError as exc:
            # The status and code only: the engine's words stay there.
            logger.warning("pharmacy share link failed (%s %s)",
                           exc.status, exc.code or "-")
            if exc.code == "not_test_data":
                self._finish(row["id"], "not_test_data")
                return TEST_ONLY_TEXT
            if exc.code == "no_patient":
                self._finish(row["id"], "no_records")
                return NO_RECORDS_TEXT
            self._finish(row["id"], "failed")
            return FAILED_TEXT
        try:
            r = self.http.post(
                self.cfg.pharmacy_intake_url,
                json={"shlink": link["shlink"], "purpose": PURPOSE,
                      "consent": CONSENT},
                timeout=INTAKE_TIMEOUT_SECONDS,
                # A redirect would carry the link to a host nobody chose.
                allow_redirects=False)
            status = r.status_code
        except requests.RequestException as exc:
            logger.warning("pharmacy intake unreachable (%s)",
                           type(exc).__name__)
            status = 0
        finally:
            link = None
        if not 200 <= status < 300:
            logger.warning("pharmacy intake answered %s", status)
            self._finish(row["id"], "failed")
            return FAILED_TEXT
        self._finish(row["id"], "sent")
        return sent_text(self.cfg.pharmacy_name)
