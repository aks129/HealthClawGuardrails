"""Accounts from before one assistant per person (spec PR #943, section 4).

Such an account can hold several assistants. Nothing makes a new one any
more, but the old ones keep working, so the tests that prove isolation
between two assistants still need two. This writes the second one the way
the old code left it: a row straight in the table, on the same person.
"""

from __future__ import annotations

from careagents.models import Agent, Connection


def legacy_agent(svc, conn_id: str, name: str = "Coach",
                 persona: str = "calm", advisor: str | None = None) -> str:
    """Another assistant on this connection's account and person."""
    with svc.session() as s:
        conn = s.get(Connection, conn_id)
        a = Agent(account_id=conn.account_id, connection_id=conn_id,
                  name=name, persona=persona, advisor=advisor, routines="",
                  person_id=conn.person_id)
        s.add(a)
        s.flush()
        return a.id
