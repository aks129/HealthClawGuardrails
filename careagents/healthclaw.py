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

import requests


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
        r = self.http.post(f"{self.actions}/{action_id}/confirm",
                           headers=self._headers(tenant), timeout=self.timeout)
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

    def fasten_connect_url(self, tenant: str, public_key: str,
                           connect_base: str) -> str:
        """The provider-picker widget URL. `external_id` carries our tenant so
        Fasten's HMAC-verified success webhook ingests into the right space."""
        from urllib.parse import urlencode
        q = urlencode({"public_id": public_key, "external_id": tenant})
        return f"{connect_base.rstrip('/')}/patients/connect?{q}"

    def tenant_has_records(self, tenant: str) -> bool:
        """Poll for whether real records have landed (pending → active)."""
        try:
            bundle = self.search(tenant, "Patient", {"_summary": "count"})
            return int(bundle.get("total") or 0) > 0
        except HealthClawError:
            return False

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
