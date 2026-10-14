"""QA (PR #846 fix pass): "Don't allow" works when HealthClaw can't be reached.

Before the fix pass a denial was built locally and never called HealthClaw.
Now it asks HealthClaw for the parked request to decide where to send the
browser, and a transport failure there raises out of the view: the person who
pressed the safe button gets a 500 and the page says "That didn't work."
Nothing was shared, so the declined page is the honest answer.
"""
import json

from careagents.healthclaw import HealthClawError
from tests.test_careagents_consent import (  # noqa: F401  (fixtures)
    _handle, _signed_in, app, cfg, fake, svc)


def test_dont_allow_still_says_no_when_healthclaw_is_unreachable(
        app, svc, fake, monkeypatch):  # noqa: F811  (pytest fixtures)
    client, acct = _signed_in(app, svc, monkeypatch)

    def unreachable(request_id):
        raise HealthClawError("consent request failed", 0)

    monkeypatch.setattr(fake, "consent_request", unreachable)
    resp = client.post("/authorize/decide", data=json.dumps({
        "req": _handle(), "decision": "denied"}), content_type="application/json")
    assert resp.status_code == 200, resp.status_code
    assert resp.get_json()["redirect"] == "/authorize/declined"
    assert svc.list_grants(acct.id) == []
