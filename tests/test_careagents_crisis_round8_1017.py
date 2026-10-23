"""Security review of PR #1017, round eight (3929abc0 to 1ecfb09b).

The sample line now closes every sample answer. On the web the banner
shows it, and under_banner / withoutFrame drop the copy in the bubble.
On a text, split_reply keeps it in the last part.

These tests fail on 1ecfb09b by design:

- A model that ends its own answer with the line (it sees the line at the
  end of every earlier answer in its history) gets it twice. _drop_echo
  only drops a leading copy.
- Ordinary sentences the new ideation patterns read as self-harm, which
  gives them the 988 line.
- `(?:pastillas|sobredosis|demasiad\\w*).{0,40}a proposito` is quadratic in
  a long word: 4,000 characters take about 0.1 s, 32,000 about 6.7 s.

The rest pin what holds: split_reply's limits and the line's place over a
fuzzed set of replies, the line shown once on the web, and no path that
drops it. Synthetic text only.
"""

from __future__ import annotations

import random

import pytest

from careagents import beta, imessage, routing
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)

FRAMES = (beta.SAMPLE_FRAME, beta.sample_frame("your parent"))
ORIGIN = "https://example.test"


def _queue(c, agent_id, said, request_id):
    r = c.post("/api/chat", json={"agent_id": agent_id, "message": said,
                                  "request_id": request_id}, buffered=False)
    assert r.status_code == 200
    next(iter(r.response))
    r.close()


def _answer(cfg, svc, fake):  # noqa: F811
    from careagents.worker import RunWorker
    w = RunWorker(cfg, fake, svc, "round8-worker")
    while w.run_once():
        pass
    return [row["content"] for rows in fake.logged.values()
            for row in rows if row["role"] == "assistant"][-1]


# --- finding: the model's own closing copy -------------------------------------

def test_a_model_that_closes_with_the_line_says_it_once(cfg, svc, monkeypatch):  # noqa: F811
    reply = ("The sample person's A1c was 6.1 in March.\n\n"
             + beta.SAMPLE_FRAME)
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply=reply)
    _queue(c, agent_id, "how is the a1c?", "echo-end")
    out = _answer(cfg, svc, fake)
    assert out.endswith(beta.SAMPLE_FRAME)
    assert out.count(beta.SAMPLE_FRAME) == 1, out
    texts = imessage.split_reply(out, ORIGIN, agent_id)
    assert "\n\n".join(texts).count(beta.SAMPLE_FRAME) == 1


# --- finding: ordinary sentences read as self-harm --------------------------------

@pytest.mark.parametrize("said", (
    "swallowed a bottle of water",
    "I took a bottle of water to the gym",
    "took the stairs on purpose",
    "i want it all to end well",
    "i hope i don't wake up late tomorrow",
    "i wish i were dead tired",
    "i want to be dead certain",
    "how much salt would kill me",
    "how many calories would it take to kill me",
    "ojalá no despertar tan temprano",
))
def test_ordinary_wording_gets_no_988_line(said):
    assert not routing.is_self_harm(said), said


# --- finding: a quadratic pattern --------------------------------------------

@pytest.mark.parametrize("unit", ("demasiad", "overdos"))
def test_is_crisis_stays_linear(unit):
    import time
    text = unit * (8_000 // len(unit))
    start = time.perf_counter()
    routing.is_crisis(text)
    routing.is_self_harm(text)
    assert time.perf_counter() - start < 0.1


# --- what holds: split_reply ---------------------------------------------------------

def _fuzzed_replies(n=3000, seed=1017):
    rng = random.Random(seed)
    for _ in range(n):
        frame = rng.choice(FRAMES)
        paras = []
        for _ in range(rng.randint(0, 9)):
            size = rng.choice([5, 300, 990, 998, 1000, 1001, 2600])
            word = rng.choice(["a", "word", "x" * 40, "nospaces" * 30])
            p = ((word + " ") * (size // (len(word) + 1) + 1))[:size]
            paras.append(p.strip() or "z")
        if rng.random() < 0.3:
            # Frame-like text in the model's own words.
            paras.insert(rng.randint(0, len(paras)), frame)
        text = "\n\n".join(paras + [frame])
        if rng.random() < 0.2:
            text += f"\n\nSee the chart: {ORIGIN}/chat?agent=a&chart=x"
        yield frame, text


def test_split_reply_keeps_the_line_last_and_every_part_in_bounds():
    for frame, text in _fuzzed_replies():
        parts = imessage.split_reply(text, ORIGIN, "agent-1")
        assert 1 <= len(parts) <= imessage.MAX_TEXT_PARTS
        assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)
        assert parts[-1].endswith(frame)
        if text.count(frame) == 1:
            assert "\n\n".join(parts).count(frame) == 1


def test_frame_like_text_inside_a_paragraph_is_not_taken_for_the_line():
    fake = "Note: " + beta.SAMPLE_FRAME + " (quoted)"
    text = "\n\n".join(["word " * 150, fake, beta.SAMPLE_FRAME])
    parts = imessage.split_reply(text, ORIGIN, "agent-1")
    joined = "\n\n".join(parts)
    assert fake in joined
    assert parts[-1].endswith(beta.SAMPLE_FRAME)


def test_a_reply_without_the_line_is_split_as_before():
    text = "\n\n".join(["word " * 190] * 6)
    parts = imessage.split_reply(text, ORIGIN, "agent-1")
    assert len(parts) == imessage.MAX_TEXT_PARTS
    assert all(len(p) <= imessage.TEXT_PART_LIMIT for p in parts)
    assert beta.SAMPLE_FRAME not in "".join(parts)
    assert parts[-1].endswith(f"{ORIGIN}/chat?agent=agent-1")


# --- what holds: the web shows the line once, nothing drops it ---------------------

@pytest.mark.parametrize("said", ("how is the a1c?", "quiero morir"))
def test_the_web_shows_the_line_once_and_the_text_keeps_it(
        cfg, svc, monkeypatch, said):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(
        cfg, svc, monkeypatch, reply="The sample person's A1c was 6.1.")
    _queue(c, agent_id, said, "web-once")
    out = _answer(cfg, svc, fake)
    assert out.endswith("\n\n" + beta.SAMPLE_FRAME)
    page = c.get(f"/chat?agent={agent_id}").get_data(as_text=True)
    assert page.count(beta.SAMPLE_FRAME) == 1      # the banner only
    assert beta.under_banner(out) == out[:-len("\n\n" + beta.SAMPLE_FRAME)]
    assert imessage.split_reply(out, ORIGIN, agent_id)[-1].endswith(
        beta.SAMPLE_FRAME)


def test_under_banner_never_leaves_an_answer_empty():
    assert beta.under_banner(beta.SAMPLE_FRAME) == beta.SAMPLE_FRAME
    assert beta.under_banner("…\n\n" + beta.SAMPLE_FRAME) == "…"
