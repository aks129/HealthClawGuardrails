"""Security attack on #1004: sign-in link rows are deleted, not stamped.

Covers replay of a deleted link, STOP across account delete and
re-signup, account delete against another account's number and links,
and the unbind paths that leave a texted number behind for account
delete to miss.

Synthetic handles only (555 numbers, example.com addresses).
"""

from __future__ import annotations

from careagents.models import ImessageHandleState, ImessageLink
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)
from tests.test_careagents_imessage import _inbound, _link_token

N = "+15550104401"
M = "+15550104402"


def _links(svc, handle):  # noqa: F811
    with svc.session() as s:
        return s.query(ImessageLink).filter_by(handle=handle).count()


def _bind_by_link(app, client, handle):
    """`handle` texts in, `client` (signed in) opens the link and says yes."""
    token = _link_token(_inbound(client, handle, "hi").get_json()["reply"])
    assert client.get(f"/link?t={token}").status_code == 302
    done = client.post("/link/done", data={"connect": "yes"})
    assert "You're connected." in done.get_data(as_text=True)
    return token


def test_a_spent_link_deleted_by_disconnect_is_not_replayable(
        cfg, svc, monkeypatch):  # noqa: F811
    app, a, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _bind_by_link(app, a, N)
    assert a.post("/api/surfaces/imessage/disconnect",
                  json={"all": True}).get_json()["removed"] == 1
    assert _links(svc, N) == 0
    # The deleted row reads exactly like a used one, to anyone.
    for c in (app.test_client(), a):
        assert c.get(f"/link?t={token}").status_code == 410
    b = app.test_client()
    _login(b, svc, monkeypatch, email="b-1004@example.com")
    assert b.get(f"/link?t={token}").status_code == 410
    assert svc.find_surface_by_handle(N) is None


def test_a_parked_link_deleted_by_stop_cannot_bind(
        cfg, svc, monkeypatch):  # noqa: F811
    app, a, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(a, N, "hi").get_json()["reply"])
    assert a.get(f"/link?t={token}").status_code == 302   # parked
    assert "Connect this phone?" in a.get("/link/done").get_data(as_text=True)
    _inbound(a, N, "STOP")                    # the row is deleted
    r = a.post("/link/done", data={"connect": "yes"})
    assert r.status_code == 200
    assert "connected" not in r.get_data(as_text=True).lower().replace(
        "isn't connected", "")
    assert svc.find_surface_by_handle(N) is None
    assert svc.imessage_opted_out(N)


def test_stop_is_honoured_after_account_delete_and_resignup(
        cfg, svc, monkeypatch):  # noqa: F811
    email = "gene@example.com"                # _chat_app's account
    app, a, *_ = _chat_app(cfg, svc, monkeypatch)
    _bind_by_link(app, a, N)
    assert _inbound(a, N, "STOP").status_code == 200
    r = a.post("/api/account/delete", json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.get_json()["deleted"] is True
    again = app.test_client()
    _login(again, svc, monkeypatch, email=email)  # the same person returns
    # A text that is not START stays unanswered and mints no link.
    r = _inbound(again, N, "hi")
    assert r.status_code == 200 and r.get_json() == {}
    assert _links(svc, N) == 0
    assert svc.imessage_opted_out(N)
    with svc.session() as s:
        assert all(N not in st.handle_key
                   for st in s.query(ImessageHandleState))


def test_account_delete_spares_another_accounts_number_and_links(
        cfg, svc, monkeypatch):  # noqa: F811
    app, a, *_ = _chat_app(cfg, svc, monkeypatch)
    _bind_by_link(app, a, N)
    # Account B, with its own assistant and number.
    b = app.test_client()
    _login(b, svc, monkeypatch, email="b-1004@example.com")
    conn = b.post("/api/connections/sample").get_json()
    b.post("/api/agents", json={"name": "Wren", "persona": "calm",
                                "connection_id": conn["id"]})
    _bind_by_link(app, b, M)
    b_acct = svc.find_surface_by_handle(M)["account_id"]
    m_links = _links(svc, M)
    assert m_links == 1
    _inbound(a, "+15550104403", "hi")
    assert a.post("/api/account/delete",
                  json={"confirm": "DELETE"}).get_json()["deleted"] is True
    assert _links(svc, N) == 0
    assert _links(svc, M) == m_links
    assert _links(svc, "+15550104403") == 1
    assert svc.find_surface_by_handle(M)["account_id"] == b_acct


def test_a_number_rebound_to_another_account_survives_the_first_delete(
        cfg, svc, monkeypatch):  # noqa: F811
    app, a, *_ = _chat_app(cfg, svc, monkeypatch)
    _bind_by_link(app, a, N)
    a.post("/api/surfaces/imessage/disconnect", json={"all": True})
    b = app.test_client()
    _login(b, svc, monkeypatch, email="b-1004@example.com")
    _bind_by_link(app, b, N)
    b_acct = svc.find_surface_by_handle(N)["account_id"]
    assert a.post("/api/account/delete",
                  json={"confirm": "DELETE"}).get_json()["deleted"] is True
    assert svc.find_surface_by_handle(N)["account_id"] == b_acct
    assert _links(svc, N) == 1


def test_account_delete_after_a_connection_delete_leaves_no_number(
        cfg, svc, monkeypatch):  # noqa: F811
    """FAILS on 20423e21. Deleting a connection deletes its assistants'
    surfaces (accounts.delete_connection) without the link rows, so the
    account no longer points at the number and delete_account cannot find
    them: the number outlives the account, which the policy says it does
    not (templates/privacy.html: "a messaging number you linked"). Nothing
    sweeps ca_imessage_links, so the row stays for good."""
    app, a, fake, agent_id, tenant, conn_id = _chat_app(cfg, svc, monkeypatch)
    _bind_by_link(app, a, N)
    r = a.delete(f"/api/connections/{conn_id}", json={"confirm": "DELETE"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert svc.find_surface_by_handle(N) is None   # unbound by the delete
    assert a.post("/api/account/delete",
                  json={"confirm": "DELETE"}).get_json()["deleted"] is True
    assert _links(svc, N) == 0, "the texted number outlived the account"


def test_account_delete_after_an_agent_delete_leaves_no_number(
        cfg, svc, monkeypatch):  # noqa: F811
    """FAILS on 20423e21: accounts.delete_agent deletes the agent's
    surfaces the same way. Driven at the service, since the route keeps a
    primary assistant (an older account's extra one is deletable there)."""
    app, a, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _bind_by_link(app, a, N)
    acct_id = svc.find_surface_by_handle(N)["account_id"]
    assert svc.delete_agent(acct_id, agent_id) is True
    assert svc.find_surface_by_handle(N) is None
    assert svc.delete_account(acct_id) is True
    assert _links(svc, N) == 0, "the texted number outlived the account"
