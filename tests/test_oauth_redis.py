"""OAuth state must survive worker changes when Redis is configured."""

import base64
import hashlib

from r6 import oauth


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.counters = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def getdel(self, key):
        return self.values.pop(key, None)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def exists(self, key):
        return int(key in self.values)

    def eval(self, _script, _key_count, key, window):
        self.counters[key] = self.counters.get(key, 0) + 1
        return [self.counters[key], int(window)]


def _clear_process_local_oauth_state():
    oauth._registered_clients.clear()
    oauth._auth_codes.clear()
    oauth._access_tokens.clear()
    oauth._revoked_tokens.clear()


def test_oauth_flow_survives_process_local_state_loss(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setattr(oauth, "_redis_client", fake, raising=False)
    monkeypatch.setattr("r6.rate_limit._redis_client", fake)

    registered = client.post("/r6/fhir/oauth/register", json={
        "client_name": "Redis OAuth Client",
        "redirect_uris": ["https://client.example/callback"],
        "scope": "fhir.read",
    }).get_json()
    _clear_process_local_oauth_state()

    verifier = "redis-worker-verifier"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    authorized = client.get(
        "/r6/fhir/oauth/authorize",
        query_string={
            "client_id": registered["client_id"],
            "redirect_uri": "https://client.example/callback",
            "scope": "fhir.read",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers={"X-Tenant-Id": "desktop-demo"},
    )
    assert authorized.status_code == 200
    code = authorized.get_json()["code"]
    _clear_process_local_oauth_state()

    token_response = client.post("/r6/fhir/oauth/token", json={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": registered["client_id"],
        "redirect_uri": "https://client.example/callback",
    })
    assert token_response.status_code == 200
    access_token = token_response.get_json()["access_token"]
    _clear_process_local_oauth_state()

    valid, info = oauth.validate_bearer_token(access_token)
    assert valid is True
    assert info["tenant_id"] == "desktop-demo"

    client.post("/r6/fhir/oauth/revoke", json={"token": access_token})
    _clear_process_local_oauth_state()
    assert oauth.validate_bearer_token(access_token) == (
        False, "Token has been revoked"
    )


def test_authorization_code_is_consumed_atomically(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setattr(oauth, "_redis_client", fake, raising=False)
    oauth._oauth_store_set("auth-code", "one", {"exp": 123}, ttl=60)

    assert oauth._oauth_store_pop("auth-code", "one") == {"exp": 123}
    assert oauth._oauth_store_pop("auth-code", "one") is None
