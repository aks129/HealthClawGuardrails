"""A finished form's review link is not a dead end (#875, patient V4).

"Your intake form is ready: <origin>/review/<agent>/<action>" is texted only
after the form is approved, and that page used to answer 404 "This form is
no longer awaiting review." Past review, the same URL now shows the form:
ready with its PDF, being prepared, or already done. 404 stays for a form
that does not exist or is not this agent's.
"""

from __future__ import annotations

import json

from careagents.healthclaw import HealthClawError
from tests.test_beta_acceptance_rows import BASE, Chain, ba
from tests.test_careagents import _chat_app, cfg, svc  # noqa: F401


def _past_review(fake, status, link="https://engine.example/f.pdf?sig=1"):
    fake.fetch_review_page = lambda t, a: (409, "not awaiting review")
    fake.action_status = lambda t, a: {
        "id": a, "status": status,
        "outcome_summary": json.dumps({"delivery_link": link})}


def test_a_completed_form_shows_its_pdf(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _past_review(fake, "completed")
    r = c.get(f"/review/{agent_id}/act-1")
    page = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Your form is ready" in page
    assert 'href="https://engine.example/f.pdf?sig=1"' in page
    assert "Open your form (PDF)" in page
    assert f'href="/chat?agent={agent_id}"' in page and "Back to chat" in page


def test_a_link_that_is_not_http_is_never_rendered(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _past_review(fake, "completed", link="javascript:alert(1)")
    page = c.get(f"/review/{agent_id}/act-1").get_data(as_text=True)
    assert "javascript:" not in page and "Open your form (PDF)" not in page


def test_a_form_still_being_made_says_so(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _past_review(fake, "executing", link=None)
    r = c.get(f"/review/{agent_id}/act-1")
    assert r.status_code == 200
    assert "being prepared" in r.get_data(as_text=True)


def test_a_form_past_review_without_a_pdf_is_already_done(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _past_review(fake, "declined", link=None)
    r = c.get(f"/review/{agent_id}/act-1")
    page = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Your form is already done." in page and "Back to chat" in page
    assert "Hmm." not in page and "your agent" not in page
    assert "no longer awaiting review" not in page


def test_someone_elses_form_is_still_a_404(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)

    def _absent(t, a):
        raise HealthClawError("not found", 404)
    fake.action_status = _absent
    assert c.get(f"/review/{agent_id}/act-9").status_code == 404
    assert c.get("/review/someone-elses-agent/act-1").status_code == 404


def test_a_status_that_cannot_be_read_is_not_a_verdict(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    fake.fetch_review_page = lambda t, a: (409, "not awaiting review")
    calls = []

    def _flaky(t, a):
        calls.append(a)
        if len(calls) > 1:        # ownership passed; the second read fails
            raise HealthClawError("status failed (503)", 503)
        return {"id": a, "status": "completed", "outcome_summary": "{}"}
    fake.action_status = _flaky
    r = c.get(f"/review/{agent_id}/act-1")
    assert r.status_code == 503
    assert "already done" not in r.get_data(as_text=True)


def test_after_approving_the_texted_link_opens_the_pdf(
        cfg, svc, monkeypatch):  # noqa: F811
    """End to end, on the real engine: approve, then open the review URL
    the text carries."""
    chain = Chain(cfg, svc, monkeypatch, allergy=True)
    action_id = chain.start_form()
    run = ba.Run(None)
    page = ba.row_form_review(chain.s, BASE, chain.agent, action_id, run)
    ba.row_form_approve(chain.s, BASE, chain.agent, action_id, page, run,
                        fetch=chain.relay.get)
    assert run.steps[-1]["status"] == "PASS", run.steps[-1]

    r = chain.s.get(f"{BASE}/review/{chain.agent}/{action_id}")
    assert r.status_code == 200, r.text[:300]
    assert "Your form is ready" in r.text
    assert "Open your form (PDF)" in r.text
    assert 'href="http' in r.text
