"""Security probes against PR #881 round 2 (4effc6a): the "No, it isn't"
disconnect, the `again` lookup on /link/done, and the Deliverer withhold.

Synthetic handles and emails only.
"""

from __future__ import annotations

import re

from careagents.models import Surface, now
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)
from tests.test_careagents_imessage import (
    OTHER_EMAIL, PHONE, _inbound, _link_token, _pair)

DAY = 86400.0
STRANGER = "+15550100199"


def _age(svc, handle=PHONE, days=90):  # noqa: F811
    with svc.session() as s:
        x = s.query(Surface).filter_by(handle=handle, kind="imessage",
                                       status="active").one()
        x.verified_at = now() - days * DAY


def _owner_of(svc, handle=PHONE):  # noqa: F811
    with svc.session() as s:
        x = s.query(Surface).filter_by(handle=handle, kind="imessage",
                                       status="active").first()
        return x.account_id if x else None


def _say_no(client, token):
    client.get(f"/link?t={token}")
    return client.post("/link/done", data={"connect": "no"})


def _other(app, svc, monkeypatch, email=OTHER_EMAIL):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    return c


def test_forwarded_link_no_from_another_account_does_not_disconnect(
        cfg, svc, monkeypatch):  # noqa: F811
    """The new holder (or anyone the link is forwarded to) says No from
    their own account: the owner's binding stays."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    owner = _owner_of(svc)
    _age(svc)
    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    page = _say_no(_other(app, svc, monkeypatch), token).get_data(
        as_text=True)
    assert "Disconnected." not in page
    assert _owner_of(svc) == owner


def test_old_link_no_after_the_handle_moved_does_not_disconnect_new_owner(
        cfg, svc, monkeypatch):  # noqa: F811
    """A link minted while A held the handle, opened by A after the handle
    moved to B: A's No must not disconnect B."""
    app, a, fake, agent_a, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(a, agent_a)
    _age(svc)
    token = _link_token(_inbound(a, PHONE, "hello").get_json()["reply"])
    _inbound(a, PHONE, "stop")                    # A's binding is gone
    b = _other(app, svc, monkeypatch)
    conn = b.post("/api/connections/sample").get_json()
    agent_b = b.post("/api/agents", json={
        "name": "Ash", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    _inbound(b, PHONE, "start")                   # opt back in
    assert _pair(b, agent_b).status_code == 200
    owner_b = _owner_of(svc)
    # STOP voided A's unused links, so the token is dead; try anyway.
    page = _say_no(a, token).get_data(as_text=True)
    assert "Disconnected." not in page
    assert _owner_of(svc) == owner_b


def test_owner_no_disconnects_only_that_handle(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _pair(c, agent_id, handle="+15550100150")     # a second phone, same acct
    _age(svc)
    token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    assert "Disconnected." in _say_no(c, token).get_data(as_text=True)
    assert _owner_of(svc) is None
    assert _owner_of(svc, "+15550100150") is not None


def _confirm_page(client, token):
    client.get(f"/link?t={token}")
    html = client.get("/link/done").get_data(as_text=True)
    return re.sub(r"\(555\) 010-\d{4}", "<handle>", html)


def test_again_lookup_reads_the_same_for_bound_elsewhere_and_unbound(
        cfg, svc, monkeypatch):  # noqa: F811
    """Another account opening a link for a handle bound to someone else
    sees exactly what it sees for a handle bound to nobody."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    _age(svc)
    bound_token = _link_token(_inbound(c, PHONE, "hello").get_json()["reply"])
    free_token = _link_token(
        _inbound(c, STRANGER, "hello").get_json()["reply"])
    viewer = _other(app, svc, monkeypatch)
    bound_page = _confirm_page(viewer, bound_token)
    viewer2 = _other(app, svc, monkeypatch)
    free_page = _confirm_page(viewer2, free_token)
    assert "Is this still your phone?" not in bound_page
    assert bound_page == free_page


def test_deliverer_withholds_a_run_whose_binding_went_due(
        cfg, monkeypatch):  # noqa: F811
    from careagents.accounts import AccountService
    from careagents.config import Config
    from tests import test_careagents_sendblue as sb

    sb_cfg = Config(env=sb._env(cfg))
    sb_svc = AccountService(sb_cfg)
    try:
        app, c, fake, fake_hc, agent_id = sb._app(
            sb_cfg, sb_svc, monkeypatch, reply="Your A1c is in range.")
        assert sb._pair(c, agent_id).status_code == 200
        assert sb._hook(c, "how is my a1c?", handle="m-q").status_code == 200
        sb._run(app)
        _age(sb_svc, handle=sb.PHONE)
        fake.sent.clear()
        sb._deliverer(app, fake).once()
        assert not any("A1c" in t for _, t in fake.sent)
        assert sb_svc.sendblue_pending() == []
    finally:
        sb_svc.engine.dispose()


def test_a_live_link_for_someone_elses_handle_no_does_not_disconnect(
        cfg, svc, monkeypatch):  # noqa: F811
    """The account check itself: A holds a live link for H while H is bound
    to B (no STOP voided it). A says No; B keeps H."""
    app, a, fake, agent_a, *_ = _chat_app(cfg, svc, monkeypatch)
    b = _other(app, svc, monkeypatch)
    conn = b.post("/api/connections/sample").get_json()
    agent_b = b.post("/api/agents", json={
        "name": "Ash", "persona": "calm",
        "connection_id": conn["id"]}).get_json()["id"]
    assert _pair(b, agent_b).status_code == 200
    owner_b = _owner_of(svc)
    token = svc.issue_imessage_link(PHONE)
    page = _say_no(a, token).get_data(as_text=True)
    assert "Disconnected." not in page
    assert _owner_of(svc) == owner_b
