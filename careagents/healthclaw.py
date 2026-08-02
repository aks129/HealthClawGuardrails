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
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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

    # --- tenant lifecycle ---------------------------------------------------

    @staticmethod
    def new_tenant_id() -> str:
        return f"ca-{secrets.token_hex(5)}"

    def mint_token(self, tenant: str) -> str:
        cached = self._tokens.get(tenant)
        if cached and (time.time() - cached[1]) < self._token_ttl:
            return cached[0]
        r = self.http.post(
            f"{self.fhir}/internal/step-up-token",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            timeout=self.timeout)
        token = (r.json() or {}).get("token") if r.ok else None
        if not token:
            raise HealthClawError(
                f"token mint failed ({r.status_code})", r.status_code)
        self._tokens[tenant] = (token, time.time())
        return token

    def seed(self, tenant: str) -> int:
        r = self.http.post(
            f"{self.fhir}/internal/seed",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            timeout=self.timeout)
        if not r.ok:
            raise HealthClawError(f"seed failed ({r.status_code})",
                                  r.status_code)
        return int((r.json() or {}).get("count") or 0)

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
        r = self.http.get(f"{self.fhir}/{resource_type}",
                          params=params or {}, headers=self._headers(tenant),
                          timeout=self.timeout)
        if r.status_code != 200:
            raise HealthClawError(
                f"search {resource_type} failed ({r.status_code})",
                r.status_code)
        return r.json()

    def interpret_labs(self, tenant: str) -> dict:
        """POST $interpret; returns {'summary','consumer','disclaimer'}."""
        r = self.http.post(f"{self.fhir}/Observation/$interpret", json={},
                           headers=self._headers(tenant), timeout=self.timeout)
        if r.status_code != 200:
            raise HealthClawError(f"$interpret failed ({r.status_code})",
                                  r.status_code)
        out = {"summary": {}, "consumer": {}, "disclaimer": ""}
        for p in (r.json() or {}).get("parameter", []):
            if p.get("name") == "summary":
                out["summary"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "consumerSummary":
                out["consumer"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "disclaimer":
                out["disclaimer"] = p.get("valueString") or ""
        return out

    def care_gaps(self, tenant: str) -> dict:
        r = self.http.get(f"{self.fhir}/Patient/$care-gaps",
                          headers=self._headers(tenant), timeout=self.timeout)
        if r.status_code != 200:
            raise HealthClawError(f"$care-gaps failed ({r.status_code})",
                                  r.status_code)
        out = {"summary": {}, "consumer": {}}
        for p in (r.json() or {}).get("parameter", []):
            if p.get("name") == "summary":
                out["summary"] = json.loads(p.get("valueString") or "{}")
            elif p.get("name") == "consumerSummary":
                out["consumer"] = json.loads(p.get("valueString") or "{}")
        return out

    # --- forms rail (propose → commit; review/confirm handled via app) -------

    def start_form_action(self, tenant: str) -> str:
        h = self._headers(tenant)
        r = self.http.post(f"{self.actions}/propose", json={
            "kind": "form-fill",
            "payload": {"to": "Intake portal",
                        "questionnaire": "healthclaw-intake",
                        "body": "new patient intake"}},
            headers=h, timeout=self.timeout)
        aid = (r.json() or {}).get("id") if r.ok else None
        if not aid:
            raise HealthClawError(f"propose failed ({r.status_code})",
                                  r.status_code)
        r = self.http.post(f"{self.actions}/{aid}/commit", headers=h,
                           timeout=self.timeout)
        if r.status_code != 202:
            raise HealthClawError(f"commit failed ({r.status_code})",
                                  r.status_code)
        return aid

    def action_status(self, tenant: str, action_id: str) -> dict:
        r = self.http.get(f"{self.actions}/{action_id}",
                          headers=self._headers(tenant), timeout=self.timeout)
        if r.status_code != 200:
            raise HealthClawError(f"action status failed ({r.status_code})",
                                  r.status_code)
        return r.json()

    def confirm_action(self, tenant: str, action_id: str) -> dict:
        mint = self.http.post(
            f"{self.actions}/{action_id}/approval-token",
            headers={"X-Tenant-Id": tenant,
                     "X-Internal-Secret": self.mint_secret},
            timeout=self.timeout)
        token = (mint.json() or {}).get("token") if mint.ok else None
        if not token:
            raise HealthClawError(
                f"approval token mint failed ({mint.status_code})",
                mint.status_code)
        r = self.http.post(f"{self.actions}/{action_id}/confirm",
                           headers={"X-Tenant-Id": tenant,
                                    "X-Step-Up-Token": token,
                                    "X-Agent-Id": "careagents"},
                           json={"approved_via": "review-page"},
                           timeout=self.timeout)
        if not r.ok:
            raise HealthClawError(f"confirm failed ({r.status_code})",
                                  r.status_code)
        return r.json()

    # --- review-page relay (credential-injecting proxy) ----------------------

    def fetch_review_page(self, tenant: str, action_id: str) -> tuple[int, str]:
        r = self.http.get(f"{self.actions}/{action_id}/review",
                          headers=self._headers(tenant), timeout=self.timeout)
        return r.status_code, r.text

    def submit_review(self, tenant: str, action_id: str,
                      decisions: dict) -> tuple[int, dict]:
        r = self.http.post(f"{self.actions}/{action_id}/review",
                           json=decisions, headers=self._headers(tenant),
                           timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
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
        """Poll for whether real records have landed (pending → active)."""
        try:
            bundle = self.search(tenant, "Patient", {"_summary": "count"})
            return int(bundle.get("total") or 0) > 0
        except HealthClawError:
            return False

    # Counted on refresh to report growth. Deliberately a fixed, clinically
    # meaningful set rather than every supported type — this is a progress
    # signal for the patient, not an inventory.
    COUNTED_TYPES = ("Condition", "Observation", "MedicationRequest",
                     "AllergyIntolerance", "Immunization", "DocumentReference")

    def record_count(self, tenant: str) -> int:
        """Total records across the counted resource types (0 on failure).

        Uses `_summary=count`, so this stays cheap and never pulls PHI into
        this app — only totals cross the boundary.
        """
        total = 0
        for rt in self.COUNTED_TYPES:
            try:
                bundle = self.search(tenant, rt, {"_summary": "count"})
                total += int(bundle.get("total") or 0)
            except HealthClawError:
                continue
        return total

    def purge_tenant(self, tenant: str) -> dict:
        """Delete this tenant's records in HealthClaw. Raises on failure.

        Deliberately not best-effort: "deleted" is only reported to the
        patient when the engine confirms it, never fire-and-forget.
        """
        r = self.http.post(
            f"{self.fhir}/internal/purge-tenant",
            json={"tenant_id": tenant},
            headers={"X-Tenant-Id": tenant,
                     "X-Step-Up-Token": self.mint_token(tenant),
                     "X-Internal-Secret": self.mint_secret},
            timeout=self.timeout)
        if r.status_code != 200:
            raise HealthClawError(f"purge failed ({r.status_code})",
                                  r.status_code)
        return r.json()

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
        """Oldest-first [{role, text}] for rehydrating a conversation."""
        try:
            r = self.http.get(
                f"{self.base}/command-center/api/conversations",
                params={"limit": limit, "full": "1", "tenant": tenant,
                        "conversation_id": conversation_id,
                        "agent_id": agent_id,
                        "through_message_id": through_message_id},
                headers={"X-Tenant-Id": tenant,
                         "X-Step-Up-Token": self.mint_token(tenant)},
                timeout=self.timeout)
            if r.status_code != 200:
                return []
            rows = r.json() or []
        except (requests.RequestException, HealthClawError, ValueError):
            logger.warning("could not load chat history for %s", tenant)
            return []
        out = [{"role": m["role"], "content": m.get("text") or ""}
               for m in reversed(rows)          # endpoint returns newest-first
               if m.get("role") in ("user", "assistant")]
        return out

    # --- durable agent runs --------------------------------------------------

    def create_agent_run(self, tenant: str, message_id: str,
                         deadline_seconds: int = 120) -> dict:
        """Create (or retrieve) the one durable run for an inbound message."""
        try:
            r = self.http.post(
                f"{self.base}/command-center/api/runs",
                json={"tenant_id": tenant, "message_id": message_id,
                      "deadline_seconds": deadline_seconds},
                headers=self._headers(tenant), timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run enqueue failed", 0) from exc
        if r.status_code not in (200, 201):
            raise HealthClawError(
                f"run enqueue failed ({r.status_code})", r.status_code)
        return r.json()

    def get_agent_run(self, tenant: str, run_id: str) -> dict:
        try:
            r = self.http.get(
                f"{self.base}/command-center/api/runs/{run_id}",
                headers=self._headers(tenant), timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run lookup failed", 0) from exc
        if r.status_code != 200:
            raise HealthClawError(
                f"run lookup failed ({r.status_code})", r.status_code)
        return r.json()

    def agent_run_events(self, tenant: str, run_id: str, after: int = 0,
                         limit: int = 100) -> dict:
        try:
            r = self.http.get(
                f"{self.base}/command-center/api/runs/{run_id}/events",
                params={"after": max(0, int(after)), "limit": limit},
                headers=self._headers(tenant), timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run event replay failed", 0) from exc
        if r.status_code != 200:
            raise HealthClawError(
                f"run event replay failed ({r.status_code})", r.status_code)
        return r.json()

    def claim_agent_run(self, worker_id: str,
                        lease_seconds: int = 60) -> dict | None:
        try:
            r = self.http.post(
                f"{self.base}/command-center/api/runs/claim",
                json={"worker_id": worker_id,
                      "lease_seconds": lease_seconds},
                headers=self._internal_headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run claim failed", 0) from exc
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise HealthClawError(
                f"run claim failed ({r.status_code})", r.status_code)
        return r.json()

    def agent_worker_health(self, max_age_seconds: int = 30) -> dict:
        """Return queue-backed worker readiness, including unavailable/503."""
        try:
            r = self.http.get(
                f"{self.base}/command-center/api/runs/workers/health",
                params={"max_age_seconds": max_age_seconds},
                headers=self._internal_headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run worker health failed", 0) from exc
        if r.status_code not in (200, 503):
            raise HealthClawError(
                f"run worker health failed ({r.status_code})", r.status_code)
        try:
            result = r.json()
        except ValueError as exc:
            raise HealthClawError(
                "run worker health returned invalid data", r.status_code
            ) from exc
        if not isinstance(result, dict):
            raise HealthClawError(
                "run worker health returned invalid data", r.status_code)
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
        try:
            r = self.http.post(
                f"{self.base}/command-center/api/runs{path}",
                json=body, headers=self._internal_headers(),
                timeout=self.timeout)
        except requests.RequestException as exc:
            raise HealthClawError("run worker request failed", 0) from exc
        if r.status_code not in expected:
            raise HealthClawError(
                f"run worker request failed ({r.status_code})", r.status_code)
        return r.json()

    # --- surfaces: Telegram binding ------------------------------------------

    def bind_telegram(self, tenant: str, chat_id: int) -> bool:
        r = self.http.post(
            f"{self.fhir}/internal/bind-telegram",
            json={"tenant_id": tenant, "chat_id": chat_id},
            headers={"X-Tenant-Id": tenant,
                     "X-Step-Up-Token": self.mint_token(tenant),
                     "X-Internal-Secret": self.mint_secret},
            timeout=self.timeout)
        return r.ok

    # --- trust panel ----------------------------------------------------------

    def conformance_badge(self) -> dict:
        r = self.http.get(f"{self.fhir}/$conformance", params={
            "format": "shields"}, timeout=self.timeout)
        return r.json() if r.ok else {"message": "unavailable"}
