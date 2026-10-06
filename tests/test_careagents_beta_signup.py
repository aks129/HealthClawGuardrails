"""The /beta page, its double opt-in request form, the confirmation and
removal links, the operator CLI and the hub's "Text your assistant" tile
(docs/briefs/2026-10-06-beta-onboarding.md, and the #874 sign-off fixes).

Synthetic data only. The advisor's real `ref` value never appears here.
"""

from __future__ import annotations

import pathlib
import re
import time

import pytest

from careagents import beta_signup
from careagents.app import create_app
from careagents.config import Config
from tests.test_careagents import FakeClient, _login

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CSS = (_ROOT / "careagents" / "static" / "careagents.css").read_text()
_JS = (_ROOT / "careagents" / "static" / "beta.js").read_text()

SENDBLUE = {"SENDBLUE_API_KEY_ID": "kid", "SENDBLUE_API_SECRET": "sec",
            "SENDBLUE_WEBHOOK_SECRET": "whs",
            "SENDBLUE_FROM_NUMBER": "+15550109000"}


def _cfg(**extra) -> Config:
    import os
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        from careagents.models import Base, make_engine
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    env = {"CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
           "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
           "HEALTHCLAW_MINT_SECRET": "mint-secret",
           # Every test that sets a provider key fakes the provider (`sent`).
           "CARE_ALLOW_REAL_MAIL": "1"}
    env.update(extra)
    return Config(env=env)


@pytest.fixture
def made():
    """(app, svc) on a fresh database. Built per test: the rate limit is
    per app."""
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
    """Every email the beta flow sends, as (to, subject, html, text)."""
    from careagents import mail
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):
        out.append((json["to"][0], json["subject"], json["html"],
                    json.get("text", "")))

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    return out


def _form(**over):
    body = {"first_name": "Avery", "email": "avery@example.com",
            "mobile": "", "consent": True, "ref": "", "website": ""}
    body.update(over)
    return body


def _post(client, ip="203.0.113.7", **over):
    return client.post("/beta", json=_form(**over),
                       headers={"X-Real-IP": ip})


def _rows(svc):
    with svc.session() as s:
        return [{c.name: getattr(r, c.name)
                 for c in beta_signup.BetaRequest.__table__.columns}
                for r in s.query(beta_signup.BetaRequest).all()]


def _row(svc, email="avery@example.com"):
    return next(r for r in _rows(svc) if r["email"] == email)


def _token(html: str) -> str:
    m = re.search(r"/beta/remove\?t=([A-Za-z0-9_-]+)", html)
    assert m, html
    return m.group(1)


def _confirm_token(text: str) -> str:
    m = re.search(r"/beta/confirm\?t=([A-Za-z0-9_-]+)", text)
    assert m, text
    return m.group(1)


def _join(app, sent, ip="203.0.113.7", **over):
    """Submit and confirm, as the address's owner would."""
    c = app.test_client()
    assert _post(c, ip=ip, **over).status_code == 200
    mine = [m for m in sent
            if m[0] == over.get("email", "avery@example.com")]
    r = c.post("/beta/confirm", data={"t": _confirm_token(mine[-1][3])})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _age_caps(svc, seconds=beta_signup.CONFIRM_CAP + 1):
    """As if every confirmation email went out `seconds` ago."""
    with svc.session() as s:
        for cap in s.query(beta_signup.BetaMailCap):
            cap.sent_at -= seconds


# --- the page ---------------------------------------------------------------

def _visible(body):
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def test_the_page_opens_with_and_without_a_ref(made):
    app, _ = made()
    c = app.test_client()
    plain = c.get("/beta")
    assert plain.status_code == 200
    text = _visible(plain.get_data(as_text=True))
    assert "CareAgents is free during the beta." in text
    assert "Someone you know" not in text
    body = c.get("/beta?ref=advisor").get_data(as_text=True)
    assert 'name="ref" value="advisor"' in body
    text = _visible(body)
    assert ("Someone you know sent you this link. CareAgents is free during "
            "the beta.") in text
    assert "advisor" not in text            # the value itself is never shown
    # A bad ref is dropped, never refused and never echoed.
    r = c.get("/beta?ref=%3Cscript%3Ealert(1)%3C/script%3E")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<script>alert" not in body and "alert(1)" not in body
    assert 'name="ref" value=""' in body
    assert "Someone you know" not in _visible(body)


def test_the_page_says_what_the_brief_says_in_plain_words(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    text = _visible(body)
    assert "tenant" not in body.lower()
    assert "fhir" not in body.lower()
    assert "synthetic" not in body.lower()
    assert "you approve anything it sends" in text
    assert "not a doctor" in text and "medical advice" in text
    step2 = body[body.index('id="beta-step-2"'):]
    step2 = step2[:step2.index("</li>")]
    assert "made-up" in step2 or "sample" in step2
    assert ("https://github.com/aks129/HealthClawGuardrails/releases/tag/"
            "demo-videos-2026-10") in body
    assert "mailto:contactus@healthclaw.io?subject=tester" in body
    assert "We keep your name, email and phone only to run the beta." in text
    assert "30 days" in text and "60 days" in text
    assert "delete your account in Settings" in text


def test_the_banner_says_made_up_and_links_feedback(made):
    app, _ = made()
    for path in ("/", "/beta"):
        body = app.test_client().get(path).get_data(as_text=True)
        banner = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', body,
                           re.S).group(1)
        assert "made-up records only" in banner
        assert 'href="mailto:contactus@healthclaw.io?subject=tester"' in banner
        assert "synthetic" not in body.lower()


def test_the_form_fields_and_the_unticked_consent_box(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    form = body[body.index("<form"):body.index("</form>")]
    assert 'name="first_name"' in form and 'name="email"' in form
    mobile = re.search(r"<input[^>]*name=\"mobile\"[^>]*>", form).group(0)
    assert "required" not in mobile
    assert ("Mobile, if you'd like to text your assistant (iPhone only, "
            "optional)") in " ".join(form.split())
    box = re.search(r"<input[^>]*name=\"consent\"[^>]*>", form).group(0)
    assert 'type="checkbox"' in box and "required" in box
    assert "checked" not in box
    assert "You can contact me about the beta" in form
    pot = re.search(r"<input[^>]*name=\"website\"[^>]*>", form).group(0)
    assert 'tabindex="-1"' in pot and 'autocomplete="off"' in pot
    # The error sits right above Send.
    assert re.search(r'id="beta-err"[^>]*>\s*</div>\s*<button[^>]*'
                     r'type="submit"', form)


def test_the_page_reads_at_phone_width():
    block = _CSS[_CSS.index(".beta-page {"):]
    block = block[:block.index("}")]
    size = int(re.search(r"font-size:\s*(\d+)px", block).group(1))
    assert size >= 16
    assert "max-width" in block
    assert "overflow-wrap: anywhere" in _CSS[_CSS.index(".beta-page"):]


def test_the_browser_refuses_while_the_box_is_unticked():
    assert "consent.checked" in _JS
    assert 'headers: { "Content-Type": "application/json" }' in _JS
    assert "scrollIntoView" in _JS
    assert '"Thanks, "' in _JS and "Check your email to confirm." in _JS
    assert "If it isn't there in a few minutes, check spam." in _JS
    assert "If you already asked today, use the email we sent earlier." \
        in _JS
    assert "innerHTML" not in _JS


# --- submit: double opt-in ----------------------------------------------------

def test_a_submit_only_asks_the_address_to_confirm(made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_BETA_NOTIFY_EMAIL="owner@example.org")
    r = _post(app.test_client(), ref="advisor", mobile="(555) 010-0101")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    row = _row(svc)
    assert row["status"] == "pending" and row["ref"] == "advisor"
    assert row["mobile"] == "+15550100101" and row["confirmed_at"] is None
    (to, subject, html, text), = sent        # no owner notice, no welcome
    assert to == "avery@example.com"
    assert subject.startswith("Confirm you asked to test CareAgents")
    token = _confirm_token(text)
    assert row["confirm_hash"] == beta_signup.hash_token(token)
    assert token not in str(row)
    assert row["removal_hash"] == beta_signup.hash_token(_token(html))
    assert "Avery" not in html + text


def test_opening_the_confirm_link_only_asks(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    token = _confirm_token(sent[0][3])
    c = app.test_client()
    for _ in range(2):
        r = c.get(f"/beta/confirm?t={token}")
        assert r.status_code == 200
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert 'method="post"' in r.get_data(as_text=True)
    assert _row(svc)["status"] == "pending"


def test_confirming_joins_once_and_says_youre_in(made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_BETA_NOTIFY_EMAIL="owner@example.org")
    _post(app.test_client(), ref="advisor")
    token = _confirm_token(sent[0][3])
    c = app.test_client()
    r = c.post("/beta/confirm", data={"t": token})
    assert r.status_code == 200
    page = _visible(r.get_data(as_text=True))
    assert ("Thanks, Avery. We sent an email to avery@example.com. If it "
            "isn't there in a few minutes, check spam.") in page
    row = _row(svc)
    assert row["status"] == "new" and row["confirmed_at"] is not None
    assert row["confirm_hash"] is None
    welcome = [m for m in sent if m[1].startswith("You're in")]
    (to, _, html, text), = welcome
    assert to == "avery@example.com"
    flat = " ".join(text.split())
    assert ("Hi Avery, thanks for helping test CareAgents. Open "
            "careagents.cloud and sign up with this email. You'll use "
            "made-up records, not your own, and CareAgents is not a "
            "doctor. CareAgents is built by HealthClaw, so questions go to "
            "contactus@healthclaw.io.") in flat
    assert "href='https://careagents.cloud'" in html
    assert "mailto:contactus@healthclaw.io?subject=tester" in html
    assert row["removal_hash"] == beta_signup.hash_token(_token(html))
    # Spent.
    assert c.post("/beta/confirm", data={"t": token}).status_code == 410
    assert c.get(f"/beta/confirm?t={token}").status_code == 410


def test_a_lapsed_confirm_link_is_gone(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    token = _confirm_token(sent[0][3])
    with svc.session() as s:
        s.get(beta_signup.BetaRequest, "avery@example.com"
              ).confirm_expires_at = time.time() - 1
    c = app.test_client()
    assert c.get(f"/beta/confirm?t={token}").status_code == 410
    assert c.post("/beta/confirm", data={"t": token}).status_code == 410
    assert _row(svc)["status"] == "pending"


@pytest.mark.parametrize("t", ["", "nope", "x" * 200])
def test_a_bad_confirm_token_is_gone(made, t):
    app, _ = made()
    c = app.test_client()
    assert c.get(f"/beta/confirm?t={t}").status_code == 410
    assert c.post("/beta/confirm", data={"t": t}).status_code == 410


def test_the_owner_hears_only_of_confirmed_requests(made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_BETA_NOTIFY_EMAIL="owner@example.org")
    _post(app.test_client())
    assert not [m for m in sent if m[0] == "owner@example.org"]
    sent.clear()
    _post(app.test_client(), email="b@example.com")
    app.test_client().post("/beta/confirm",
                           data={"t": _confirm_token(sent[0][3])})
    owner = [m for m in sent if m[0] == "owner@example.org"]
    assert len(owner) == 1


def test_the_owner_email_has_the_name_and_the_masked_domain_only(made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_BETA_NOTIFY_EMAIL="owner@example.org")
    _join(app, sent, first_name="<b>Avery</b>", mobile="+15550100101",
          ref="advisor")
    (_, subject, html, text), = [m for m in sent
                                 if m[0] == "owner@example.org"]
    for part in (subject, html, text):
        assert "avery@example.com" not in part
        assert "example.com" not in part
        assert "5550100101" not in part and "0101" not in part
        assert "<b>" not in part
    assert "&lt;b&gt;Avery&lt;/b&gt;" in html
    assert "e***.com" in text
    assert "mobile: yes" in text and "ref: advisor" in text


def test_no_owner_address_means_no_owner_email(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    assert {m[0] for m in sent} == {"avery@example.com"}


def test_the_consent_box_is_checked_on_the_server(made):
    app, svc = made()
    c = app.test_client()
    for i, value in enumerate((False, None, "", "false", 0, "on", 1)):
        r = _post(c, ip=f"198.51.100.{i}", consent=value)
        assert r.status_code == 400, value
        assert r.get_json()["error"] == "consent_required"
    body = _form()
    del body["consent"]
    assert c.post("/beta", json=body).status_code == 400
    assert _rows(svc) == []


@pytest.mark.parametrize("raw,kept", [
    ("advisor", "advisor"),
    ("a-1", "a-1"),
    ("x" * 32, "x" * 32),
    ("x" * 33, None),
    ("Advisor", None),
    ("adv isor", None),
    ("adv_isor", None),
    ("<b>", None),
    ("advisor\n", None),
    ("", None),
    (7, None),
    (["advisor"], None),
    (None, None),
])
def test_ref_is_kept_only_when_it_is_plain(made, raw, kept):
    app, svc = made()
    r = _post(app.test_client(), ref=raw)
    assert r.status_code == 200
    assert _rows(svc)[0]["ref"] == kept


def test_clean_ref_is_a_full_match():
    assert beta_signup.clean_ref("advisor") == "advisor"
    assert beta_signup.clean_ref("advisor!") is None
    assert beta_signup.clean_ref("!advisor") is None


# --- the per-address cap -----------------------------------------------------

def test_one_confirmation_email_per_address_per_day(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c, ip="198.51.100.1")
    first = _row(svc)
    _post(c, ip="198.51.100.2", first_name="Mallory", mobile="+15550100666")
    assert len(sent) == 1
    assert _row(svc) == first               # nothing written either
    _age_caps(svc)
    _post(c, ip="198.51.100.3", first_name="Ava")
    assert len(sent) == 2
    assert _row(svc)["first_name"] == "Ava"   # still pending: refreshed


def test_the_cap_holds_across_processes(made, sent):
    """The cap is in the database: a second app (a second worker, or a
    restart) over the same database is capped too."""
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    other = create_app(config=app.extensions["careagents_runtime"]["config"],
                       client=FakeClient(), accounts=svc)
    other.config["TESTING"] = True
    assert _post(other.test_client(), ip="198.51.100.9").status_code == 200
    assert len(sent) == 1


def test_claim_mail_is_once_a_day():
    from careagents.accounts import AccountService
    svc = AccountService(_cfg())
    try:
        t0 = 1_000_000.0
        assert beta_signup.claim_mail(svc.session, "a@example.com", t0)
        assert not beta_signup.claim_mail(svc.session, "a@example.com",
                                          t0 + 60)
        assert beta_signup.claim_mail(svc.session, "b@example.com", t0 + 60)
        assert beta_signup.claim_mail(svc.session, "a@example.com",
                                      t0 + beta_signup.CONFIRM_CAP)
    finally:
        svc.engine.dispose()


# --- a confirmed request is not overwritten ----------------------------------

def test_a_confirmed_request_changes_only_when_the_address_confirms(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    _age_caps(svc)
    sent.clear()
    _post(app.test_client(), ip="198.51.100.5", first_name="Ava",
          mobile="+15550100199")
    row = _row(svc)
    assert (row["first_name"], row["mobile"]) == ("Avery", "+15550100101")
    assert row["status"] == "new"
    (to, subject, html, text), = sent
    assert "change" in subject.lower()
    app.test_client().post("/beta/confirm",
                           data={"t": _confirm_token(text)})
    row = _row(svc)
    assert (row["first_name"], row["mobile"]) == ("Ava", "+15550100199")
    assert row["status"] == "new"


def test_asking_again_with_nothing_new_sends_nothing(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    _age_caps(svc)
    sent.clear()
    _post(app.test_client(), ip="198.51.100.5")
    assert sent == []


def test_a_confirmed_change_never_puts_a_mobile_on_an_added_row(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    beta_signup.mark(svc.session, "avery@example.com", "added")
    _age_caps(svc)
    sent.clear()
    _post(app.test_client(), ip="198.51.100.5", mobile="+15550100199")
    app.test_client().post("/beta/confirm",
                           data={"t": _confirm_token(sent[0][3])})
    row = _row(svc)
    assert row["status"] == "added" and row["mobile"] is None


def test_after_removal_a_new_submit_needs_confirming_again(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    c = app.test_client()
    c.post("/beta/remove", data={"t": _token(sent[-1][2])})
    assert _rows(svc) == []
    _age_caps(svc)
    sent.clear()
    _post(c, ip="198.51.100.5")
    assert _row(svc)["status"] == "pending"
    assert [m[1].split(" —")[0] for m in sent] == [
        "Confirm you asked to test CareAgents"]


def test_after_the_owner_removed_it_a_submit_starts_pending(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    beta_signup.mark(svc.session, "avery@example.com", "removed")
    _age_caps(svc)
    _post(app.test_client(), ip="198.51.100.5")
    assert _row(svc)["status"] == "pending"


# --- validation ---------------------------------------------------------------

def test_a_filled_honeypot_answers_the_same_and_keeps_nothing(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    r = _post(app.test_client(), website="http://spam.example")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert _rows(svc) == []
    assert sent == []


def test_a_form_post_is_refused_as_cross_site(made):
    app, svc = made()
    r = app.test_client().post("/beta", data=_form(consent="on"))
    assert r.status_code == 415
    assert _rows(svc) == []


@pytest.mark.parametrize("over,error", [
    ({"email": "not-an-email"}, "email"),
    ({"email": "a@b"}, "email"),
    ({"email": "x" * 243 + "@example.com"}, "email"),
    ({"email": "a@b@example.com"}, "email"),
    ({"email": "a,b@example.com"}, "email"),
    ({"email": "a b@example.com"}, "email"),
    ({"email": "a​@example.com"}, "email"),
    ({"email": "nul\x00@example.com"}, "email"),
    ({"first_name": ""}, "first_name"),
    ({"first_name": "   "}, "first_name"),
    ({"first_name": "x" * 41}, "first_name"),
    ({"first_name": "A\x00"}, "first_name"),
    ({"first_name": "A\x1b[2J"}, "first_name"),
    ({"first_name": "A\nB"}, "first_name"),
    ({"first_name": "A‮B"}, "first_name"),
    ({"mobile": "call me"}, "mobile"),
    ({"mobile": "12"}, "mobile"),
    ({"mobile": "1" * 16}, "mobile"),
    ({"mobile": "me@icloud.com"}, "mobile"),
])
def test_bad_fields_are_refused_with_a_reason(made, over, error):
    app, svc = made()
    r = _post(app.test_client(), **over)
    assert r.status_code == 400
    assert r.get_json()["error"] == error
    assert _rows(svc) == []


def test_an_email_of_254_characters_is_accepted(made):
    app, svc = made()
    email = "x" * 242 + "@example.com"
    assert len(email) == 254
    assert _post(app.test_client(), email=email).status_code == 200


def test_names_in_other_scripts_are_fine(made):
    app, svc = made()
    assert _post(app.test_client(), first_name="José").status_code == 200


@pytest.mark.parametrize("typed,stored", [
    ("(555) 010-0101", "+15550100101"),
    ("555.010.0101", "+15550100101"),
    ("+1 555 010 0101", "+15550100101"),
    ("1 555 010 0101", "+15550100101"),
])
def test_a_mobile_is_read_the_way_imessage_reads_it(made, typed, stored):
    app, svc = made()
    _post(app.test_client(), mobile=typed)
    assert _row(svc)["mobile"] == stored


# --- the rate limit -----------------------------------------------------------

def test_submits_are_limited_per_ip(made):
    app, svc = made()
    c = app.test_client()
    for i in range(beta_signup.SUBMITS_PER_WINDOW):
        assert _post(c, email=f"t{i}@example.com").status_code == 200
    assert _post(c, email="late@example.com").status_code == 429
    assert _post(c, ip="198.51.100.4",
                 email="other@example.com").status_code == 200


def test_x_forwarded_for_is_never_read(made):
    """The key is X-Real-IP, which Railway's edge sets, or the peer.
    X-Forwarded-For, private or public, opens no bucket of its own."""
    app, _ = made()
    c = app.test_client()
    codes = [c.post("/beta", json=_form(email=f"t{i}@example.com"),
                    headers={"X-Forwarded-For": f"203.0.113.{i}"}
                    ).status_code for i in range(10)]
    assert codes.count(429) == 10 - beta_signup.SUBMITS_PER_WINDOW


def test_a_garbled_x_real_ip_falls_back_to_the_peer(made):
    app, _ = made()
    c = app.test_client()
    codes = [c.post("/beta", json=_form(email=f"t{i}@example.com"),
                    headers={"X-Real-IP": f"not-an-ip-{i}"}).status_code
             for i in range(10)]
    assert codes.count(429) == 10 - beta_signup.SUBMITS_PER_WINDOW


# --- the iMessage spots and the queue -----------------------------------------

def _fill_spots(app, sent, n=beta_signup.IMESSAGE_SPOTS):
    for i in range(n):
        _join(app, sent, ip=f"198.51.100.{i}", email=f"t{i}@example.com",
              mobile=f"+1555010{i:04d}")


def test_only_confirmed_requests_take_a_spot(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    for i in range(beta_signup.IMESSAGE_SPOTS):     # pending, never confirmed
        _post(c, ip=f"198.51.100.{100 + i}", email=f"p{i}@example.com",
              mobile=f"+1555011{i:04d}")
    _join(app, sent, ip="198.51.100.50", mobile="+15550109999")
    assert _row(svc)["status"] == "new"


def test_the_eleventh_confirmed_mobile_waits(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _fill_spots(app, sent)
    _join(app, sent, ip="198.51.100.50", email="nomobile@example.com")
    sent.clear()
    _join(app, sent, ip="198.51.100.51", email="late@example.com",
          mobile="+15550109999")
    assert _row(svc, "nomobile@example.com")["status"] == "new"
    assert _row(svc, "late@example.com")["status"] == "waitlist"
    welcome = [m for m in sent if m[1].startswith("You're in")][0]
    assert ("iMessage is full for now. We'll email you when a spot opens. "
            "The web app works today.") in welcome[3]


def test_a_spot_counts_after_its_mobile_is_deleted(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _fill_spots(app, sent)
    for i in range(beta_signup.IMESSAGE_SPOTS):
        beta_signup.mark(svc.session, f"t{i}@example.com", "added")
    _join(app, sent, ip="198.51.100.51", email="late@example.com",
          mobile="+15550109999")
    assert _row(svc, "late@example.com")["status"] == "waitlist"


def _queue(app, sent, svc, names=("w1", "w2")):
    _fill_spots(app, sent)
    for i, name in enumerate(names):
        _join(app, sent, ip=f"198.51.100.{60 + i}",
              email=f"{name}@example.com", mobile=f"+1555012{i:04d}")
    for name in names:
        assert _row(svc, f"{name}@example.com")["status"] == "waitlist"


def test_the_oldest_waitlisted_request_gets_a_freed_spot(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    # w2 was created first, but w1 confirmed first: confirmation order.
    with svc.session() as s:
        s.get(beta_signup.BetaRequest, "w2@example.com").created_at = 1.0
    promoted = beta_signup.mark(svc.session, "t3@example.com", "removed")
    assert promoted == ["w1@example.com"]
    assert _row(svc, "w1@example.com")["status"] == "new"
    assert _row(svc, "w2@example.com")["status"] == "waitlist"


def test_a_removal_link_frees_a_spot_for_the_queue(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    t0_welcome = [m for m in sent if m[0] == "t0@example.com"][-1]
    app.test_client().post("/beta/remove", data={"t": _token(t0_welcome[2])})
    assert _row(svc, "w1@example.com")["status"] == "new"


def test_deleting_an_account_frees_its_spot(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch, email="t0@example.com")
    assert c.post("/api/account/delete",
                  json={"confirm": "DELETE"}).status_code == 200
    assert _row(svc, "w1@example.com")["status"] == "new"


def test_the_list_shows_the_queue_in_order(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    w1 = next(x for x in out.splitlines() if "w1@example.com" in x)
    w2 = next(x for x in out.splitlines() if "w2@example.com" in x)
    assert "waitlist #1" in w1 and "waitlist #2" in w2
    assert "+15550120000" in w1               # in full: the owner acts on it


# --- removal ----------------------------------------------------------------

def test_opening_the_removal_link_only_asks(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    token = _token(sent[-1][2])
    r = app.test_client().get(f"/beta/remove?t={token}")
    assert r.status_code == 200
    assert r.headers["Referrer-Policy"] == "no-referrer"
    body = r.get_data(as_text=True)
    assert "Remove my request" in body and 'method="post"' in body
    assert "Keep my request" in body
    assert len(_rows(svc)) == 1


def test_the_removal_token_works_once(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    token = _token(sent[-1][2])
    c = app.test_client()
    r = c.post("/beta/remove", data={"t": token})
    assert r.status_code == 200
    assert "removed" in r.get_data(as_text=True)
    assert _rows(svc) == []
    assert c.post("/beta/remove", data={"t": token}).status_code == 410
    assert c.get(f"/beta/remove?t={token}").status_code == 410


def test_the_welcome_email_replaces_the_earlier_removal_link(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent)
    first, second = (_token(m[2]) for m in sent)
    assert first != second
    c = app.test_client()
    assert c.post("/beta/remove", data={"t": first}).status_code == 410
    assert c.post("/beta/remove", data={"t": second}).status_code == 200


@pytest.mark.parametrize("t", ["", "nope", "x" * 200])
def test_a_bad_removal_token_is_gone(made, t):
    app, _ = made()
    c = app.test_client()
    assert c.get(f"/beta/remove?t={t}").status_code == 410
    assert c.post("/beta/remove", data={"t": t}).status_code == 410


# --- delete_account ---------------------------------------------------------

def test_deleting_the_account_deletes_the_beta_request(made, monkeypatch):
    app, svc = made()
    c = app.test_client()
    _post(c, email="gene@example.com")
    _post(c, email="other@example.com")
    _login(c, svc, monkeypatch, email="gene@example.com")
    r = c.post("/api/account/delete", json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.get_json()["deleted"] is True
    assert [row["email"] for row in _rows(svc)] == ["other@example.com"]


# --- CLI --------------------------------------------------------------------

def test_list_shows_confirmed_requests_and_the_mobile_while_actionable(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, email="a@example.com", mobile="+15550100101",
          ref="advisor")
    _join(app, sent, ip="198.51.100.2", email="b@example.com",
          mobile="+15550100102")
    beta_signup.mark(svc.session, "b@example.com", "active")
    _post(app.test_client(), ip="198.51.100.3", email="p@example.com")
    run = app.test_cli_runner()
    out = run.invoke(args=["beta-requests", "list"]).output
    a_line = next(x for x in out.splitlines() if "a@example.com" in x)
    b_line = next(x for x in out.splitlines() if "b@example.com" in x)
    assert "+15550100101" in a_line and "advisor" in a_line
    assert " new" in a_line and "Avery" in a_line
    assert "+15550100102" not in b_line and "0102" in b_line
    assert "p@example.com" not in out                 # not confirmed
    pending = run.invoke(args=["beta-requests", "list", "--pending"]).output
    assert "p@example.com" in pending and "a@example.com" not in pending


def test_list_with_nothing_says_so(made):
    app, _ = made()
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    assert "no requests" in out


def test_list_escapes_what_a_visitor_typed(made):
    """Defence in depth behind the 400: a row that holds controls (put
    there some other way) prints escaped, one line per row."""
    app, svc = made()
    with svc.session() as s:
        s.add(beta_signup.BetaRequest(
            email="b\x1b[2J@example.net", status="new",
            first_name="B\n2026-01-01  Fake  ok@x.co  -  active",
            created_at=time.time(), updated_at=time.time()))
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    assert "\x1b" not in out
    assert len(out.splitlines()) == 2
    assert "\\x1b[2J" in out and "\\n2026" in out


def test_mark_added_deletes_the_mobile_and_sends_the_text_hi_email(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test", **SENDBLUE)
    _join(app, sent, mobile="+15550100101")
    sent.clear()
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "Avery@Example.com", "added"])
    assert r.exit_code == 0, r.output
    row = _row(svc)
    assert row["status"] == "added" and row["mobile"] is None
    (to, _, html, text), = sent
    assert to == "avery@example.com"
    assert ("Text hi to +1 555-010-9000. You'll get a link back to sign in, "
            "then your assistant answers there.") in text
    assert ("Your texts pass through a texting company we use, so only use "
            "the made-up records here.") in text
    assert "<a href='sms:+15550109000'>+1 555-010-9000</a>" in html
    assert "mailto:contactus@healthclaw.io?subject=tester" in html
    assert row["removal_hash"] == beta_signup.hash_token(_token(html))


def test_mark_added_with_sendblue_off_sends_nothing_and_says_so(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    sent.clear()
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "avery@example.com", "added"])
    assert r.exit_code == 0
    assert "Sendblue is off" in r.output
    assert sent == []
    assert _row(svc)["status"] == "added"


@pytest.mark.parametrize("status", ["new", "waitlist", "active", "removed"])
def test_mark_other_statuses(made, sent, status):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "avery@example.com", status])
    assert r.exit_code == 0, r.output
    assert _row(svc)["status"] == status


def test_mark_refuses_an_unknown_email_or_status(made):
    app, _ = made()
    _post(app.test_client())
    run = app.test_cli_runner()
    assert run.invoke(args=["beta-requests", "mark", "no@example.com",
                            "added"]).exit_code != 0
    for bad in ("banana", "pending"):
        assert run.invoke(args=["beta-requests", "mark", "avery@example.com",
                                bad]).exit_code != 0


def test_purge_keeps_what_the_page_promises(made, sent, monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    for i, email in enumerate(("old@example.com", "kept@example.com",
                               "month@example.com", "gene@example.com")):
        _join(app, sent, ip=f"198.51.100.{i}", email=email,
              mobile=None if email.startswith("gene") else f"+155501001{i:02d}")
    _post(app.test_client(), ip="198.51.100.9", email="pend@example.com")
    _post(app.test_client(), ip="198.51.100.8", email="fresh@example.com")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="gene@example.com")
    day = 86400
    with svc.session() as s:
        q = s.query(beta_signup.BetaRequest)
        for email, age in (("old@example.com", 61), ("month@example.com", 31),
                           ("gene@example.com", 90), ("pend@example.com", 8)):
            row = q.filter_by(email=email).one()
            row.created_at = row.updated_at = time.time() - age * day
            if row.mobile:
                row.mobile_given_at = time.time() - age * day
    _age_caps(svc, 2 * day)
    r = app.test_cli_runner().invoke(args=["beta-requests", "purge"])
    assert r.exit_code == 0, r.output
    by = {row["email"]: row for row in _rows(svc)}
    assert "old@example.com" not in by            # no account, 60 days
    assert "pend@example.com" not in by           # unconfirmed, 7 days
    assert "fresh@example.com" in by              # unconfirmed, today
    assert "gene@example.com" in by               # an account holder's
    assert by["month@example.com"]["mobile"] is None
    assert by["kept@example.com"]["mobile"] == "+15550100101"
    assert "2 request" in r.output and "1 mobile" in r.output
    with svc.session() as s:
        assert s.query(beta_signup.BetaMailCap).count() == 0


# --- hub ----------------------------------------------------------------------

def _hub(app, svc, monkeypatch, kinds, request_status="added"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email="gene@example.com")
    with c.session_transaction() as s:
        aid = s["account_id"]
    for kind in kinds:
        svc.add_connection(aid, kind, f"t-{kind}", kind.title(),
                           consent_version="2026-08-01")
    if request_status:
        with svc.session() as s:
            s.add(beta_signup.BetaRequest(
                email="gene@example.com", first_name="Gene",
                status=request_status, created_at=time.time(),
                updated_at=time.time(), confirmed_at=time.time()))
    return c.get("/home").get_data(as_text=True)


@pytest.mark.parametrize("status", ["added", "active"])
def test_the_text_tile_shows_for_an_added_tester_on_sample_records(
        made, monkeypatch, status):
    app, svc = made(**SENDBLUE)
    body = _hub(app, svc, monkeypatch, ["sample"], request_status=status)
    tile = body[body.index('id="text-tile"'):]
    tile = tile[:tile.index("</section>")]
    assert "Text your assistant" in tile
    assert ('Text hi to <a href="sms:+15550109000">+1 555-010-9000</a>. '
            "You'll get a link back to sign in, then your assistant answers "
            "there.") in " ".join(tile.split())
    assert ("Your texts pass through a texting company we use, so only use "
            "the made-up records here.") in tile
    assert "code" not in tile.lower()


@pytest.mark.parametrize("status", [None, "pending", "new", "waitlist",
                                    "removed"])
def test_the_text_tile_is_hidden_from_anyone_not_added(made, monkeypatch,
                                                       status):
    """Only the Sendblue sandbox's contacts can reach the line."""
    app, svc = made(**SENDBLUE)
    body = _hub(app, svc, monkeypatch, ["sample"], request_status=status)
    assert 'id="text-tile"' not in body


def test_the_text_tile_is_hidden_with_a_real_connection(made, monkeypatch):
    app, svc = made(**SENDBLUE)
    body = _hub(app, svc, monkeypatch, ["sample", "fasten"])
    assert 'id="text-tile"' not in body


def test_the_text_tile_is_hidden_while_sendblue_is_off(made, monkeypatch):
    app, svc = made(CARE_IMESSAGE_HANDLE="+15550109000")
    body = _hub(app, svc, monkeypatch, ["sample"])
    assert 'id="text-tile"' not in body


def test_the_hub_links_testers_to_feedback(made, monkeypatch):
    app, svc = made()
    body = _hub(app, svc, monkeypatch, [])
    assert "mailto:contactus@healthclaw.io?subject=tester" in body


def test_the_hub_banner_says_made_up_records(made, monkeypatch):
    app, svc = made()
    body = _hub(app, svc, monkeypatch, ["sample"])
    banner = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', body,
                       re.S).group(1)
    assert "made-up records" in banner


# --- round 3: mailboxes, spots, lapsed changes, promotions -------------------

@pytest.mark.parametrize("typed,box", [
    ("Victim@Example.net", "victim@example.net"),
    ("victim+beta@example.net", "victim@example.net"),
    ("victim+a+b@example.net", "victim@example.net"),
    ("v.i.c.t.i.m@example.net", "v.i.c.t.i.m@example.net"),
    ("V.ictim+x@gmail.com", "victim@gmail.com"),
    ("victim@googlemail.com", "victim@gmail.com"),
    ("vic.tim+1@GoogleMail.com", "victim@gmail.com"),
])
def test_mailbox_folds_aliases_of_one_inbox(typed, box):
    assert beta_signup.mailbox(typed) == box


def test_the_cap_is_keyed_on_the_mailbox_hash(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c, ip="198.51.100.1", email="v.ictim+1@gmail.com")
    _post(c, ip="198.51.100.2", email="victim@googlemail.com")
    assert len(sent) == 1
    with svc.session() as s:
        keys = [k for (k,) in s.query(beta_signup.BetaMailCap.email_hash)]
    assert keys == [beta_signup.hash_token("victim@gmail.com")]


def test_one_spot_per_mailbox_with_a_note(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, ip="198.51.100.1", email="pat@example.com",
          mobile="+15550100101")
    _age_caps(svc)
    _join(app, sent, ip="198.51.100.2", email="pat+2@example.com",
          mobile="+15550100102")
    assert _row(svc, "pat+2@example.com")["status"] == "waitlist"
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    line = next(x for x in out.splitlines() if "pat+2@example.com" in x)
    assert "same mailbox" in line


def test_one_spot_per_mobile_with_a_note(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, ip="198.51.100.1", email="a@example.com",
          mobile="+15550100101")
    _join(app, sent, ip="198.51.100.2", email="b@example.com",
          mobile="(555) 010-0101")
    assert _row(svc, "b@example.com")["status"] == "waitlist"
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    line = next(x for x in out.splitlines() if "b@example.com" in x)
    assert "same mobile" in line


def test_a_duplicate_is_not_promoted_while_its_twin_holds_a_spot(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, ip="198.51.100.1", email="a@example.com",
          mobile="+15550100101")
    _join(app, sent, ip="198.51.100.2", email="b@example.com",
          mobile="+15550100101")
    with svc.session() as s:
        assert beta_signup.promote(s) == []
    assert _row(svc, "b@example.com")["status"] == "waitlist"


def test_the_confirm_page_shows_what_is_confirmed(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    _post(c, first_name="Vic", mobile="+15550100123")
    page = _visible(c.get(
        f"/beta/confirm?t={_confirm_token(sent[0][3])}").get_data(
            as_text=True))
    assert "A new request" in page
    assert "Vic" in page and "Mobile: ending in 0123" in page
    assert "a mobile" not in page
    assert "+15550100123" not in page
    assert "Not right? Close this page and nothing changes." in page


def test_the_confirm_page_shows_a_change_and_no_mobile(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100123")
    _age_caps(svc)
    sent.clear()
    c = app.test_client()
    _post(c, ip="198.51.100.5", first_name="Ava")
    page = _visible(c.get(
        f"/beta/confirm?t={_confirm_token(sent[0][3])}").get_data(
            as_text=True))
    assert "A change to your request" in page and "Ava" in page
    # The number on file stays; the change carries none.
    assert "no new mobile" in page


def test_confirmed_says_what_to_do_next(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    r = _join(app, sent)
    assert ("Next: open careagents.cloud and sign up with this email."
            in _visible(r.get_data(as_text=True)))


def test_keep_my_request_lands_on_a_plain_page(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    c = app.test_client()
    ask = c.get(f"/beta/remove?t={_token(sent[0][2])}").get_data(as_text=True)
    href = re.search(r'href="([^"]+)"[^>]*>Keep my request', ask).group(1)
    assert href == "/beta/kept"
    page = c.get(href)
    assert page.status_code == 200
    text = _visible(page.get_data(as_text=True))
    assert "Your request is kept. Nothing changed." in text
    assert 'href="/auth"' in page.get_data(as_text=True)
    assert len(_rows(svc)) == 1


def _lapse(svc, email="avery@example.com"):
    with svc.session() as s:
        s.get(beta_signup.BetaRequest, email).confirm_expires_at = (
            time.time() - 1)


def test_a_lapsed_change_is_cleared_on_the_next_read(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    _age_caps(svc)
    _post(app.test_client(), ip="198.51.100.5", first_name="Mal",
          mobile="+15550100666")
    assert _row(svc)["pending_mobile"] == "+15550100666"
    _lapse(svc)
    app.test_cli_runner().invoke(args=["beta-requests", "list"])
    row = _row(svc)
    assert row["pending_mobile"] is None and row["pending_first_name"] is None
    assert row["confirm_hash"] is None
    assert row["mobile"] == "+15550100101"


def test_a_lapsed_unconfirmed_mobile_is_cleared_by_purge(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client(), mobile="+15550100666")
    _lapse(svc)
    beta_signup.purge(svc.session)
    row = _row(svc)
    assert row["mobile"] is None and row["status"] == "pending"


def test_a_live_change_is_kept(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, mobile="+15550100101")
    _age_caps(svc)
    _post(app.test_client(), ip="198.51.100.5", mobile="+15550100666")
    beta_signup.purge(svc.session)
    assert _row(svc)["pending_mobile"] == "+15550100666"


def test_a_promoted_tester_is_emailed_and_logged_masked(made, sent, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="careagents.beta_signup")
    app, svc = made(RESEND_API_KEY="re_test", **SENDBLUE)
    _queue(app, sent, svc)
    t0 = [m for m in sent if m[0] == "t0@example.com"][-1]
    sent.clear()
    app.test_client().post("/beta/remove", data={"t": _token(t0[2])})
    (to, subject, html, text), = sent
    assert to == "w1@example.com"
    flat = " ".join(text.split())
    assert ("Hi Avery, an iMessage spot opened for you. We'll email you "
            "again within a day once your number is ready to text. "
            "Meanwhile the web app works: https://careagents.cloud") in flat
    assert "href='https://careagents.cloud'" in html
    # Not added to Sendblue yet: nobody is told to text a line that won't
    # answer. That waits for `mark ... added`.
    assert "Text hi" not in flat and "sms:" not in html
    assert "555-010-9000" not in flat
    assert "w1@example.com" not in caplog.text
    assert "promoted" in caplog.text and "e***.com" in caplog.text
    assert _row(svc, "w1@example.com")["promoted_at"] is None   # sent


def test_purge_and_delete_account_email_the_promoted(made, sent,
                                                     monkeypatch):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch, email="t0@example.com")
    sent.clear()
    assert c.post("/api/account/delete",
                  json={"confirm": "DELETE"}).status_code == 200
    assert [m[0] for m in sent] == ["w1@example.com"]
    sent.clear()
    with svc.session() as s:
        s.get(beta_signup.BetaRequest, "t1@example.com").created_at = 1.0
    app.test_cli_runner().invoke(args=["beta-requests", "purge"])
    assert [m[0] for m in sent] == ["w2@example.com"]


def test_mark_new_respects_the_cap_unless_forced(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _queue(app, sent, svc)
    run = app.test_cli_runner()
    r = run.invoke(args=["beta-requests", "mark", "w2@example.com", "new"])
    assert r.exit_code != 0 and "--force" in r.output
    assert _row(svc, "w2@example.com")["status"] == "waitlist"
    r = run.invoke(args=["beta-requests", "mark", "w2@example.com", "new",
                         "--force"])
    assert r.exit_code == 0, r.output
    assert _row(svc, "w2@example.com")["status"] == "new"


@pytest.mark.parametrize("raw,shown", [
    ("+15550109999", "+1 555-010-9999"),
    ("+447700900123", "+447700900123"),
])
def test_numbers_are_shown_the_way_people_write_them(raw, shown):
    assert beta_signup.show_number(raw) == shown


# --- round 4: domains, operator guards, tile aliases, waitlist reason --------

@pytest.mark.parametrize("typed,stored", [
    ("victim@ｇｍａｉｌ.com", "victim@gmail.com"),
    ("victim@bücher.example", "victim@xn--bcher-kva.example"),
])
def test_a_domain_is_stored_in_its_ascii_form(made, typed, stored):
    app, svc = made()
    assert _post(app.test_client(), email=typed).status_code == 200
    assert [r["email"] for r in _rows(svc)] == [stored]


@pytest.mark.parametrize("email", ["victim@gmail.com.", "victim@a..example",
                                   "victim@-x.example"])
def test_a_trailing_dot_or_unencodable_domain_is_refused(made, email):
    app, svc = made()
    r = _post(app.test_client(), email=email)
    assert r.status_code == 400 and r.get_json()["error"] == "email"
    assert _rows(svc) == []


def test_mark_refuses_a_pending_row_unless_forced(made, sent):
    app, svc = made(RESEND_API_KEY="re_test", **SENDBLUE)
    _post(app.test_client())
    run = app.test_cli_runner()
    sent.clear()
    r = run.invoke(args=["beta-requests", "mark", "avery@example.com",
                         "added"])
    assert r.exit_code != 0 and "not confirmed" in r.output
    assert _row(svc)["status"] == "pending" and sent == []
    r = run.invoke(args=["beta-requests", "mark", "avery@example.com",
                         "added", "--force"])
    assert r.exit_code == 0, r.output
    assert _row(svc)["status"] == "added"


def test_mark_new_refuses_a_twin_unless_forced(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, ip="198.51.100.1", email="a@example.com",
          mobile="+15550100101")
    _join(app, sent, ip="198.51.100.2", email="b@example.com",
          mobile="+15550100101")
    run = app.test_cli_runner()
    r = run.invoke(args=["beta-requests", "mark", "b@example.com", "new"])
    assert r.exit_code != 0 and "same mobile" in r.output
    assert _row(svc, "b@example.com")["status"] == "waitlist"
    r = run.invoke(args=["beta-requests", "mark", "b@example.com", "new",
                         "--force"])
    assert r.exit_code == 0 and _row(svc, "b@example.com")["status"] == "new"


def test_the_tile_finds_the_request_by_mailbox(made, monkeypatch):
    app, svc = made(**SENDBLUE)
    c = app.test_client()
    _login(c, svc, monkeypatch, email="g.ene@gmail.com")
    with c.session_transaction() as s:
        aid = s["account_id"]
    svc.add_connection(aid, "sample", "t-sample", "Sample",
                       consent_version="2026-08-01")
    with svc.session() as s:
        s.add(beta_signup.BetaRequest(
            email="gene+beta@gmail.com", first_name="Gene", status="added",
            created_at=time.time(), updated_at=time.time(),
            confirmed_at=time.time()))
    assert 'id="text-tile"' in c.get("/home").get_data(as_text=True)


def test_a_twin_on_the_waitlist_is_not_told_the_line_is_full(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _join(app, sent, ip="198.51.100.1", email="a@example.com",
          mobile="+15550100101")
    sent.clear()
    _join(app, sent, ip="198.51.100.2", email="b@example.com",
          mobile="+15550100101")
    welcome = [m for m in sent if m[1].startswith("You're in")][0][3]
    assert ("Your request is on the waitlist for iMessage. The web app "
            "works today.") in welcome
    assert "iMessage is full" not in welcome
