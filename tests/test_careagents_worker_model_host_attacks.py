"""Security-tester attacks on the per-turn model-host gate (PR #990).

The gate's verdict must match the host the HTTP client actually connects
to. Python's urlparse and requests/urllib3 disagree on a backslash in the
authority, so a base URL can read as an approved host to the gate while
the request goes elsewhere. Synthetic data only.
"""

from __future__ import annotations

import pytest
import requests
from urllib3.util import parse_url

from careagents import agent, tester_terms
from careagents.config import Config
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _turn, cfg, svc)


def _cfg(**env):
    return Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                       "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost", **env})


@pytest.mark.parametrize("base", [
    # userinfo naming the approved host, real host elsewhere
    "https://api.openai.com@evil.example/v1",
    "https://api.openai.com:443@evil.example/v1",
    # parser differential: urlparse says api.openai.com, requests connects
    # to evil.example (verified end to end against a local listener)
    "https://evil.example\\@api.openai.com/v1",
])
def test_the_gate_never_vets_a_host_the_client_would_not_reach(base):
    c = _cfg(OPENAI_API_KEY="k", OPENAI_BASE_URL=base)
    prepared = requests.Request("POST", f"{c.openai_base}/chat/completions"
                                ).prepare()
    actual = parse_url(prepared.url).host
    assert actual == "evil.example"
    assert c.model_host_vetted is False, (
        f"gate vetted {c.model_host!r} but requests connects to {actual!r}")


@pytest.mark.parametrize("kind", ["direct", "shl", "unknown-kind"])
def test_every_non_sample_kind_is_gated(cfg, svc, monkeypatch, kind):  # noqa: F811
    """MUTATION: gate only kind == 'fasten' -> red."""
    from careagents.models import Connection
    app, c, fake, agent_id, tenant, _ = _chat_app(cfg, svc, monkeypatch)
    with svc.session() as s:
        for row in s.query(Connection).filter_by(tenant_id=tenant).all():
            row.kind = kind
            row.consent_version = tester_terms.CONSENT_VERSION
    monkeypatch.setattr(cfg, "real_records", "off")
    monkeypatch.setattr(cfg, "openai_base", "https://llm.unvetted.example/v1")
    monkeypatch.setattr("careagents.app.time.sleep", lambda _s: None)
    calls, reads = [], []
    monkeypatch.setattr(agent.llm, "complete",
                        lambda *a, **k: calls.append(1))
    for name in ("recent_messages", "search", "read"):
        orig = getattr(fake, name)
        monkeypatch.setattr(
            fake, name,
            lambda *a, _o=orig, _n=name, **k: reads.append(_n) or _o(*a, **k))
    import careagents.worker as worker_mod
    orig_arr = worker_mod.records_arriving
    monkeypatch.setattr(
        worker_mod, "records_arriving",
        lambda *a, **k: reads.append("records_arriving") or orig_arr(*a, **k))
    body = _turn(c, agent_id, "what were my results").get_data(as_text=True)
    assert calls == [], "model called for an unapproved host"
    assert reads == [], f"records read before the refusal: {reads}"
    assert agent.UNAVAILABLE_TEXT.split(";")[0] in body
