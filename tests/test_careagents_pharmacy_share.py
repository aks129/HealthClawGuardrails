"""Share with a pharmacy by text: PHARMACY, YES, and the RX Genie handoff
(careagents/pharmacy_share.py, careagents/healthclaw.py ktc_share_link).

No real network: the engine is the FakeClient with a scripted
ktc_share_link, the pharmacy's intake is a recording HTTP double, and
Sendblue is the recorder from tests/test_careagents_sendblue.py. Synthetic
handles only (555 numbers).
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents import pharmacy_share
from careagents.config import Config, ConfigError
from careagents.healthclaw import HealthClawClient, HealthClawError
from careagents.models import Connection, PharmacyShare
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)
from tests.test_careagents_sendblue import PHONE, _env, _hook, _pair
from tests.test_careagents_sendblue import FakeSendblue

INTAKE = "https://rxgenie.us/pharmacy/api/intake"
SHLINK = "shlink:/eyJ1cmwiOiJodHRwczovL2VuZ2luZS5leGFtcGxlL3NobC94In0"


class _Resp:
    def __init__(self, status):
        self.status_code = status


class FakeIntake:
    """The pharmacy's intake endpoint: records each POST."""

    def __init__(self, status=200, raises=None):
        self.status, self.raises = status, raises
        self.posts: list[dict] = []

    def post(self, url, **kw):
        self.posts.append({"url": url, **kw})
        if self.raises:
            raise self.raises
        return _Resp(self.status)


@pytest.fixture
def ps_cfg(cfg):  # noqa: F811
    return Config(env=_env(cfg, CARE_PHARMACY_SHARE="on"))


@pytest.fixture
def ps_svc(ps_cfg):
    from careagents.accounts import AccountService
    service = AccountService(ps_cfg)
    yield service
    service.engine.dispose()


def _app(cfg, svc, monkeypatch, kind="fasten", link=None, intake=None):  # noqa: F811
    """A paired phone on an agent whose connection is `kind`."""
    app, c, fake_hc, agent_id, tenant, conn_id = _chat_app(
        cfg, svc, monkeypatch, reply="assistant answer")
    with svc.session() as s:
        s.get(Connection, conn_id).kind = kind
    sb = app.extensions["careagents_sendblue"]
    sb.client = FakeSendblue()
    sb.spawn = lambda fn: fn()
    calls = []

    def ktc_share_link(t, types):
        calls.append((t, list(types)))
        if isinstance(link, Exception):
            raise link
        return link or {"shlink": SHLINK, "exp": 1}

    fake_hc.ktc_share_link = ktc_share_link
    share = app.extensions["careagents_imessage"].pharmacy_share
    if share is not None:
        share.http = intake or FakeIntake()
    _pair(c, agent_id)
    sb.client.sent.clear()
    return app, c, sb.client, calls, share, tenant


def _texts(fake):
    return [t for _, t in fake.sent]


def _shares(svc):  # noqa: F811
    with svc.session() as s:
        return [{"decision": r.decision, "outcome": r.outcome,
                 "host": r.pharmacy_host, "purpose": r.purpose}
                for r in s.query(PharmacyShare).order_by(
                    PharmacyShare.asked_at).all()]


# --- config -------------------------------------------------------------------

def test_off_by_default_with_the_rx_genie_intake(cfg):  # noqa: F811
    c = Config(env=_env(cfg))
    assert c.pharmacy_share is False
    assert c.pharmacy_intake_url == INTAKE
    assert c.pharmacy_name == "RX Genie Demo Pharmacy"


@pytest.mark.parametrize("url", [
    "http://rxgenie.us/pharmacy/api/intake", "https://10.0.0.1/intake",
    "https://localhost/intake", "https://user@rxgenie.us/intake",
    "https://rxgenie.us/intake?x=1", "https://rxgenie.us\\@evil.test/x"])
def test_on_a_bad_intake_url_refuses_to_boot(cfg, url):  # noqa: F811
    with pytest.raises(ConfigError):
        Config(env=_env(cfg, CARE_PHARMACY_SHARE="on",
                        CARE_PHARMACY_INTAKE_URL=url))


def test_the_flag_is_on_or_off_only(cfg):  # noqa: F811
    with pytest.raises(ConfigError):
        Config(env=_env(cfg, CARE_PHARMACY_SHARE="maybe"))


def test_off_the_words_reach_the_assistant(cfg, svc, monkeypatch):  # noqa: F811
    sb_cfg = Config(env=_env(cfg))
    from careagents.accounts import AccountService
    sb_svc = AccountService(sb_cfg)
    try:
        app, c, fake, calls, share, _ = _app(sb_cfg, sb_svc, monkeypatch)
        assert share is None
        _hook(c, "PHARMACY", handle="m1")
        assert pharmacy_share.offer_text("RX Genie Demo Pharmacy") \
            not in _texts(fake)
        assert calls == []
    finally:
        sb_svc.engine.dispose()


# --- the flow -------------------------------------------------------------------

def test_pharmacy_then_yes_hands_the_link_to_the_pharmacy(
        ps_cfg, ps_svc, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    intake = FakeIntake(200)
    app, c, fake, calls, share, tenant = _app(ps_cfg, ps_svc, monkeypatch,
                                              intake=intake)
    _hook(c, "pharmacy", handle="m1")
    assert _texts(fake) == [pharmacy_share.offer_text(
        "RX Genie Demo Pharmacy")]
    assert calls == [] and intake.posts == []

    _hook(c, "Yes!", handle="m2")
    assert calls == [(tenant, list(pharmacy_share.SHARE_TYPES))]
    assert len(intake.posts) == 1
    post = intake.posts[0]
    assert post["url"] == INTAKE
    assert post["json"] == {"shlink": SHLINK, "purpose": "Pharmacy visit",
                            "consent": "Patient replied YES by text"}
    assert post["allow_redirects"] is False
    assert _texts(fake)[-1] == pharmacy_share.sent_text(
        "RX Genie Demo Pharmacy")
    assert _shares(ps_svc) == [{"decision": "yes", "outcome": "sent",
                                "host": "rxgenie.us",
                                "purpose": "pharmacy-visit"}]
    # The link carries the key: never texted, logged or stored.
    assert all(SHLINK not in t and "shlink" not in t for t in _texts(fake))
    assert SHLINK not in caplog.text
    with ps_svc.session() as s:
        row = s.query(PharmacyShare).one()
        assert SHLINK not in json.dumps(
            {k: str(v) for k, v in vars(row).items()})


def test_a_second_yes_shares_nothing_more(ps_cfg, ps_svc, monkeypatch):
    intake = FakeIntake(200)
    app, c, fake, calls, *_ = _app(ps_cfg, ps_svc, monkeypatch,
                                   intake=intake)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "YES", handle="m2")
    _hook(c, "YES", handle="m3")
    assert len(calls) == 1 and len(intake.posts) == 1


def test_yes_without_an_offer_goes_to_the_assistant(
        ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch)
    r = _hook(c, "yes", handle="m1")
    assert r.status_code == 200
    assert calls == [] and share.http.posts == []
    assert _shares(ps_svc) == []


def test_no_shares_nothing(ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "no", handle="m2")
    assert _texts(fake)[-1] == pharmacy_share.DECLINED_TEXT
    assert calls == [] and share.http.posts == []
    assert _shares(ps_svc)[0]["outcome"] == "declined"


def test_an_expired_offer_cannot_be_accepted(ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch)
    t = [1_000_000.0]
    share.clock = lambda: t[0]
    _hook(c, "PHARMACY", handle="m1")
    t[0] += pharmacy_share.OFFER_TTL_SECONDS + 1
    _hook(c, "YES", handle="m2")
    assert calls == [] and share.http.posts == []


def test_the_sample_is_never_offered(ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch,
                                         kind="sample")
    _hook(c, "PHARMACY", handle="m1")
    assert _texts(fake) == [pharmacy_share.SAMPLE_TEXT]
    assert _shares(ps_svc) == []


def test_records_without_the_test_marker_are_refused_by_the_engine(
        ps_cfg, ps_svc, monkeypatch):
    err = HealthClawError("ktc link failed (403)", 403, code="not_test_data")
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch,
                                         link=err)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "YES", handle="m2")
    assert _texts(fake)[-1] == pharmacy_share.TEST_ONLY_TEXT
    assert share.http.posts == []
    assert _shares(ps_svc)[0]["outcome"] == "not_test_data"


@pytest.mark.parametrize("intake", [
    FakeIntake(502), FakeIntake(302), FakeIntake(400),
    FakeIntake(raises=__import__("requests").ConnectionError("boom"))])
def test_a_pharmacy_failure_says_a_fixed_sentence(
        ps_cfg, ps_svc, monkeypatch, caplog, intake):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch,
                                         intake=intake)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "YES", handle="m2")
    assert _texts(fake)[-1] == pharmacy_share.FAILED_TEXT
    assert _shares(ps_svc)[0]["outcome"] == "failed"
    assert SHLINK not in caplog.text and "boom" not in caplog.text


def test_an_engine_failure_says_a_fixed_sentence(ps_cfg, ps_svc, monkeypatch):
    err = HealthClawError("ktc link failed (500) secret detail", 500)
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch,
                                         link=err)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "YES", handle="m2")
    assert _texts(fake)[-1] == pharmacy_share.FAILED_TEXT
    assert all("secret" not in t for t in _texts(fake))
    assert share.http.posts == []


def test_a_paused_account_is_not_shared(ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch)
    monkeypatch.setattr(ps_svc, "is_paused", lambda account_id: True)
    _hook(c, "PHARMACY", handle="m1")
    assert _texts(fake) == [pharmacy_share.PAUSED_TEXT]


def test_stop_still_wins(ps_cfg, ps_svc, monkeypatch):
    app, c, fake, calls, share, _ = _app(ps_cfg, ps_svc, monkeypatch)
    _hook(c, "PHARMACY", handle="m1")
    _hook(c, "STOP", handle="m2")
    _hook(c, "YES", handle="m3")
    assert calls == [] and share.http.posts == []
    assert fake.sent[-1][0] == PHONE


# --- the engine client ------------------------------------------------------------

class _EngineResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.content = json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._body


class _EngineHTTP:
    def __init__(self, export):
        self.export = export
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return _EngineResp(200, {"resourceType": "Bundle", "entry": [
            {"resource": {"resourceType": "Patient", "id": "pt-1"}}]})

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if url.endswith("/internal/step-up-token"):
            return _EngineResp(200, {"token": "ktc-tok"})
        return self.export


def _client(export):
    hc = HealthClawClient("http://engine.local", "mint-secret")
    hc.http = _EngineHTTP(export)
    hc.mint_token = lambda tenant: "general"
    return hc


def test_the_client_asks_for_a_link_for_the_one_patient():
    hc = _client(_EngineResp(200, {"link_id": "x", "shlink": SHLINK,
                                   "exp": 99}))
    assert hc.ktc_share_link("t-1", ["Patient", "Coverage"]) == {
        "shlink": SHLINK, "exp": 99}
    mint = [kw for u, kw in hc.http.calls if u.endswith("step-up-token")][0]
    assert mint["json"] == {"tenant_id": "t-1", "purpose": "ktc",
                            "resource_types": ["Patient", "Coverage"]}
    export = [kw for u, kw in hc.http.calls if u.endswith("$share-bundle")][0]
    assert export["json"] == {"profile": "ktc", "deliver": "link",
                              "resource_types": ["Patient", "Coverage"],
                              "patient_id": "pt-1"}
    assert export["headers"]["X-Step-Up-Token"] == "ktc-tok"


@pytest.mark.parametrize("status,code", [
    (403, "not_test_data"), (404, "disabled"), (413, "too_large"),
    (422, "no_patient"), (503, "link_unavailable"), (500, "")])
def test_the_client_maps_refusals_to_fixed_codes(status, code):
    hc = _client(_EngineResp(status, {"error": "engine words CANARY"}))
    with pytest.raises(HealthClawError) as e:
        hc.ktc_share_link("t-1", ["Patient"])
    assert e.value.status == status and e.value.code == code
    assert "CANARY" not in str(e.value)


def test_the_client_refuses_an_answer_without_a_shlink():
    hc = _client(_EngineResp(200, {"link_id": "x", "shlink": "https://x",
                                   "exp": 1}))
    with pytest.raises(HealthClawError):
        hc.ktc_share_link("t-1", ["Patient"])
