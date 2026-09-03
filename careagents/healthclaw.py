"""HealthClaw HTTP client — CareAgents' ONLY data path.

Every read is redacted and audited by the guardrail layer before it reaches
this process; every action goes through propose → commit → human review →
out-of-band confirm. This client adds no policy — it carries credentials the
browser never sees (the mint secret and tenant-bound step-up tokens).
"""

from __future__ import annotations

import json
import secrets
import time

import logging

import requests

logger = logging.getLogger(__name__)


class HealthClawError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "",
                 correlation_id: str = ""):
        super().__init__(message)
        self.status = status
        # Stable error code from the engine (e.g. `too_many_entries`,
        # `not_a_bundle`, `payload_too_large`). Preserved through the
        # CareAgents layer so the UI can render an actionable message
        # rather than collapsing everything into "sync failed" (#227).
        self.code = code
        # Server-side correlation id for engine failures (e.g. `commit_failed`,
        # per-entry `ingest_error`). PHI-safe: it is an opaque handle a user
        # can quote to support without exposing exception text.
        self.correlation_id = correlation_id


class HealthClawUnconfirmed(HealthClawError):
    """The request went out and the engine never answered.

    Not the same as a refusal (#220). A refusal is an observed response saying
    no; this is silence, and the engine may have done the thing. Any caller
    that only knows about HealthClawError still catches it and degrades to
    "failed" — which is why callers that can act on the difference must catch
    this FIRST.

    Silence is not only an absent response. A gateway 502/503/504 is a
    response, and a 200 carrying an interstitial is a response, but neither is
    the ENGINE's answer (#416).
    """


class HealthClawClient:
    def __init__(self, base: str, mint_secret: str, timeout: float = 25.0):
        self.base = base.rstrip("/")
        self.fhir = f"{self.base}/r6/fhir"
        self.actions = f"{self.base}/r6/actions"
        self.mint_secret = mint_secret
        self.timeout = timeout
        self.http = requests.Session()
        # token cache: tenant -> (token, fetched_at). Step-up TTL is 5 min on
        # the layer; refresh comfortably before expiry.
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_ttl = 240.0

    # --- transport ------------------------------------------------------------
    #
    # The one place a HealthClaw call becomes either a value or a
    # HealthClawError. `agent_worker_health` has always done both halves;
    # #403 applies the same shape to the rest of the seam, because a caller
    # must be able to tell "we looked and there is nothing" from "we could
    # not look". Anything else escaping this module is caught by no CareAgents
    # caller and reaches Flask as an unhandled 500 — the defect #267 fixed for
    # `ingest_bundle` alone rather than for the boundary.

    def _send(self, method: str, url: str, *, what: str,
              error: type[HealthClawError] = HealthClawError, **kwargs):
        """Issue one request; a transport failure becomes `error`.

        Dispatches to `Session.get`/`Session.post` rather than
        `Session.request` so the call surface is exactly what every method
        here used before, and the relay doubles the cross-layer tests
        substitute for a Session keep working. Resolved lazily for the same
        reason: some of those doubles define only the verb they relay.

        `error` exists for one caller and one reason (#220). Losing the answer
        to a *retryable* call means it did not happen, which is an ordinary
        HealthClawError. Losing the answer to a call that can EXECUTE a
        clinical action means we do not know whether it happened, which is
        HealthClawUnconfirmed. Collapsing the two tells a person nothing was
        confirmed when it may already have run, and they confirm twice.
        """
        send = self.http.get if method == "GET" else self.http.post
        try:
            return send(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise error(f"{what} failed", 0) from exc

    @staticmethod
    def _json_object(r, what: str) -> dict:
        """Decode a success body the caller will treat as a JSON object.

        A 200 carrying a proxy's HTML interstitial, or valid JSON of the
        wrong type, is a failed call and not data. Returning it moves the
        failure into whatever consumes it — an AttributeError in caller code,
        one layer away from the boundary that accepted it.
        """
        try:
            body = r.json()
        except ValueError as exc:
            raise HealthClawError(f"{what} returned invalid data",
                                  r.status_code) from exc
        if not isinstance(body, dict):
            raise HealthClawError(f"{what} returned invalid data",
                                  r.status_code)
        return body

    @staticmethod
    def _upstream_answered(status: int) -> bool:
        """Whether `status` is the ENGINE's own decision about the request.

        A 4xx other than 408/429 is an answer: either the engine declined, or
        an edge rejected the request before delivering it. Nothing ran either
        way, so a caller may say so.

        This asks "did it run?", which is the write path's question. A read
        path asks a different one and must use `_answered_about_data`: a 401
        settles whether an action executed (it did not) but says nothing about
        whether a patient has a brief.

        Everything else — 5xx, 408, 429 — is a gateway speaking on the
        upstream's behalf, quite possibly after the request was already
        delivered and executed. It is silence with a status code on it. For a
        call with no side effect that distinction does not matter; for one
        that can execute a clinical action it is the whole of #416.
        """
        return 400 <= status < 500 and status not in (408, 429)

    @staticmethod
    def _answered_about_data(status: int) -> bool:
        """Whether `status` is the engine answering about the RESOURCE.

        401 and 403 are the engine answering about our credential. Every read
        caller turns "answered" into a statement about the patient — an empty
        conversation, an absent brief, "this form is no longer awaiting
        review" — so classifying a rejected token as an answer reintroduces
        the exact collapses #416, #424 and #430 removed, through the one
        predicate they share. Web and worker drifting onto different
        credentials is a documented failure mode here, so a stale token is
        reachable rather than theoretical.

        A 404 stays an answer: the engine looked and there is no such thing.
        """
        return (HealthClawClient._upstream_answered(status)
                and status not in (401, 403))

    # --- tenant lifecycle ---------------------------------------------------

    @staticmethod
    def new_tenant_id() -> str:
        return f"ca-{secrets.token_hex(5)}"

    def mint_token(self, tenant: str) -> str:
        cached = self._tokens.get(tenant)
        if cached and (time.time() - cached[1]) < self._token_ttl:
            return cached[0]
        r = self._send(
            "POST", f"{self.fhir}/internal/step-up-token",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            what="token mint")
        token = (self._json_object(r, "token mint").get("token")
                 if r.ok else None)
        if not token:
            raise HealthClawError(
                f"token mint failed ({r.status_code})", r.status_code)
        self._tokens[tenant] = (token, time.time())
        return token

    def seed(self, tenant: str) -> int:
        r = self._send(
            "POST", f"{self.fhir}/internal/seed",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            what="seed")
        if not r.ok:
            raise HealthClawError(f"seed failed ({r.status_code})",
                                  r.status_code)
        return int(self._json_object(r, "seed").get("count") or 0)

    def ingest_bundle(self, tenant: str, bundle: dict) -> dict:
        """Push a FHIR Bundle into a tenant via the engine's internal ingest.

        Used by the `direct` (upload) tile (#227). The engine is the source of
        truth for size/entry caps and per-entry validity — a failed engine
        pre-flight (413/415/400) is raised as HealthClawError carrying the
        engine's stable `error` code (e.g. `too_many_entries`, `not_a_bundle`),
        so the CareAgents layer can render an actionable message rather than
        collapsing everything to "sync failed". On 200, returns the engine's
        per-entry summary as-is.

        Contract: the engine derives tenant SOLELY from the `X-Tenant-Id`
        header. The JSON body carries ONLY `{"bundle": ...}` — sending a
        `tenant_id` in the body is rejected engine-side as a legacy selector.
        """
        # `application/json` — this is the CareAgents→engine internal
        # envelope call, NOT a raw FHIR Bundle post. `application/fhir+json`
        # would mis-label the envelope; the patient-facing route above
        # accepts fhir+json for the raw Bundle from the browser.
        r = self._send(
            "POST", f"{self.fhir}/internal/ingest-bundle",
            json={"bundle": bundle},
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret,
                     "Content-Type": "application/json"},
            what="ingest")
        if r.status_code != 200:
            # On the error path an unparseable body is expected — an edge or
            # proxy may answer instead of the engine — so fall back to a
            # synthetic code rather than losing the status.
            try:
                body = r.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                body = None
            code = (body or {}).get("error") or f"http_{r.status_code}"
            msg = (body or {}).get("message") \
                or f"ingest failed ({code})"
            correlation = (body or {}).get("correlation_id") or ""
            raise HealthClawError(msg, r.status_code, code=code,
                                  correlation_id=correlation)
        # A 200 that will not decode is not "ingested nothing". It used to
        # become `{}`, which careagents/app.py reports to the upload tile as
        # a completed upload of zero records while the file never landed
        # (#403).
        return self._json_object(r, "ingest")

    def _headers(self, tenant: str) -> dict:
        return {"X-Tenant-Id": tenant,
                "X-Step-Up-Token": self.mint_token(tenant),
                "X-Agent-Id": "careagents"}

    def _internal_headers(self) -> dict:
        return {"X-Internal-Secret": self.mint_secret,
                "X-Agent-Id": "careagents-worker"}

    # --- reads (redacted + audited by the layer) -----------------------------

    def search(self, tenant: str, resource_type: str,
               params: dict | None = None) -> dict:
        r = self._send("GET", f"{self.fhir}/{resource_type}",
                       params=params or {}, headers=self._headers(tenant),
                       what=f"search {resource_type}")
        if r.status_code != 200:
            raise HealthClawError(
                f"search {resource_type} failed ({r.status_code})",
                r.status_code)
        return self._json_object(r, f"search {resource_type}")

    def read(self, tenant: str, resource_type: str, resource_id: str) -> dict:
        """Read one resource by id, through the same redact+audit gate as
        search. Exists for reference-chasing (MedicationRequest → Medication);
        every call is a separately audited access, which is the point."""
        r = self._send("GET", f"{self.fhir}/{resource_type}/{resource_id}",
                       headers=self._headers(tenant),
                       what=f"read {resource_type}")
        if r.status_code != 200:
            raise HealthClawError(
                f"read {resource_type} failed ({r.status_code})",
                r.status_code)
        return self._json_object(r, f"read {resource_type}")

    def interpret_labs(self, tenant: str) -> dict:
        """POST $interpret; returns {'summary','consumer','disclaimer'}."""
        r = self._send("POST", f"{self.fhir}/Observation/$interpret", json={},
                       headers=self._headers(tenant), what="$interpret")
        if r.status_code != 200:
            raise HealthClawError(f"$interpret failed ({r.status_code})",
                                  r.status_code)
        # `bundle` carries the ANNOTATED Observations — each already redacted,
        # audited and flagged by the engine. The timeline card builds its
        # series from these rather than issuing a second, differently-shaped
        # read, so the chart and the agent's prose can never disagree about a
        # value or a flag.
        out = {"summary": {}, "consumer": {}, "disclaimer": "", "bundle": {}}
        for p in self._json_object(r, "$interpret").get("parameter") or []:
            if p.get("name") == "summary":
                out["summary"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "consumerSummary":
                out["consumer"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "disclaimer":
                out["disclaimer"] = p.get("valueString") or ""
            elif p.get("name") == "return":
                out["bundle"] = p.get("resource") or {}
        return out

    def care_gaps(self, tenant: str) -> dict:
        r = self._send("GET", f"{self.fhir}/Patient/$care-gaps",
                       headers=self._headers(tenant), what="$care-gaps")
        if r.status_code != 200:
            raise HealthClawError(f"$care-gaps failed ({r.status_code})",
                                  r.status_code)
        out = {"summary": {}, "consumer": {}}
        for p in self._json_object(r, "$care-gaps").get("parameter") or []:
            if p.get("name") == "summary":
                out["summary"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "consumerSummary":
                out["consumer"] = json.loads(p.get("valueString") or "{}")
        return out

    # --- forms rail (propose → commit; review/confirm handled via app) -------

    def start_form_action(self, tenant: str) -> str:
        h = self._headers(tenant)
        r = self._send("POST", f"{self.actions}/propose", json={
            "kind": "form-fill",
            "payload": {"to": "Intake portal",
                        "questionnaire": "healthclaw-intake",
                        "body": "new patient intake"}},
            headers=h, what="propose")
        aid = self._json_object(r, "propose").get("id") if r.ok else None
        if not aid:
            raise HealthClawError(f"propose failed ({r.status_code})",
                                  r.status_code)
        r = self._send("POST", f"{self.actions}/{aid}/commit", headers=h,
                       what="commit")
        if r.status_code != 202:
            raise HealthClawError(f"commit failed ({r.status_code})",
                                  r.status_code)
        return aid

    def action_status(self, tenant: str, action_id: str) -> dict:
        r = self._send("GET", f"{self.actions}/{action_id}",
                       headers=self._headers(tenant), what="action status")
        if r.status_code != 200:
            raise HealthClawError(f"action status failed ({r.status_code})",
                                  r.status_code)
        return self._json_object(r, "action status")

    def confirm_action(self, tenant: str, action_id: str) -> dict:
        """Confirm a reviewed action. Raises on refusal, HealthClawUnconfirmed
        on silence.

        Three outcomes, and the caller must be able to tell them apart (#220).
        The mint is safely retryable, so a transport failure there means the
        confirm never went out — a refusal. The confirm POST is the one that
        can execute a clinical action, so losing its answer is NOT evidence
        that nothing happened.

        "Losing the answer" is wider than a dropped socket, which is the gap
        #416 closed: a gateway status (5xx, 408, 429) and a 200 whose body
        will not decode are both responses that say nothing about what the
        engine did. Only `_upstream_answered` is a refusal.
        """
        # Nothing was confirmed if this fails: we never reached the confirm.
        mint = self._send(
            "POST", f"{self.actions}/{action_id}/approval-token",
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            what="approval token mint")
        token = (self._json_object(mint, "approval token mint").get("token")
                 if mint.ok else None)
        if not token:
            raise HealthClawError(
                f"approval token mint failed ({mint.status_code})",
                mint.status_code)
        # A lost answer HERE means the confirm may already have run, so it is
        # HealthClawUnconfirmed and not an ordinary failure. This is the one
        # call in the client that gets a different error type, and the reason
        # `_send` takes one.
        r = self._send("POST", f"{self.actions}/{action_id}/confirm",
                       headers={"X-Tenant-Id": tenant,
                                "X-Step-Up-Token": token,
                                "X-Agent-Id": "careagents"},
                       json={"approved_via": "review-page"},
                       what="confirm", error=HealthClawUnconfirmed)
        if not r.ok:
            if not self._upstream_answered(r.status_code):
                # An edge 502/503/504 IS a response, so `_send` handed it
                # back — but it is not the engine's, and the confirm may
                # already have run. Filing it as a refusal is what told a
                # patient "nothing has been sent, please try approving again"
                # for an action the engine had executed (#416).
                raise HealthClawUnconfirmed(
                    f"confirm unanswered ({r.status_code})", r.status_code)
            raise HealthClawError(f"confirm failed ({r.status_code})",
                                  r.status_code)
        try:
            return self._json_object(r, "confirm")
        except HealthClawError as exc:
            # Same rule on the success side: something answered 200, and a
            # body we cannot read says nothing about whether the engine
            # executed. Only a decodable answer is a confirmation.
            raise HealthClawUnconfirmed("confirm returned invalid data",
                                        r.status_code) from exc

    # --- review-page relay (credential-injecting proxy) ----------------------

    def fetch_review_page(self, tenant: str, action_id: str) -> tuple[int, str]:
        r = self._send("GET", f"{self.actions}/{action_id}/review",
                       headers=self._headers(tenant), what="review fetch")
        return r.status_code, r.text

    def submit_review(self, tenant: str, action_id: str,
                      decisions: dict) -> tuple[int, dict]:
        r = self._send("POST", f"{self.actions}/{action_id}/review",
                       json=decisions, headers=self._headers(tenant),
                       what="review submit")
        if r.ok:
            # The rule the rest of this client follows, which this method was
            # the one exemption from: a 200 carrying a proxy's interstitial is
            # a failed call and not data (`_json_object`). Substituting a body
            # and returning the status unchanged made the relay read it as the
            # engine saying "saved" — it then confirmed, and told a patient
            # their approval was recorded with no confirmation row anywhere
            # (QA on #566, reproduced against a running engine).
            #
            # HealthClawUnconfirmed rather than a plain error, for the reason
            # `confirm_action` raises it on its own unreadable 200: this POST
            # MINTS the ActionConfirmation (#528), so something answered and
            # whether an approval now exists is not knowable from here. Third
            # answer (#220), and the relay routes it to `confirmed: null`.
            try:
                return r.status_code, self._json_object(r, "review submit")
            except HealthClawError as exc:
                raise HealthClawUnconfirmed(
                    "review submit returned invalid data",
                    r.status_code) from exc
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # A non-2xx is somebody's refusal and nothing was minted by it.
            # The contract is (status, dict); a non-object body would break
            # the caller's `.get`.
            body = {"error": "unexpected response"}
        return r.status_code, body

    # --- Fasten (verified-provider real records) -----------------------------

    def fasten_connect_url(self, tenant: str) -> str:
        """Route to HealthClaw's own Fasten Connect page for this tenant.

        That page (already wired on the deployment) embeds the Stitch widget
        with the server's FASTEN_PUBLIC_KEY and external-id=<tenant>; Fasten's
        HMAC-verified `patient.connection_success` webhook then registers the
        org_connection_id back to this tenant and ingests the records. We do
        NOT build a Fasten-hosted URL ourselves — the provider domain differs
        by TEFCA/mode and only the HealthClaw page has the verified key."""
        return f"{self.base}/connect/{tenant}"

    def wearables_connect_url(self, tenant: str, provider: str) -> str:
        """Route to HealthClaw's wearables OAuth kickoff for this tenant +
        provider (Apple Health, Oura, Whoop, …). HealthClaw owns the Open
        Wearables handshake; if the sidecar isn't wired it returns a clear
        503 there rather than leaking any credential."""
        from urllib.parse import urlencode
        q = urlencode({"provider": provider, "tenant_id": tenant})
        return f"{self.base}/wearables/oauth/start?{q}"

    def tenant_has_records(self, tenant: str) -> bool:
        """Whether real records have landed (pending → active).

        Raises HealthClawError when the engine could not be asked. This used
        to answer False, which the poll endpoint renders as "pending" — so an
        outage left the patient watching "still fetching your records" on a
        condition that would never be re-evaluated, with nothing anywhere
        saying the record store was down (#403).
        """
        bundle = self.search(tenant, "Patient", {"_summary": "count"})
        return int(bundle.get("total") or 0) > 0

    # Counted on refresh to report growth. Deliberately a fixed, clinically
    # meaningful set rather than every supported type — this is a progress
    # signal for the patient, not an inventory.
    COUNTED_TYPES = ("Condition", "Observation", "MedicationRequest",
                     "AllergyIntolerance", "Immunization", "DocumentReference")

    def record_count(self, tenant: str) -> int:
        """Total records across the counted resource types.

        Uses `_summary=count`, so this stays cheap and never pulls PHI into
        this app — only totals cross the boundary.

        Raises HealthClawError when any type could not be counted, rather
        than skipping it. A partial sum is indistinguishable from a genuinely
        smaller record set, and a total outage used to return 0 — which
        careagents/app.py reports to the patient, as fact, as the number of
        records they have (#403).
        """
        total = 0
        for rt in self.COUNTED_TYPES:
            bundle = self.search(tenant, rt, {"_summary": "count"})
            total += int(bundle.get("total") or 0)
        return total

    def fetch_appointment_brief(self, tenant: str) -> dict | None:
        """The brief, or None when the engine answered and there is none.

        Raises HealthClawError when we could not find out. The two were the
        same value before, and the template renders None as "Not available
        from your connected records" — a statement about the patient's
        records, made during an outage that read none of them. The same page
        already gets this right one section down, where the screening review
        requires an explicit "ok" before it claims anything (#381).

        A malformed 200 raises too: it means we did not learn whether a brief
        exists, which is the same fact as an unreachable engine.
        """
        r = self._send(
            "GET", f"{self.fhir}/AppointmentBrief",
            headers=self._headers(tenant),
            what="appointment brief")
        if r.status_code == 200:
            # dict-or-None is what the brief renderer is written against.
            # A wrongly-shaped 200 used to be handed straight through as
            # though it were the FHIR Basic resource, moving the failure
            # one layer past the boundary that accepted it (#403).
            return self._json_object(r, "appointment brief")
        if self._answered_about_data(r.status_code):
            return None
        raise HealthClawError(
            f"appointment brief unavailable ({r.status_code})", r.status_code)

    def purge_tenant(self, tenant: str) -> dict:
        """Delete this tenant's records in HealthClaw. Raises on failure.

        Deliberately not best-effort: "deleted" is only reported to the
        patient when the engine confirms it, never fire-and-forget.
        """
        r = self._send(
            "POST", f"{self.fhir}/internal/purge-tenant",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Step-Up-Token": self.mint_token(tenant),
                     "X-Internal-Secret": self.mint_secret},
            what="purge")
        if r.status_code != 200:
            raise HealthClawError(f"purge failed ({r.status_code})",
                                  r.status_code)
        return self._json_object(r, "purge")

    # --- conversation history -------------------------------------------------
    #
    # Chat history lives in HealthClaw, per tenant, NOT in the CareAgents
    # database. A conversation about someone's records is PHI-adjacent, and
    # CareAgents stores no PHI — putting transcripts in its tables would trade
    # away that boundary for a caching convenience. Keeping them tenant-scoped
    # in the engine also means "delete my records" already removes them
    # (r6/purge.py purges ConversationMessage).
    #
    # Only user and assistant text is persisted — never tool calls or their
    # results. Those are point-in-time facts about records that may since have
    # changed; replaying them as history would let a stale reading masquerade
    # as current truth. On a cold start the agent re-reads instead.

    @staticmethod
    def conversation_id(agent_id: str) -> str:
        """Stable default thread shared by every surface for one agent."""
        return f"careagents:{agent_id}"

    def _post_message(self, tenant: str, role: str, text: str,
                      agent_id: str | None = None,
                      conversation_id: str | None = None,
                      surface: str = "web",
                      request_id: str | None = None,
                      reply_to: str | None = None) -> tuple[int | None, dict | None]:
        try:
            r = self.http.post(
                f"{self.base}/command-center/api/conversations",
                json={"tenant_id": tenant, "role": role, "text": text,
                      "agent_id": agent_id,
                      "conversation_id": conversation_id,
                      "surface": surface,
                      "request_id": request_id,
                      "reply_to": reply_to,
                      "metadata": {"careagents_agent_id": agent_id}},
                headers={"X-Tenant-Id": tenant,
                         "X-Step-Up-Token": self.mint_token(tenant)},
                timeout=self.timeout)
            body = r.json() if r.status_code in (200, 201) else None
            return r.status_code, body
        except (requests.RequestException, HealthClawError, ValueError,
                AttributeError):
            logger.warning("could not persist a chat turn for %s", tenant)
            return None, None

    def claim_inbound_message(self, tenant: str, text: str, agent_id: str,
                              conversation_id: str, surface: str,
                              request_id: str) -> tuple[bool | None, str | None]:
        """Create an inbound turn once.

        Returns ``(True, id)`` when created, ``(False, id)`` on an idempotent
        replay, and ``(None, None)`` when durable storage is unavailable.
        """
        status, body = self._post_message(
            tenant, "user", text, agent_id, conversation_id, surface,
            request_id=request_id)
        if status not in (200, 201) or not body:
            if status is not None:
                logger.warning("chat turn rejected for %s: HTTP %s",
                               tenant, status)
            return None, None
        return status == 201, body.get("id")

    def log_message(self, tenant: str, role: str, text: str,
                    agent_id: str | None = None,
                    conversation_id: str | None = None,
                    surface: str = "web",
                    reply_to: str | None = None,
                    request_id: str | None = None) -> bool:
        """Append one turn. Returns False on failure rather than raising:
        losing a transcript must never break the conversation in progress.

        The command-center API treats ``agent_id`` as an opaque tenant-scoped
        identity, so CareAgents can preserve its own agent UUID explicitly.
        """
        status, _body = self._post_message(
            tenant, role, text, agent_id, conversation_id, surface,
            request_id=request_id, reply_to=reply_to)
        if status not in (200, 201):
            if status is not None:
                logger.warning("chat turn rejected for %s: HTTP %s",
                               tenant, status)
            return False
        return True

    def recent_messages(self, tenant: str, limit: int = 20,
                        conversation_id: str | None = None,
                        agent_id: str | None = None,
                        through_message_id: str | None = None) -> list[dict]:
        """Oldest-first [{role, text}] for rehydrating a conversation.

        An empty list means the engine answered and there is nothing to
        replay. Losing the thread raises instead, because the two were the
        same value and the collapse was invisible in both directions: the web
        tier rendered every return visit during an outage as a first visit,
        and the worker built the agent's context from it, so the model
        answered with amnesia and said nothing about it.
        """
        r = self._send(
            "GET", f"{self.base}/command-center/api/conversations",
            params={"limit": limit, "full": "1", "tenant": tenant,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "through_message_id": through_message_id},
            headers={"X-Tenant-Id": tenant,
                     "X-Step-Up-Token": self.mint_token(tenant)},
            what="chat history")
        if r.status_code != 200:
            if self._answered_about_data(r.status_code):
                return []
            raise HealthClawError(
                f"chat history unavailable ({r.status_code})", r.status_code)
        try:
            rows = r.json() or []
        except ValueError as exc:
            raise HealthClawError("chat history was not JSON", 200) from exc
        # A 200 of the wrong shape is a failed call, not history. Indexing it
        # raised AttributeError/TypeError out of this module, past the one
        # boundary whose job is to turn a bad call into a HealthClawError —
        # and app.py catches only HealthClawError, so each was a 500 on /chat.
        # #430 hardened the brief against exactly this and left the sibling.
        if not isinstance(rows, list):
            raise HealthClawError("chat history returned invalid data", 200)
        out = [{"role": m["role"], "content": m.get("text") or ""}
               for m in reversed(rows)          # endpoint returns newest-first
               if isinstance(m, dict)
               and m.get("role") in ("user", "assistant")]
        return out

    # --- durable agent runs --------------------------------------------------

    def create_agent_run(self, tenant: str, message_id: str,
                         deadline_seconds: int = 120) -> dict:
        """Create (or retrieve) the one durable run for an inbound message."""
        r = self._send(
            "POST", f"{self.base}/command-center/api/runs",
            json={"tenant_id": tenant, "message_id": message_id,
                  "deadline_seconds": deadline_seconds},
            headers=self._headers(tenant), what="run enqueue")
        if r.status_code not in (200, 201):
            raise HealthClawError(
                f"run enqueue failed ({r.status_code})", r.status_code)
        return self._json_object(r, "run enqueue")

    def get_agent_run(self, tenant: str, run_id: str) -> dict:
        r = self._send(
            "GET", f"{self.base}/command-center/api/runs/{run_id}",
            headers=self._headers(tenant), what="run lookup")
        if r.status_code != 200:
            raise HealthClawError(
                f"run lookup failed ({r.status_code})", r.status_code)
        return self._json_object(r, "run lookup")

    def agent_run_events(self, tenant: str, run_id: str, after: int = 0,
                         limit: int = 100) -> dict:
        r = self._send(
            "GET", f"{self.base}/command-center/api/runs/{run_id}/events",
            params={"after": max(0, int(after)), "limit": limit},
            headers=self._headers(tenant), what="run event replay")
        if r.status_code != 200:
            raise HealthClawError(
                f"run event replay failed ({r.status_code})", r.status_code)
        return self._json_object(r, "run event replay")

    def claim_agent_run(self, worker_id: str,
                        lease_seconds: int = 60) -> dict | None:
        r = self._send(
            "POST", f"{self.base}/command-center/api/runs/claim",
            json={"worker_id": worker_id,
                  "lease_seconds": lease_seconds},
            headers=self._internal_headers(), what="run claim")
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise HealthClawError(
                f"run claim failed ({r.status_code})", r.status_code)
        return self._json_object(r, "run claim")

    def agent_worker_health(self, max_age_seconds: int = 30) -> dict:
        """Return queue-backed worker readiness, including unavailable/503."""
        r = self._send(
            "GET", f"{self.base}/command-center/api/runs/workers/health",
            params={"max_age_seconds": max_age_seconds},
            headers=self._internal_headers(), what="run worker health")
        if r.status_code not in (200, 503):
            raise HealthClawError(
                f"run worker health failed ({r.status_code})", r.status_code)
        result = self._json_object(r, "run worker health")
        result["available"] = r.status_code == 200 and bool(
            result.get("available"))
        return result

    def heartbeat_agent_run(self, run_id: str, worker_id: str,
                            lease_seconds: int = 60) -> dict:
        return self._run_internal_post(
            f"/{run_id}/heartbeat",
            {"worker_id": worker_id, "lease_seconds": lease_seconds})

    def transition_agent_run(self, run_id: str, worker_id: str, status: str,
                             *, event_type: str | None = None,
                             payload=None,
                             error_class: str | None = None,
                             available_in_seconds: int = 0) -> dict:
        body = {"worker_id": worker_id, "status": status,
                "available_in_seconds": available_in_seconds}
        if event_type is not None:
            body["event_type"] = event_type
        if payload is not None:
            body["payload"] = payload
        if error_class is not None:
            body["error_class"] = error_class
        return self._run_internal_post(f"/{run_id}/transition", body)

    def finalize_agent_run(self, run_id: str, worker_id: str, text: str,
                           checkpoint_id: str) -> dict:
        """Atomically persist the assistant answer and run completion."""
        return self._run_internal_post(
            f"/{run_id}/finalize",
            {"worker_id": worker_id, "text": text,
             "checkpoint_id": checkpoint_id},
        )

    def append_agent_run_event(self, run_id: str, worker_id: str,
                               event_type: str, payload=None) -> dict:
        body = {"worker_id": worker_id, "type": event_type}
        if payload is not None:
            body["payload"] = payload
        return self._run_internal_post(f"/{run_id}/events", body,
                                       expected=(201,))

    def register_agent_tool_call(self, run_id: str, worker_id: str,
                                 provider_call_id: str, tool_name: str,
                                 arguments: dict) -> dict:
        return self._run_internal_post(
            f"/{run_id}/tool-calls",
            {"worker_id": worker_id,
             "provider_call_id": provider_call_id,
             "tool_name": tool_name,
             "arguments": arguments},
            expected=(200, 201))

    def transition_agent_tool_call(self, run_id: str, call_id: str,
                                   worker_id: str, status: str, *,
                                   result=None, outcome_ref: str | None = None,
                                   error_class: str | None = None) -> dict:
        body = {"worker_id": worker_id, "status": status}
        if result is not None:
            body["result"] = result
        if outcome_ref is not None:
            body["outcome_ref"] = outcome_ref
        if error_class is not None:
            body["error_class"] = error_class
        return self._run_internal_post(
            f"/{run_id}/tool-calls/{call_id}/transition", body)

    def _run_internal_post(self, path: str, body: dict,
                           expected: tuple[int, ...] = (200,)) -> dict:
        r = self._send(
            "POST", f"{self.base}/command-center/api/runs{path}",
            json=body, headers=self._internal_headers(),
            what="run worker request")
        if r.status_code not in expected:
            raise HealthClawError(
                f"run worker request failed ({r.status_code})", r.status_code)
        return self._json_object(r, "run worker request")

    # --- surfaces: Telegram binding ------------------------------------------

    def bind_telegram(self, tenant: str, chat_id: int) -> bool:
        r = self._send(
            "POST", f"{self.fhir}/internal/bind-telegram",
            json={"tenant_id": tenant, "chat_id": chat_id},
            headers={"X-Tenant-Id": tenant,
                     "X-Step-Up-Token": self.mint_token(tenant),
                     "X-Internal-Secret": self.mint_secret},
            what="telegram bind")
        return r.ok

    # --- trust panel ----------------------------------------------------------

    def conformance_badge(self) -> dict:
        r = self._send("GET", f"{self.fhir}/$conformance",
                       params={"format": "shields"},
                       what="conformance badge")
        if not r.ok:
            return {"message": "unavailable"}
        return self._json_object(r, "conformance badge")
