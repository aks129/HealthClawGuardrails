"""The /beta page, its request form, the removal link, the operator CLI and
the hub's "Text your assistant" tile (docs/briefs/2026-10-06-beta-onboarding.md).

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
           "HEALTHCLAW_MINT_SECRET": "mint-secret"}
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
                       headers={"X-Forwarded-For": ip})


def _rows(svc):
    with svc.session() as s:
        return [{c.name: getattr(r, c.name)
                 for c in beta_signup.BetaRequest.__table__.columns}
                for r in s.query(beta_signup.BetaRequest).all()]


def _token(html: str) -> str:
    m = re.search(r"/beta/remove\?t=([A-Za-z0-9_-]+)", html)
    assert m, html
    return m.group(1)


# --- the page ---------------------------------------------------------------

def test_the_page_opens_with_and_without_a_ref(made):
    app, _ = made()
    c = app.test_client()
    assert c.get("/beta").status_code == 200
    body = c.get("/beta?ref=advisor").get_data(as_text=True)
    assert 'name="ref" value="advisor"' in body
    # A bad ref is dropped, never refused and never echoed.
    r = c.get("/beta?ref=%3Cscript%3Ealert(1)%3C/script%3E")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<script>alert" not in body and "alert(1)" not in body
    assert 'name="ref" value=""' in body


def test_the_page_says_what_the_brief_says_in_plain_words(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    assert "tenant" not in body.lower()
    assert "fhir" not in body.lower()
    assert "you approve anything it sends" in text
    assert "not a doctor" in text and "medical advice" in text
    # Step 2 names the records as made up.
    step2 = body[body.index('id="beta-step-2"'):]
    step2 = step2[:step2.index("</li>")]
    assert "made-up" in step2 or "sample" in step2
    assert ("https://github.com/aks129/HealthClawGuardrails/releases/tag/"
            "demo-videos-2026-10") in body
    assert "mailto:contactus@healthclaw.io?subject=tester" in body
    # Retention, as the owner set it.
    assert "We keep your name, email and phone only to run the beta." in text
    assert "30 days" in text and "60 days" in text
    assert "delete your account in Settings" in text


def test_the_form_fields_and_the_unticked_consent_box(made):
    app, _ = made()
    body = app.test_client().get("/beta").get_data(as_text=True)
    form = body[body.index("<form"):body.index("</form>")]
    assert 'name="first_name"' in form and 'name="email"' in form
    mobile = re.search(r"<input[^>]*name=\"mobile\"[^>]*>", form).group(0)
    assert "required" not in mobile
    assert "for iMessage" in form
    box = re.search(r"<input[^>]*name=\"consent\"[^>]*>", form).group(0)
    assert 'type="checkbox"' in box and "required" in box
    assert "checked" not in box
    assert "You can contact me about the beta" in form
    # The honeypot is off-screen and out of the tab order, not type=hidden
    # (a bot skips hidden inputs).
    pot = re.search(r"<input[^>]*name=\"website\"[^>]*>", form).group(0)
    assert 'tabindex="-1"' in pot and 'autocomplete="off"' in pot


def test_the_page_reads_at_phone_width():
    # 375px: body text at least 16px, nothing wider than the screen.
    block = _CSS[_CSS.index(".beta-page {"):]
    block = block[:block.index("}")]
    size = int(re.search(r"font-size:\s*(\d+)px", block).group(1))
    assert size >= 16
    assert "max-width" in block
    assert "overflow-wrap: anywhere" in _CSS[_CSS.index(".beta-page"):]


def test_the_browser_refuses_while_the_box_is_unticked():
    assert "consent.checked" in _JS
    assert 'headers: { "Content-Type": "application/json" }' in _JS
    assert "Thanks. Check your email." in _JS


# --- submit -----------------------------------------------------------------

def test_a_submit_creates_one_new_request(made):
    app, svc = made()
    r = _post(app.test_client(), ref="advisor", mobile="(555) 010-0101")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    rows = _rows(svc)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "new" and row["ref"] == "advisor"
    assert row["email"] == "avery@example.com"
    assert row["first_name"] == "Avery"
    assert row["mobile"] == "+5550100101"
    assert row["mobile_given_at"] is not None


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


def test_a_second_submit_with_the_same_email_updates_the_row(made):
    app, svc = made()
    c = app.test_client()
    _post(c, ref="advisor")
    _post(c, first_name="Ava", email="AVERY@Example.com",
          mobile="+1 555 010 0199")
    rows = _rows(svc)
    assert len(rows) == 1
    assert rows[0]["first_name"] == "Ava"
    assert rows[0]["mobile"] == "+15550100199"
    assert rows[0]["ref"] == "advisor"      # a missing ref keeps the first one


def test_a_second_submit_after_removed_starts_again(made):
    app, svc = made()
    c = app.test_client()
    _post(c)
    beta_signup.mark(svc.session, "avery@example.com", "removed")
    _post(c)
    assert _rows(svc)[0]["status"] == "new"


def test_coming_back_after_removed_counts_against_the_ten(made):
    app, svc = made()
    c = app.test_client()
    _post(c, ip="198.51.100.200", mobile="+15550100101")
    beta_signup.mark(svc.session, "avery@example.com", "removed")
    for i in range(beta_signup.IMESSAGE_SPOTS):
        _post(c, ip=f"198.51.100.{i}", email=f"t{i}@example.com",
              mobile=f"+1555010{i:04d}")
    _post(c, ip="198.51.100.201", mobile="+15550100101")
    by = {r["email"]: r["status"] for r in _rows(svc)}
    assert by["avery@example.com"] == "waitlist"


def test_a_filled_honeypot_answers_the_same_and_keeps_nothing(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    r = _post(app.test_client(), website="http://spam.example")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert _rows(svc) == []
    assert sent == []


def test_a_form_post_is_refused_as_cross_site(made):
    """The app's forms post JSON, which a cross-site page cannot send
    without a preflight. A plain form post is refused."""
    app, svc = made()
    r = app.test_client().post("/beta", data=_form(consent="on"))
    assert r.status_code == 415
    assert _rows(svc) == []


@pytest.mark.parametrize("over,error", [
    ({"email": "not-an-email"}, "email"),
    ({"email": "a@b"}, "email"),
    ({"email": "x" * 250 + "@example.com"}, "email"),
    ({"first_name": ""}, "first_name"),
    ({"first_name": "   "}, "first_name"),
    ({"first_name": "x" * 41}, "first_name"),
    ({"mobile": "call me"}, "mobile"),
    ({"mobile": "12"}, "mobile"),
    ({"mobile": "1" * 16}, "mobile"),
])
def test_bad_fields_are_refused_with_a_reason(made, over, error):
    app, svc = made()
    r = _post(app.test_client(), **over)
    assert r.status_code == 400
    assert r.get_json()["error"] == error
    assert _rows(svc) == []


def test_submits_are_limited_per_ip(made):
    app, svc = made()
    c = app.test_client()
    for i in range(beta_signup.SUBMITS_PER_WINDOW):
        assert _post(c, email=f"t{i}@example.com").status_code == 200
    r = _post(c, email="late@example.com")
    assert r.status_code == 429
    # Another address is its own bucket.
    assert _post(c, ip="198.51.100.4",
                 email="other@example.com").status_code == 200
    # The proxy's own hop is the one trusted: a forged left-hand entry
    # does not open a new bucket.
    r = _post(c, ip="192.0.2.1, 203.0.113.7", email="forged@example.com")
    assert r.status_code == 429


def test_the_eleventh_mobile_goes_on_the_waitlist(made):
    app, svc = made()
    c = app.test_client()
    for i in range(beta_signup.IMESSAGE_SPOTS):
        _post(c, ip=f"198.51.100.{i}", email=f"t{i}@example.com",
              mobile=f"+1555010{i:04d}")
    _post(c, ip="198.51.100.50", email="nomobile@example.com")
    _post(c, ip="198.51.100.51", email="late@example.com",
          mobile="+15550109999")
    by = {r["email"]: r["status"] for r in _rows(svc)}
    assert by["t9@example.com"] == "new"
    assert by["nomobile@example.com"] == "new"
    assert by["late@example.com"] == "waitlist"


def test_a_spot_counts_after_its_mobile_is_deleted(made):
    """`added` deletes the number. The spot is still taken."""
    app, svc = made()
    c = app.test_client()
    for i in range(beta_signup.IMESSAGE_SPOTS):
        _post(c, ip=f"198.51.100.{i}", email=f"t{i}@example.com",
              mobile=f"+1555010{i:04d}")
        beta_signup.mark(svc.session, f"t{i}@example.com", "added")
    _post(c, ip="198.51.100.51", email="late@example.com",
          mobile="+15550109999")
    by = {r["email"]: r["status"] for r in _rows(svc)}
    assert by["late@example.com"] == "waitlist"


def test_with_no_mail_provider_the_submit_still_succeeds(made):
    app, svc = made()
    assert _post(app.test_client()).status_code == 200
    assert len(_rows(svc)) == 1


def test_the_owner_email_carries_the_masked_domain_only(made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_BETA_NOTIFY_EMAIL="owner@example.org")
    _post(app.test_client(), first_name="<b>Avery</b>",
          email="avery@example.com", mobile="+15550100101", ref="advisor")
    owner = [m for m in sent if m[0] == "owner@example.org"]
    assert len(owner) == 1
    _, subject, html, text = owner[0]
    for part in (subject, html, text):
        assert "avery@example.com" not in part
        assert "Avery" not in part
        assert "5550100101" not in part and "0101" not in part
        assert "example.com" not in part
    assert "e***.com" in text
    assert "mobile: yes" in text
    assert "ref: advisor" in text


def test_no_owner_address_means_no_owner_email(made, sent):
    app, _ = made(RESEND_API_KEY="re_test")
    _post(app.test_client())
    assert [m[0] for m in sent] == ["avery@example.com"]


def test_the_tester_email_says_youre_in_and_carries_the_links(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client(), first_name="<b>Avery</b>")
    (to, subject, html, text), = sent
    assert to == "avery@example.com"
    assert "You're in. Open careagents.cloud and sign up with this email." \
        in text
    assert "mailto:contactus@healthclaw.io?subject=tester" in html
    assert "<b>Avery" not in html and "<b>Avery" not in text
    token = _token(html)
    assert f"/beta/remove?t={token}" in text
    assert "iMessage is full" not in text
    # Only the hash is kept.
    row = _rows(svc)[0]
    assert token not in str(row)
    assert row["removal_hash"] == beta_signup.hash_token(token)


def test_a_waitlisted_tester_is_told_the_web_app_works_today(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    c = app.test_client()
    for i in range(beta_signup.IMESSAGE_SPOTS):
        _post(c, ip=f"198.51.100.{i}", email=f"t{i}@example.com",
              mobile=f"+1555010{i:04d}")
    sent.clear()
    _post(c, ip="198.51.100.51", email="late@example.com",
          mobile="+15550109999")
    (_, _, _, text), = sent
    assert ("iMessage is full for now. We'll email you when a spot opens. "
            "The web app works today.") in text


# --- removal ----------------------------------------------------------------

def _submitted_token(app, sent):
    _post(app.test_client())
    return _token(sent[-1][2])


def test_opening_the_removal_link_only_asks(made, sent):
    """A mail scanner follows GET links. Opening one removes nothing."""
    app, svc = made(RESEND_API_KEY="re_test")
    token = _submitted_token(app, sent)
    r = app.test_client().get(f"/beta/remove?t={token}")
    assert r.status_code == 200
    assert r.headers["Referrer-Policy"] == "no-referrer"
    body = r.get_data(as_text=True)
    assert "Remove my request" in body and 'method="post"' in body
    assert len(_rows(svc)) == 1


def test_the_removal_token_works_once(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    token = _submitted_token(app, sent)
    c = app.test_client()
    r = c.post("/beta/remove", data={"t": token})
    assert r.status_code == 200
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "removed" in r.get_data(as_text=True)
    assert _rows(svc) == []
    again = c.post("/beta/remove", data={"t": token})
    assert again.status_code == 410
    assert c.get(f"/beta/remove?t={token}").status_code == 410


def test_a_later_email_replaces_the_earlier_link(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    first = _submitted_token(app, sent)
    second = _submitted_token(app, sent)
    assert first != second
    c = app.test_client()
    assert c.post("/beta/remove", data={"t": first}).status_code == 410
    assert len(_rows(svc)) == 1
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

def test_list_shows_the_mobile_in_full_only_while_new(made):
    app, svc = made()
    c = app.test_client()
    _post(c, email="a@example.com", mobile="+15550100101", ref="advisor")
    _post(c, email="b@example.com", mobile="+15550100102")
    beta_signup.mark(svc.session, "b@example.com", "waitlist")
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    a_line = next(x for x in out.splitlines() if "a@example.com" in x)
    b_line = next(x for x in out.splitlines() if "b@example.com" in x)
    assert "+15550100101" in a_line and "advisor" in a_line
    assert " new" in a_line and "Avery" in a_line
    assert "+15550100102" not in b_line and "0102" in b_line
    assert "waitlist" in b_line


def test_list_with_nothing_says_so(made):
    app, _ = made()
    out = app.test_cli_runner().invoke(args=["beta-requests", "list"]).output
    assert "no requests" in out


def test_mark_added_deletes_the_mobile_and_sends_the_text_hi_email(
        made, sent):
    app, svc = made(RESEND_API_KEY="re_test",
                    CARE_IMESSAGE_HANDLE="+15550109000")
    _post(app.test_client(), mobile="+15550100101")
    sent.clear()
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "Avery@Example.com", "added"])
    assert r.exit_code == 0, r.output
    row = _rows(svc)[0]
    assert row["status"] == "added" and row["mobile"] is None
    (to, _, html, text), = sent
    assert to == "avery@example.com"
    assert "Text hi to +15550109000" in text
    assert "mailto:contactus@healthclaw.io?subject=tester" in html
    assert row["removal_hash"] == beta_signup.hash_token(_token(html))


def test_mark_added_without_a_number_sends_nothing_and_says_so(made, sent):
    app, svc = made(RESEND_API_KEY="re_test")
    _post(app.test_client(), mobile="+15550100101")
    sent.clear()
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "avery@example.com", "added"])
    assert r.exit_code == 0
    assert "no iMessage number" in r.output
    assert sent == []
    assert _rows(svc)[0]["status"] == "added"


@pytest.mark.parametrize("status", ["waitlist", "active", "removed"])
def test_mark_other_statuses(made, status):
    app, svc = made()
    _post(app.test_client(), mobile="+15550100101")
    r = app.test_cli_runner().invoke(
        args=["beta-requests", "mark", "avery@example.com", status])
    assert r.exit_code == 0, r.output
    assert _rows(svc)[0]["status"] == status


def test_mark_refuses_an_unknown_email_or_status(made):
    app, _ = made()
    _post(app.test_client())
    run = app.test_cli_runner()
    assert run.invoke(args=["beta-requests", "mark", "no@example.com",
                            "added"]).exit_code != 0
    assert run.invoke(args=["beta-requests", "mark", "avery@example.com",
                            "banana"]).exit_code != 0


def test_purge_keeps_what_the_page_promises(made, monkeypatch):
    app, svc = made()
    c = app.test_client()
    _post(c, email="old@example.com", mobile="+15550100101")
    _post(c, email="kept@example.com", mobile="+15550100102")
    _post(c, email="month@example.com", mobile="+15550100103")
    _post(c, email="gene@example.com")
    _login(c, svc, monkeypatch, email="gene@example.com")
    day = 86400
    with svc.session() as s:
        q = s.query(beta_signup.BetaRequest)
        for email, age in (("old@example.com", 61), ("month@example.com", 31),
                           ("gene@example.com", 90)):
            row = q.filter_by(email=email).one()
            row.created_at = time.time() - age * day
            if row.mobile:
                row.mobile_given_at = time.time() - age * day
    r = app.test_cli_runner().invoke(args=["beta-requests", "purge"])
    assert r.exit_code == 0, r.output
    by = {row["email"]: row for row in _rows(svc)}
    # No account after 60 days: gone.
    assert "old@example.com" not in by
    # An account holder's request stays.
    assert "gene@example.com" in by
    # A mobile older than 30 days is deleted, the request stays.
    assert by["month@example.com"]["mobile"] is None
    assert by["kept@example.com"]["mobile"] == "+15550100102"
    assert "1 request" in r.output and "1 mobile" in r.output


# --- hub ----------------------------------------------------------------------

def _hub(app, svc, monkeypatch, kinds):
    c = app.test_client()
    _login(c, svc, monkeypatch, email="gene@example.com")
    with c.session_transaction() as s:
        aid = s["account_id"]
    for kind in kinds:
        svc.add_connection(aid, kind, f"t-{kind}", kind.title(),
                           consent_version="2026-08-01")
    return c.get("/home").get_data(as_text=True)


def test_the_text_tile_shows_for_a_sample_account_while_imessage_is_on(
        made, monkeypatch):
    app, svc = made(CARE_IMESSAGE_HANDLE="+15550109000")
    body = _hub(app, svc, monkeypatch, ["sample"])
    assert 'id="text-tile"' in body
    assert "Text your assistant" in body
    assert ("Texts go through an outside messaging service. Use it with "
            "sample records only.") in body


def test_the_text_tile_is_hidden_with_a_real_connection(made, monkeypatch):
    app, svc = made(CARE_IMESSAGE_HANDLE="+15550109000")
    body = _hub(app, svc, monkeypatch, ["sample", "fasten"])
    assert 'id="text-tile"' not in body


def test_the_text_tile_is_hidden_while_imessage_is_off(made, monkeypatch):
    app, svc = made()
    body = _hub(app, svc, monkeypatch, ["sample"])
    assert 'id="text-tile"' not in body


def test_the_hub_links_testers_to_feedback(made, monkeypatch):
    app, svc = made()
    body = _hub(app, svc, monkeypatch, [])
    assert "mailto:contactus@healthclaw.io?subject=tester" in body
