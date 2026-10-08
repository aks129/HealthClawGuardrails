"""The tester feedback form at /feedback, and the beta path that leads to
it: the "you're confirmed" page and email list the real next steps, and
the /beta mobile field says plainly that it is optional.

The answers are emailed to the team and kept nowhere: CareAgents stores no
PHI, and a tester may type some despite the note. Synthetic data only.
"""

from __future__ import annotations

import html
import logging
import pathlib
import re

import pytest
import requests
from sqlalchemy import text as sql

from careagents import beta_signup, feedback
from careagents.app import create_app
from careagents.models import Base
from tests.test_careagents import FakeClient, _login
from tests.test_careagents_beta_signup import _cfg, _confirm_token, _visible

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "careagents" / "templates"

QUESTIONS = (
    "Where did you get stuck or confused? Tell us which screen.",
    "What would you change, and why? The why matters most.",
    "Did anything feel wrong, or make you not trust it?",
    "Would you connect your real records to this? Why or why not?",
)
MARKER = "zebra-marker-7731"


@pytest.fixture
def made():
    from careagents.accounts import AccountService
    built = []

    def build(**env):
        cfg = _cfg(**env)
        svc = AccountService(cfg)
        a = create_app(config=cfg, client=FakeClient(), accounts=svc)
        a.config["TESTING"] = True
        built.append(svc)
        return a, svc
    yield build
    for svc in built:
        svc.engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    """Every email sent, as the JSON body handed to the provider."""
    from careagents import mail
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):
        out.append(json)

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    return out


def _answers(**over):
    body = {"stuck": "", "change": "", "trust": "", "real": ""}
    body.update(over)
    return body


def _send(client, ip="203.0.113.9", **over):
    return client.post("/feedback", json=_answers(**over),
                       headers={"X-Real-IP": ip})


def _signed_in(app, svc, monkeypatch, email="gene@example.com"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    return c


# --- the page -----------------------------------------------------------------

def test_the_page_asks_the_four_questions_in_order(made):
    app, _ = made()
    r = app.test_client().get("/feedback")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    text = _visible(body)
    at = [text.index(q) for q in QUESTIONS]
    assert at == sorted(at)
    assert "Please don't include personal health details." in text
    form = body[body.index("<form"):body.index("</form>")]
    boxes = re.findall(r"<textarea[^>]*>", form)
    assert [re.search(r'name="(\w+)"', b).group(1) for b in boxes] == [
        "stuck", "change", "trust", "real"]
    for b in boxes:
        assert f'maxlength="{feedback.ANSWER_MAX}"' in b
        assert "required" not in b
    # Nothing asks for a name, an email or a phone number.
    assert "<input" not in form.replace('type="hidden"', "")


def test_the_page_opens_signed_in_too(made, monkeypatch):
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    assert c.get("/feedback").status_code == 200


def test_the_thank_you_page_is_plain(made):
    app, _ = made()
    r = app.test_client().get("/feedback/thanks")
    assert r.status_code == 200
    text = _visible(r.get_data(as_text=True))
    assert "Thank you" in text
    assert "<form" not in r.get_data(as_text=True)


# --- submit -------------------------------------------------------------------

def test_a_signed_in_answer_is_emailed_with_reply_to(made, sent,
                                                     monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    r = _send(c, stuck="The code screen", change="Bigger text, I squint",
              trust="", real="Not yet")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "redirect": "/feedback/thanks"}
    (msg,) = sent
    assert msg["to"] == [feedback.FEEDBACK_TO] == ["contactus@healthclaw.io"]
    assert msg["subject"] == "Tester feedback — CareAgents"
    assert msg["reply_to"] == "gene@example.com"
    flat = " ".join(msg["text"].split())
    for q, a in zip(QUESTIONS, ("The code screen", "Bigger text, I squint",
                                "(no answer)", "Not yet")):
        assert f"{q} {a}" in flat
    assert "Bigger text, I squint" in msg["html"]


def test_signed_out_answers_go_without_a_reply_to(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _send(app.test_client(), trust="It felt fine")
    assert r.status_code == 200
    (msg,) = sent
    assert "reply_to" not in msg
    assert "not signed in" in msg["text"]


def test_answer_text_never_reaches_a_header(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    hostile = "x\r\nBcc: evil@example.com\r\nSubject: hi <script>alert(1)</script>"
    assert _send(c, stuck=hostile, change=hostile).status_code == 200
    (msg,) = sent
    for header in ("to", "subject", "reply_to", "from"):
        assert "evil" not in str(msg.get(header))
        assert "\n" not in str(msg.get(header))
    assert "<script>" not in msg["html"]
    assert "&lt;script&gt;" in msg["html"]


def test_at_least_one_answer_is_needed(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _send(app.test_client(), stuck="   ", change="\n\t")
    assert r.status_code == 400 and r.get_json()["error"] == "empty"
    assert sent == []


def test_a_long_answer_is_refused(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _send(app.test_client(), real="a" * (feedback.ANSWER_MAX + 1))
    assert r.status_code == 400 and r.get_json()["error"] == "too_long"
    assert sent == []
    assert _send(app.test_client(),
                 real="a" * feedback.ANSWER_MAX).status_code == 200


@pytest.mark.parametrize("body", [["a"], "a", {"stuck": ["a"]},
                                  {"stuck": {"a": 1}}, {"stuck": 5}])
def test_a_malformed_body_is_refused(made, sent, body):
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", json=body)
    assert r.status_code in (400, 415)
    assert sent == []


def test_a_form_post_is_refused_as_csrf(made, sent):
    # JSON only, as every form here: a cross-site page cannot send it
    # without a preflight.
    app, _ = made(RESEND_API_KEY="re_test")
    r = app.test_client().post("/feedback", data={"stuck": "hi"})
    assert r.status_code == 415
    r = app.test_client().post("/feedback", data='{"stuck": "hi"}',
                               content_type="text/plain")
    assert r.status_code == 415
    assert sent == []


def test_answers_are_not_kept_anywhere(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    assert _send(c, stuck=MARKER).status_code == 200
    with svc.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(sql(f'SELECT * FROM "{table.name}"')).all()
            assert MARKER not in repr(rows), table.name


@pytest.mark.parametrize("provider", ["ok", "refused", "down", "timeout"])
def test_answers_are_never_logged(made, monkeypatch, caplog, provider):
    from careagents import mail

    def fake_post(url, headers=None, json=None, timeout=None):
        if provider == "down":
            raise requests.ConnectionError("down")
        if provider == "timeout":
            raise requests.ReadTimeout("slow")

        class R:
            status_code = 200 if provider == "ok" else 500
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        _send(c, stuck=MARKER, change=MARKER, trust=MARKER, real=MARKER)
        _send(c, stuck=MARKER * 1000)            # refused as too long
    assert MARKER not in caplog.text


def test_mail_that_did_not_go_is_not_thanked(made, sent):
    # No provider key: nothing is sent, and the page must not say thanks.
    app, _ = made()
    r = _send(app.test_client(), stuck="hello")
    assert r.status_code == 503 and r.get_json()["error"] == "not_sent"
    assert sent == []


def test_real_mail_stays_off_outside_production(made, sent):
    app, _ = made(RESEND_API_KEY="re_test", CARE_ALLOW_REAL_MAIL="")
    r = _send(app.test_client(), stuck="hello")
    assert r.status_code == 503
    assert sent == []


def test_a_lost_answer_from_the_provider_still_thanks(made, monkeypatch):
    # The request went out and the answer was lost: the mail may well have
    # arrived, so asking the tester to type it all again is the worse
    # mistake. The thank-you page claims nothing about delivery.
    from careagents import mail

    def slow(*a, **k):
        raise requests.ReadTimeout("slow")
    monkeypatch.setattr(mail.requests, "post", slow)
    app, _ = made(RESEND_API_KEY="re_test")
    r = _send(app.test_client(), stuck="hello")
    assert r.status_code == 200 and r.get_json()["ok"] is True


# --- rate limits --------------------------------------------------------------

def test_an_account_is_rate_limited(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    c = _signed_in(app, svc, monkeypatch)
    for i in range(feedback.SENDS_PER_WINDOW):
        # A different address each time: the account is the key.
        assert _send(c, ip=f"203.0.113.{i + 1}",
                     stuck="hi").status_code == 200
    r = _send(c, ip="198.51.100.1", stuck="hi")
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    assert len(sent) == feedback.SENDS_PER_WINDOW
    other = _signed_in(app, svc, monkeypatch, email="ana@example.com")
    assert _send(other, stuck="hi").status_code == 200


def test_signed_out_is_rate_limited_by_address(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    for _ in range(feedback.SENDS_PER_WINDOW):
        assert _send(c, ip="203.0.113.50", stuck="hi").status_code == 200
    assert _send(c, ip="203.0.113.50", stuck="hi").status_code == 429
    assert _send(c, ip="203.0.113.51", stuck="hi").status_code == 200


# --- where a tester finds it --------------------------------------------------

def test_the_hub_links_to_the_form(made, monkeypatch):
    app, svc = made()
    body = _signed_in(app, svc, monkeypatch).get("/home").get_data(
        as_text=True)
    assert 'href="/feedback"' in body
    assert "?subject=tester" not in body


def test_the_chat_screen_links_to_the_form(made, monkeypatch):
    app, svc = made()
    c = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    page = c.get(f"/chat?agent={conn['agent_id']}")
    assert page.status_code == 200
    assert 'href="/feedback"' in page.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/", "/beta"])
def test_the_banner_links_to_the_form(made, path):
    app, _ = made()
    body = app.test_client().get(path).get_data(as_text=True)
    banner = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', body,
                       re.S).group(1)
    assert 'href="/feedback"' in banner
    assert "mailto:" not in banner


def test_no_template_sends_testers_to_a_mailto_for_feedback():
    for page in _TEMPLATES.glob("*.html"):
        assert "?subject=tester" not in page.read_text(), page.name
    assert not hasattr(beta_signup, "FEEDBACK_MAILTO")


# --- the beta path: confirmed page and email ----------------------------------

def _confirm(app, sent):
    c = app.test_client()
    c.post("/beta", json={"first_name": "Avery", "email": "avery@example.com",
                          "mobile": "", "consent": True, "ref": "",
                          "website": ""},
           headers={"X-Real-IP": "203.0.113.7"})
    token = _confirm_token(sent[-1]["text"])
    return c.post("/beta/confirm", data={"t": token})


def _quoted(step: str) -> list[str]:
    return re.findall(r'"([^"]+)"', step)


def test_every_step_names_a_label_that_is_on_the_screen():
    """Each quoted button in the steps is on the page the tester reaches
    at that step, so the list matches the real screens."""
    screens = {t: (_TEMPLATES / t).read_text()
               for t in ("landing.html", "auth.html", "home.html")}
    labels = [q for s in beta_signup.NEXT_STEPS for q in _quoted(s)]
    assert labels == ["Get started", "Email me a code", "Continue",
                      "Skip for now", "Explore with made-up records",
                      "Tell us"]
    where = {"Get started": "landing.html", "Email me a code": "auth.html",
             "Continue": "auth.html", "Skip for now": "auth.html",
             "Explore with made-up records": "home.html"}
    for label, page in where.items():
        assert label in screens[page], (label, page)
    assert 'maxlength="8"' in screens["auth.html"]
    assert any("8-digit code" in s for s in beta_signup.NEXT_STEPS)
    # "Tell us" is the link to the form on the hub and in the chat.
    assert '<a href="/feedback">Tell us</a>' in screens["home.html"]
    chat = (_TEMPLATES / "chat.html").read_text()
    assert '<a href="/feedback">Tell us</a>' in chat


def test_the_confirmed_page_lists_the_steps(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _confirm(app, sent)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    text = _visible(body)
    steps = body[body.index('<ol class="beta-steps'):]
    steps = steps[:steps.index("</ol>")]
    assert steps.count("<li") == len(beta_signup.NEXT_STEPS)
    # Tags dropped without a space, so a linked label reads as typed.
    flat = " ".join(html.unescape(re.sub(r"<[^>]+>", "", steps)).split())
    for s in beta_signup.NEXT_STEPS:
        assert s in flat
    assert '<a href="https://careagents.cloud">careagents.cloud</a>' in steps
    assert 'href="/feedback"' in body
    assert "Thanks, Avery." in text


def test_the_youre_in_email_lists_the_steps(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    _confirm(app, sent)
    (msg,) = [m for m in sent if m["subject"].startswith("You're in")]
    flat = " ".join(msg["text"].split())
    for i, s in enumerate(beta_signup.NEXT_STEPS, 1):
        assert f"{i}. {s}" in flat
    assert "http://localhost/feedback" in msg["text"]
    assert "href='http://localhost/feedback'" in msg["html"]
    assert "?subject=tester" not in msg["html"] + msg["text"]
    assert "<ol" in msg["html"]


def test_every_tester_email_links_the_form(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    app.test_client().post(
        "/beta", json={"first_name": "Avery", "email": "avery@example.com",
                       "consent": True},
        headers={"X-Real-IP": "203.0.113.7"})
    (msg,) = sent
    assert "href='http://localhost/feedback'" in msg["html"]
    assert "http://localhost/feedback" in msg["text"]
    assert "mailto:" not in msg["html"]


# --- the /beta mobile field ---------------------------------------------------

def test_the_mobile_field_says_it_is_optional_and_why(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    form = body[body.index("<form"):body.index("</form>")]
    flat = _visible(form)
    assert "Mobile number (optional)" in flat
    hint = re.search(r'<p[^>]*id="beta-mobile-hint"[^>]*>(.*?)</p>', form,
                     re.S)
    assert hint, form
    words = " ".join(_visible(hint.group(1)).split())
    assert "only if you have an iPhone" in words
    assert "text your assistant" in words
    assert "Spots are limited" in words
    assert "when yours is ready" in words
    assert "If you leave it blank" in words
    mobile = re.search(r'<input[^>]*name="mobile"[^>]*>', form).group(0)
    assert 'aria-describedby="beta-mobile-hint"' in mobile
    assert "required" not in mobile


def test_the_send_message_reply_to_is_optional(monkeypatch):
    from careagents import mail
    from careagents.config import Config
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(json)

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    cfg = Config(env={"RESEND_API_KEY": "k", "CARE_ALLOW_REAL_MAIL": "1",
                      "OPENAI_API_KEY": "k", "CARE_RP_ID": "localhost",
                      "CARE_ORIGIN": "http://localhost",
                      "HEALTHCLAW_MINT_SECRET": "m"})
    assert mail.send_message(cfg, "a@example.com", "S", "<p>h</p>",
                             "t") == mail.SENT
    assert mail.send_message(cfg, "a@example.com", "S", "<p>h</p>", "t",
                             reply_to="b@example.com") == mail.SENT
    assert "reply_to" not in seen[0]
    assert seen[1]["reply_to"] == "b@example.com"
