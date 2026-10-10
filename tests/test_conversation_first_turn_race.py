"""Concurrent first turns of a new conversation create it once, without a 500.

The conversation row was found with a SELECT and, when missing, added with a
plain INSERT. Two first turns arriving together (a double-tapped send, the
same thread from two surfaces) both saw no row and both inserted it. The
loser hit the unique key `pk_cc_conversations`, the engine answered 500, and
CareAgents told the patient "message store unavailable". A forced 3-way race
on Postgres lost about 10 turns in 24.

The test forces the race rather than hoping for it: an engine hook holds
every request at its INSERT into cc_conversations until all of them have
passed the SELECT. Each request runs on its own thread, connection and
transaction.

It needs a real multi-connection database. On the default SQLite lane it uses
a file database (the in-memory default shares one connection and cannot
race). When SQLALCHEMY_DATABASE_URI points at Postgres it runs there.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

_THREADS = 3
_TENANT = "test-tenant"
_ROUTE = "/command-center/api/conversations"


@pytest.fixture
def race_app(tmp_path):
    from main import create_app
    from models import db

    uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite"):
        uri = f"sqlite:///{tmp_path / 'race.db'}"
    flask_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": uri,
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with flask_app.app_context():
        db.create_all()
        from r6.rate_limit import _rate_limits
        _rate_limits.clear()
        yield flask_app
        db.session.remove()
        db.engine.dispose()


def _hold_inserts_until_all_arrive(engine, parties):
    """Block each INSERT INTO cc_conversations until `parties` have arrived.

    Every request has already run its SELECT and seen no row by then, which
    is exactly the window the bug lives in. The timeout keeps a request that
    never reaches an INSERT (it found the row) from hanging the others.
    """
    from sqlalchemy import event

    barrier = threading.Barrier(parties)
    arrived = []

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO CC_CONVERSATIONS"):
            arrived.append(threading.get_ident())
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass

    event.listen(engine, "before_cursor_execute", _before)
    return _before, arrived


def _post(app, headers, body):
    """POST one turn; the status code, or the exception TESTING re-raised in
    place of a 500 (it then names the constraint that failed)."""
    return app.test_client().post(_ROUTE, headers=headers,
                                  json=body).status_code


def test_concurrent_first_turns_create_one_conversation(race_app):
    from sqlalchemy import event

    from models import db
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    # Unique per run: a Postgres lane keeps rows between runs.
    conversation_id = f"careagents:race-{uuid.uuid4().hex[:12]}"
    headers = {"X-Step-Up-Token": generate_step_up_token(_TENANT)}
    hook, arrived = _hold_inserts_until_all_arrive(db.engine, _THREADS)
    results: list = []
    lock = threading.Lock()

    def turn(n):
        try:
            r = _post(race_app, headers, {
                "tenant_id": _TENANT,
                "conversation_id": conversation_id,
                "agent_id": "race-agent",
                "channel": "web",
                "request_id": f"race-turn-{n}",
                "role": "user",
                "text": f"synthetic turn {n}",
            })
        except Exception as exc:  # noqa: BLE001 - TESTING re-raises a 500
            r = exc
        with lock:
            results.append(r)

    try:
        threads = [threading.Thread(target=turn, args=(n,))
                   for n in range(_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)

    # The race was actually forced: every request reached the INSERT, so
    # every one had passed the SELECT and seen no row.
    assert len(arrived) == _THREADS, arrived
    assert results == [201] * _THREADS, results

    db.session.expire_all()
    assert Conversation.query.filter_by(
        tenant_id=_TENANT, id=conversation_id).count() == 1
    assert ConversationMessage.query.filter_by(
        tenant_id=_TENANT, conversation_id=conversation_id).count() == _THREADS


def test_a_racing_turn_for_another_agent_is_still_refused(race_app):
    """The row a request finds after losing the race is checked like any
    other: a turn naming a different agent gets the 409, not a message in
    somebody else's thread."""
    from sqlalchemy import event

    from models import db
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    conversation_id = f"careagents:race-{uuid.uuid4().hex[:12]}"
    headers = {"X-Step-Up-Token": generate_step_up_token(_TENANT)}
    hook, arrived = _hold_inserts_until_all_arrive(db.engine, 2)
    results: dict = {}
    lock = threading.Lock()

    def turn(agent):
        try:
            r = _post(race_app, headers, {
                "tenant_id": _TENANT,
                "conversation_id": conversation_id,
                "agent_id": agent,
                "channel": "web",
                "role": "user",
                "text": "synthetic turn",
            })
        except Exception as exc:  # noqa: BLE001 - TESTING re-raises a 500
            r = exc
        with lock:
            results[agent] = r

    try:
        threads = [threading.Thread(target=turn, args=(a,))
                   for a in ("agent-a", "agent-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)

    assert len(arrived) == 2, arrived
    assert set(results.values()) == {201, 409}, results

    db.session.expire_all()
    rows = Conversation.query.filter_by(
        tenant_id=_TENANT, id=conversation_id).all()
    assert len(rows) == 1
    winner = rows[0].agent_id
    assert results[winner] == 201
    msgs = ConversationMessage.query.filter_by(
        tenant_id=_TENANT, conversation_id=conversation_id).all()
    assert [m.agent_id for m in msgs] == [winner]
