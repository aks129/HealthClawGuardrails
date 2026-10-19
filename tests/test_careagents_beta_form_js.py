"""The /beta form's answers, run in node against the real beta.js (#979
QA): only a send that happened is green and says "We sent"; a wait, a
refused send and an unconfirmed one each say what happened; "Send the
email again" comes only once the wait is over; a failed re-send keeps
what the panel said and is never green.

A small fake DOM, fetch and clock; no browser. Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

BETA_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "beta.js")

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const steps = JSON.parse(process.argv[2]);

function el(id) {
  const classes = new Set();
  return {
    id, textContent: '', hidden: false, disabled: false, checked: true,
    value: '', handlers: {},
    classList: {
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    classes,
    scrollIntoView() {},
    addEventListener(kind, fn) { this.handlers[kind] = fn; },
  };
}
const els = {};
for (const id of ['beta-form', 'beta-err', 'beta-consent', 'beta-done',
                  'beta-done-text', 'beta-again']) els[id] = el(id);
els['beta-done'].hidden = true;
els['beta-again'].hidden = true;
els['beta-err'].hidden = true;
const submit = el('submit');
const values = { first_name: 'Robin', email: 'robin@example.com',
                 mobile: '', ref: '', invite: '', website: '' };
els['beta-form'].elements = Object.fromEntries(
  Object.entries(values).map(([k, v]) => [k, { value: v }]));
els['beta-form'].querySelector = () => submit;

let clock = 0;
let timers = [];
const sandbox = {
  document: { getElementById: (id) => els[id] },
  setTimeout: (fn, ms) => { const t = { at: clock + ms, fn };
                            timers.push(t); return t; },
  clearTimeout: (t) => { timers = timers.filter((x) => x !== t); },
  fetch: null, JSON, Math,
};
let answer = null;
sandbox.fetch = async () => {
  if (answer.throw) throw new Error('offline');
  return { ok: answer.status >= 200 && answer.status < 300,
           status: answer.status, json: async () => answer.body };
};
new Function(...Object.keys(sandbox), src)(...Object.values(sandbox));

const flush = () => new Promise((r) => setImmediate(r));
function state() {
  const done = els['beta-done'];
  return { text: els['beta-done-text'].textContent,
           green: done.classes.has('form-ok'),
           warn: done.classes.has('form-warn'),
           doneHidden: done.hidden, againHidden: els['beta-again'].hidden,
           err: els['beta-err'].hidden ? null : els['beta-err'].textContent };
}
(async () => {
  const out = [];
  for (const s of steps) {
    if (s.tick !== undefined) {
      clock += s.tick * 1000;
      const due = timers.filter((t) => t.at <= clock);
      timers = timers.filter((t) => t.at > clock);
      due.forEach((t) => t.fn());
    } else {
      answer = s.answer;
      const h = s.do === 'again' ? els['beta-again'].handlers.click
        : els['beta-form'].handlers.submit;
      await h({ preventDefault() {} });
      for (let i = 0; i < 5; i++) await flush();
    }
    out.push(state());
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(*steps) -> list[dict]:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(BETA_JS), json.dumps(steps)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _submit(status, body=None, do="submit"):
    return {"do": do, "answer": {"status": status, "body": body or {}}}


SENT = {"ok": True, "sent": True}


def test_a_send_that_happened_is_green_and_says_we_sent():
    (s,) = _run(_submit(200, SENT))
    assert s["green"] and not s["warn"] and not s["doneHidden"]
    assert s["text"].startswith("Thanks, Robin. We sent an email to "
                                "robin@example.com.")
    assert "Get started" in s["text"]


def test_a_wait_says_when_and_is_not_green():
    (s,) = _run(_submit(200, {"sent": False, "retry_after": 42}))
    assert not s["green"] and s["warn"]
    assert "a moment ago, so we didn't send another yet" in s["text"]
    assert "ask for a new one in 42 seconds." in s["text"]
    assert "We sent an email to robin@example.com." not in s["text"]


def test_a_refused_send_says_so_and_offers_again_at_once():
    (s,) = _run(_submit(502, {"error": "not_sent", "sent": False}))
    assert not s["green"] and s["warn"]
    assert s["text"].startswith("Sorry, Robin. We couldn't send the email")
    assert s["againHidden"]          # due now, shown on the next tick
    (_, s) = _run(_submit(502, {"error": "not_sent", "sent": False}),
                  {"tick": 0})
    assert not s["againHidden"]


def test_an_unconfirmed_send_says_we_do_not_know():
    (s,) = _run(_submit(202, {"sent": None, "retry_after": 60}))
    assert not s["green"] and s["warn"]
    assert s["text"].startswith("We couldn't confirm the email reached "
                                "robin@example.com.")
    assert "ask again in 60 seconds." in s["text"]


def test_send_again_appears_only_once_the_wait_is_over():
    states = _run(_submit(200, {"sent": False, "retry_after": 42}),
                  {"tick": 41}, {"tick": 1})
    assert [s["againHidden"] for s in states] == [True, True, False]
    states = _run(_submit(200, SENT), {"tick": 59}, {"tick": 1})
    assert [s["againHidden"] for s in states] == [True, True, False]


def test_a_failed_resend_keeps_what_was_said_and_is_never_green():
    for answer in ({"status": 429, "body": {"error": "rate_limited"}},
                   {"throw": True}):
        states = _run(_submit(200, SENT), {"tick": 60},
                      {"do": "again", "answer": answer})
        s = states[-1]
        assert not s["green"] and s["warn"], answer
        assert s["text"].startswith("Thanks, Robin. We sent an email to "
                                    "robin@example.com."), s["text"]
        assert ("Too many tries" in s["text"]
                or "Couldn't reach us" in s["text"]), s["text"]
        assert not s["againHidden"]
    # Twice in a row says the refusal once, not twice.
    states = _run(_submit(200, SENT), {"tick": 60},
                  _submit(429, {"error": "rate_limited"}, do="again"),
                  _submit(429, {"error": "rate_limited"}, do="again"))
    assert states[-1]["text"].count("Too many tries") == 1


def test_a_refusal_on_the_form_shows_next_to_send():
    (s,) = _run(_submit(400, {"error": "email"}))
    assert s["err"] == "Please check your email address."
    assert s["doneHidden"]


@pytest.mark.parametrize("seconds,said", [
    (90, "in 90 seconds"), (91, "in 2 minutes"), (2999, "in 50 minutes"),
    (3540, "in about an hour"), (5400, "in about an hour"),
    (5401, "in about 2 hours"), (80000, "in about 23 hours"),
])
def test_a_long_wait_is_said_in_minutes_or_hours(seconds, said):
    (s,) = _run(_submit(200, {"sent": False, "retry_after": seconds}))
    assert said + "." in s["text"], s["text"]
    assert "3540 seconds" not in s["text"]
