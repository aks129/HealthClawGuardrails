"""#908 re-walk: the sample brief, status labels, hub card, and the model's
own copy of the made-up-records line. Real connections stay byte for byte.

The patient re-walk passed the chat and stopped at the visit brief: a red
box read "your creatinine rose ... Contact your doctor promptly" under the
grey line, and nothing said "made-up" once she scrolled. QA then found two
more: sample-only template lines left blank lines on real pages, and a bold
or reworded echo of the line from the model showed it twice.

The engine's sample voice is pinned in tests/test_brief_sample_voice.py.
This file covers CareAgents. Synthetic data only.
"""

from __future__ import annotations

import json
import re

import pytest

from careagents import beta
from careagents.personas import system_prompt
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _chat_app, cfg, svc)
from tests.test_careagents_brief_lab_trends import _brief_with_trend
from tests.test_careagents_sample_framing import (
    MODEL_TEXT, _answer, _real_chat_app)

FRAMED = f"{beta.SAMPLE_FRAME}\n\n{MODEL_TEXT}"
SECOND_PERSON = re.compile(r"\b(you|your)\b", re.IGNORECASE)


# --- the model's own copy of the line (QA F2) ------------------------------
# From the second turn the model has read earlier answers that open with the
# line, and may write it itself: bold, quoted or in its own words.

@pytest.mark.parametrize("says", [
    f"**{beta.SAMPLE_FRAME}**\n\n{MODEL_TEXT}",
    f"\"{beta.SAMPLE_FRAME}\"\n\n{MODEL_TEXT}",
    f"> _{beta.SAMPLE_FRAME.upper()}_\n\n{MODEL_TEXT}",
    f"Just so you know, these records are made up and aren't yours. "
    f"{MODEL_TEXT}",
    f"*Reminder: this is sample data, not your own.*\n\n{MODEL_TEXT}",
], ids=["bold", "quoted", "quote-block-caps", "reworded-inline",
        "reworded-italic"])
def test_a_model_echo_of_the_line_is_shown_once(
        cfg, svc, monkeypatch, says):  # noqa: F811
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    assert _answer(c, fake, agent_id, cfg, svc, monkeypatch, [],
                   says=says) == FRAMED


def test_a_sample_sentence_that_is_content_is_kept(
        cfg, svc, monkeypatch):  # noqa: F811
    """"In these made-up records, ..." is the answer, not an echo."""
    says = "In these made-up records, creatinine rose from 0.8 to 1.3."
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    assert _answer(c, fake, agent_id, cfg, svc, monkeypatch, [],
                   says=says) == f"{beta.SAMPLE_FRAME}\n\n{says}"


@pytest.mark.parametrize("says", [
    f"**{beta.SAMPLE_FRAME}**\n\n{MODEL_TEXT}",
    f"These records are made up and aren't yours. {MODEL_TEXT}",
], ids=["bold", "reworded"])
def test_a_real_answer_is_never_stripped(cfg, svc, monkeypatch, says):  # noqa: F811
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    assert _answer(c, fake, agent_id, cfg, svc, monkeypatch, [],
                   says=says) == says


# --- status labels, prompt, hub card (re-walk F4, F6, F7) -------------------

def _tool_labels(c, fake, agent_id, cfg, svc, monkeypatch):  # noqa: F811
    """The status labels shown while a turn prepares the brief."""
    from careagents import agent as agent_mod
    from careagents.worker import RunWorker

    class _Call:
        id, name, arguments = "c1", "appointment_brief", {}

    class _Ask:
        text, tool_calls, raw_tool_calls = "", [_Call()], []

    class _Done:
        text, tool_calls, raw_tool_calls = "done", [], []
    turns = iter([_Ask(), _Done()])
    monkeypatch.setattr(agent_mod.llm, "complete",
                        lambda *a, **k: next(turns))
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": "brief",
                                  "request_id": "label-1"}, buffered=False)
    r.close()
    RunWorker(cfg, fake, svc, "label-worker").run_once()
    return [e["payload"]["label"] for events in fake.events.values()
            for e in events if e["type"] == "agent.tool"]


def test_sample_status_labels_say_made_up(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.agent import SAMPLE_TOOL_LABELS, TOOL_LABELS
    _app, c, fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    assert _tool_labels(c, fake, agent_id, cfg, svc, monkeypatch) == [
        "Preparing the sample visit brief"]
    assert set(SAMPLE_TOOL_LABELS) == set(TOOL_LABELS)
    assert not any(SECOND_PERSON.search(v)
                   for v in SAMPLE_TOOL_LABELS.values())


def test_real_status_labels_are_todays(cfg, svc, monkeypatch):  # noqa: F811
    c, fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    assert _tool_labels(c, fake, agent_id, cfg, svc, monkeypatch) == [
        "Preparing your visit brief"]


def test_the_sample_prompt_says_the_app_shows_the_line():
    sample = system_prompt("Juniper", "calm", sample=True)
    assert "already shows a line" in sample
    assert 'never "clinician"' in sample
    assert '"partial lab list"' in sample


def _card_note(body):
    start = body.index('class="card-note"')
    return body[start:body.index("</p>", start)]


def test_the_sample_hub_card_says_made_up(cfg, svc, monkeypatch):  # noqa: F811
    _app, c, _fake, _agent, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    note = _card_note(c.get("/home").get_data(as_text=True))
    assert "these made-up records" in note
    assert not SECOND_PERSON.search(note)


def test_the_real_hub_card_is_todays(cfg, svc, monkeypatch):  # noqa: F811
    c, _fake, _agent = _real_chat_app(cfg, svc, monkeypatch)
    assert _card_note(c.get("/home").get_data(as_text=True)) == (
        'class="card-note">Visit brief: a one-page summary of your records '
        'to bring to an appointment.')


# --- the brief asks for the sample voice (re-walk F1) -----------------------

def test_the_sample_brief_asks_the_engine_for_the_sample_voice(
        cfg, svc, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant, **kw: seen.append(kw))
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    c.get(f"/brief?agent={agent_id}")
    assert seen == [{"voice": "sample"}]


def test_a_real_brief_asks_the_engine_as_before(cfg, svc, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant, **kw: seen.append(kw))
    c, _fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    c.get(f"/brief?agent={agent_id}")
    assert seen == [{}]


def test_the_client_sends_the_voice_only_for_the_sample():
    from careagents.healthclaw import HealthClawClient, HealthClawError
    hc = HealthClawClient("http://engine", "s")
    urls = []

    def _send(method, url, **_):
        urls.append(url)
        raise HealthClawError("stop", 503)
    hc._headers = lambda tenant: {}
    hc._send = _send
    for kwargs in ({}, {"voice": "sample"}, {"voice": "nonsense"}):
        with pytest.raises(HealthClawError):
            hc.fetch_appointment_brief("t", **kwargs)
    assert [u.rsplit("/", 1)[-1] for u in urls
            if "AppointmentBrief" in u] == [
        "AppointmentBrief", "AppointmentBrief?voice=sample",
        "AppointmentBrief"]


def test_the_sample_brief_reaches_the_real_engine(cfg, svc, monkeypatch):  # noqa: F811
    """Cross-layer: the real client on the real engine, sample connection.
    Proves the engine accepts the voice, not only that it was sent."""
    from tests.test_beta_acceptance_rows import BASE, Chain
    chain = Chain(cfg, svc, monkeypatch)
    page = chain.s.get(f"{BASE}/brief", params={"agent": chain.agent}).text
    assert "the sample person&#39;s creatinine rose" in page \
        or "the sample person's creatinine rose" in page
    assert "your creatinine" not in page
    assert "The sample person may be due for" in page


# --- the brief's own words (re-walk F2) -------------------------------------

def _main(body):
    return body[body.index("<main"):body.index("</main>")]


def _fetch(kind):
    from careagents.healthclaw import HealthClawError

    def fetch(self, tenant, **_):
        if kind == "down":
            raise HealthClawError("down", 503)
        return _brief_with_trend() if kind == "trend" else None
    return fetch


def _trend_value():
    ext = _brief_with_trend()["extension"][0]["extension"][0]
    return json.loads(ext["valueString"])["value"]


@pytest.mark.parametrize("brief", ["trend", "none", "down"])
def test_the_sample_brief_page_never_says_your(cfg, svc, monkeypatch, brief):  # noqa: F811
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief", _fetch(brief))
    _app, c, _fake, agent_id, _t, _cid = _chat_app(cfg, svc, monkeypatch)
    main = _main(c.get(f"/brief?agent={agent_id}").get_data(as_text=True))
    # The fake engine's trend sentence says "your"; the real engine's sample
    # voice does not (tests/test_brief_sample_voice.py). Everything the page
    # writes itself:
    own = main.replace(_trend_value(), "").replace(beta.SAMPLE_FRAME, "")
    assert not SECOND_PERSON.search(own), SECOND_PERSON.findall(own)
    assert "made-up" in own


def test_the_sample_banner_is_sticky():
    import pathlib
    css = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
           / "static" / "careagents.css").read_text()
    rule = css[css.index(".beta-banner.sample-banner {"):]
    rule = rule[:rule.index("}")]
    assert "position: sticky" in rule and "top: 0" in rule


# --- byte-exact real pages (QA F1) ------------------------------------------
# What main renders around each spot this change touched, copied from a
# render of main. A sample-only construct that leaks even a blank line onto
# a real page fails here.

def test_the_real_chat_page_renders_as_main(cfg, svc, monkeypatch):  # noqa: F811
    c, _fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    body = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert ('href="/safety">Safety grade: …</a>\n    </div>\n  </div>\n\n  \n'
            '  <div class="chat-log" id=') in body


@pytest.mark.parametrize("brief", ["trend", "none", "down"])
def test_the_real_brief_renders_as_main(cfg, svc, monkeypatch, brief):  # noqa: F811
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief", _fetch(brief))
    c, _fake, agent_id = _real_chat_app(cfg, svc, monkeypatch)
    body = c.get(f"/brief?agent={agent_id}").get_data(as_text=True)
    assert '<main class="hub">\n  <div class="hub-head">\n    <div>\n' in body
    assert ('<div class="hub-sub">A snapshot of your records to bring to your '
            'visit.</div>') in body
    empty = ("We could not reach your records just now, so this section was "
             "not checked." if brief == "down"
             else "Not available from your connected records.")
    assert (f'</h2></div>\n    \n    \n    <p class="brief-empty">{empty}</p>'
            f'\n    \n  </div>') in body
    if brief == "trend":
        assert ('<h2 id="brief-alert-title">Something to raise with your '
                'doctor</h2>') in body
        assert ('\n      <span class="brief-alert-source">From your lab '
                'results</span>\n    </div>\n') in body
    else:
        assert ("<p class=\"brief-empty\">We couldn't check your screenings "
                "just now. Ask your doctor which ones you are due for.</p>"
                ) in body
