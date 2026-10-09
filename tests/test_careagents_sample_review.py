"""The relay asks for the sample review only on made-up records.

CareAgents decides sample vs real from the assistant's connection, on the
server, as the brief does (#908). Nothing the browser sends can choose the
voice: a real-records review is fetched exactly as before. The last test is
cross-layer, the real client on the real engine, because the relay tests
fake the page (the trap where a fake proves a call is made, not accepted).
Synthetic data only.
"""

from __future__ import annotations

import re

import pytest

from tests.test_careagents import FakeClient, _chat_app, cfg, svc  # noqa: F401
from tests.test_careagents_beta_weekly import _real_agent


def _record(monkeypatch):
    seen = []

    def fetch(self, tenant, action_id, **kw):
        seen.append(kw)
        return 200, f"<html>/r6/actions/{action_id}/review</html>"
    monkeypatch.setattr(FakeClient, "fetch_review_page", fetch)
    return seen


def test_a_sample_review_asks_for_the_sample_voice(cfg, svc, monkeypatch):  # noqa: F811
    seen = _record(monkeypatch)
    _app, c, _fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    assert c.get(f"/review/{agent_id}/act-1").status_code == 200
    assert seen == [{"voice": "sample"}]


@pytest.mark.parametrize("query", ["", "?voice=sample", "?sample=1"])
def test_a_real_review_is_fetched_as_before(cfg, svc, monkeypatch, query):  # noqa: F811
    """MUTATION: decide `sample` from request.args -> red on ?voice=sample."""
    seen = _record(monkeypatch)
    c, _fake, agent_id = _real_agent(cfg, svc, monkeypatch)
    assert c.get(f"/review/{agent_id}/act-1{query}").status_code == 200
    assert seen == [{}]


def test_the_client_sends_the_voice_only_for_the_sample():
    from careagents.healthclaw import HealthClawClient, HealthClawError
    hc = HealthClawClient("http://engine", "s")
    urls, secrets = [], []

    def _send(method, url, headers=None, **_):
        urls.append(url)
        secrets.append((headers or {}).get("X-Internal-Secret"))
        raise HealthClawError("stop", 503)
    hc._headers = lambda tenant: {}
    hc._send = _send
    for kwargs in ({}, {"voice": "sample"}, {"voice": "nonsense"}):
        with pytest.raises(HealthClawError):
            hc.fetch_review_page("t", "a1", **kwargs)
    assert [u.rsplit("/", 1)[-1] for u in urls] == [
        "review", "review?voice=sample", "review"]
    # The engine honours the voice only with the internal secret; a real
    # review's request carries no extra header.
    assert secrets == [None, "s", None]


def test_the_sample_review_reaches_the_real_engine(cfg, svc, monkeypatch):  # noqa: F811
    """Cross-layer: a sample connection's intake review, through the real
    client and the real engine, is about the sample person, opens with the
    made-up-records line, and leaves the attestation for the tester."""
    from careagents import beta
    from tests.test_beta_acceptance_rows import BASE, Chain
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    action_id = chain.start_form()
    r = chain.s.get(f"{BASE}/review/{chain.agent}/{action_id}")
    assert r.status_code == 200, r.text
    page = r.text
    assert beta.SAMPLE_FRAME in page
    assert "About the sample person" in page
    assert "Tick to say the sample person has no known allergies." in page
    box = re.search(r'<input[^>]*\bid="nka"[^>]*>', page).group(0)
    assert "checked" not in box
    assert 'id="sample-back-to-chat"' in page
    # The relay's rewrite, which the back link reads, still applied.
    assert f'action="/review/{chain.agent}/{action_id}/submit"' in page
