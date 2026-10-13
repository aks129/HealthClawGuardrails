"""QA on #940 (connect journey, #937): pins the claims its own tests left loose.

Mutation evidence for each test is in the QA comment on #940:

- "It does not switch automatically" had no direct test. An auto-switch
  added to the poll path was caught only because the switch prompt then
  disappeared; one added to the chat-settle path was not caught at all.
- The chat-settle path's "N records" (mark_synced) could be deleted with
  every test green.
- `bool(expected) and ...` in the sig check could be deleted with every test
  green: with no secret set, an empty `sig=` then matched.

Synthetic data only.
"""

from __future__ import annotations

# ruff: noqa: F811

from tests.test_careagents import (  # noqa: F401  (fixtures)
    FakeClient, _login, cfg, consented, svc)


def _app(cfg, svc):
    from careagents.app import create_app
    fake = FakeClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    return app, fake


def _reads(svc, email="gene@example.com"):
    """agent id -> the connection id it reads, straight from the store."""
    from careagents.models import Account
    with svc.session() as s:
        acct = s.query(Account).filter_by(email=email).one().id
    return acct, {a["id"]: a["connection_id"]
                  for a in svc.list_home(acct)["agents"]}


def _sample_agent_and_fasten(c):
    sample = c.post("/api/connections/sample").get_json()["id"]
    agent = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": sample}).get_json()["id"]
    created = c.post("/api/connections/fasten", json=consented()).get_json()
    tenant = created["connect_url"].rsplit("/connect/", 1)[1]
    return sample, agent, created["id"], tenant


def test_records_landing_by_poll_never_move_the_assistant(cfg, svc,
                                                          monkeypatch):
    """#940 claim 4: the switch is the person's tap, never the poll's."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    sample, agent, conn, tenant = _sample_agent_and_fasten(c)
    assert c.get(f"/api/connections/{tenant}/poll").get_json()[
        "status"] == "active"
    c.get(f"/api/connections/{tenant}/poll")
    c.get("/home")
    _, reads = _reads(svc)
    assert reads[agent] == sample


def test_records_settling_on_the_chat_page_never_move_the_assistant(
        cfg, svc, monkeypatch):
    """The chat page settles a pending connection whose records landed while
    nobody polled. That must not switch an assistant reading the sample, and
    it stores the count the card then shows."""
    app, fake = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch)
    sample, juniper, conn, tenant = _sample_agent_and_fasten(c)
    # A second assistant already on the (still connecting) real records:
    # a legacy account, since one person now has one assistant (#947).
    from tests.careagents_legacy_helpers import legacy_agent
    own = legacy_agent(svc, conn, "Rowan")
    fake.search = lambda t, rt, params=None: {"total": {
        "Condition": 2, "Observation": 5}.get(rt, 0)}
    fake.counted = 7
    assert c.get(f"/chat?agent={own}").status_code == 200
    acct, reads = _reads(svc)
    card = {x["id"]: x for x in svc.list_home(acct)["connections"]}[conn]
    assert card["status"] == "active"
    assert card["last_count"] == 7
    assert reads[juniper] == sample


def test_no_secret_and_an_empty_sig_is_not_the_patient_view(client,
                                                            monkeypatch):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_patient")
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET", raising=False)
    r = client.get("/connect/ca-0123456789?src=careagents&sig=")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="back-to-careagents"' not in html
    assert 'id="main-nav"' in html
