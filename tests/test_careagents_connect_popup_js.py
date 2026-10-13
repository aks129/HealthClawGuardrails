"""The connect page's window opens inside the tap, run under node (#937).

iOS Safari and in-app browsers block a window.open that runs after an
await. QA on #940 found the old test only searched home.js for text, so
moving the open after the request stayed green. This one runs the shipped
home.js against a stub page and records whether window.open ran while the
tap's handler was still on the stack, and whether the request came after.

Playwright WebKit does not enforce the gesture rule either, so a real
iPhone check is still owed; this pins the code shape that rule needs.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "home.js")

_HARNESS = r"""
// Runs the shipped home.js against a stub page; reports the order of
// window.open and fetch, and whether open ran inside the tap itself.
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const log = [];
let inTap = false;
function stub(name) {
  const store = {};
  const fn = function () { return stub(name + '()'); };
  return new Proxy(fn, {
    get(t, k) {
      if (k in store) return store[k];
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'then') return undefined;
      if (k === 'dataset') return (store.dataset = {});
      if (k === 'hidden' || k === 'disabled' || k === 'closed') return false;
      if (k === 'forEach') return () => {};
      if (k === 'length') return 0;
      return stub(name + '.' + String(k));
    },
    set(t, k, v) { store[k] = v; return true; },
  });
}
// Plain objects with the listener hooks (a Proxy target ignores assign).
function node(props) {
  const handlers = {};
  const base = stub('node');
  const own = Object.assign({
    addEventListener(type, h) { handlers[type] = h; },
    fire(type) {
      inTap = true;
      try { return handlers[type]({ preventDefault() {} }); }
      finally { inTap = false; }
    },
  }, props);
  return new Proxy(own, { get(t, k) { return k in t ? t[k] : base[k]; } });
}
const modal = node({ hidden: true });
const agree = node({});
const cancel = node({});
const card = node({ dataset: { tenant: 'ca-x' } });
const tile = node({ dataset: { connector: 'fasten', consent: '1' } });
const refresh = node({ dataset: { conn: 'c1' }, closest: () => card });
const ids = { 'consent-modal': modal, 'consent-agree': agree,
              'consent-cancel': cancel };
global.document = new Proxy({
  getElementById: (id) => ids[id] || stub('#' + id),
  querySelector: () => null,
  querySelectorAll: (sel) => sel === '.connector-tile' ? [tile]
    : sel === '.conn-refresh' ? [refresh] : [],
}, { get(t, k) { return k in t ? t[k] : stub('document.' + String(k)); } });
const page = { closed: false, opener: 1, location: { href: '' } };
let answer = null;
global.window = new Proxy({
  open(url, target) { log.push(['open', url, inTap]); return page; },
}, { get(t, k) { return k in t ? t[k] : stub('window.' + String(k)); } });
global.location = { reload() { log.push(['reload']); },
                    assign() {}, hash: '', search: '', pathname: '/home' };
global.navigator = stub('navigator');
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.setInterval = () => 0; global.clearInterval = () => {};
global.requestAnimationFrame = () => 0;
global.fetch = (url) => {
  log.push(['fetch', url, inTap]);
  return Promise.resolve({ ok: true, status: 200,
                           json: () => Promise.resolve(answer) });
};
new Function(src)();
(async () => {
  const which = process.argv[2];
  if (which === 'tile') {
    answer = { id: 'c2', status: 'pending', connect_url: 'https://e/connect/t' };
    const done = tile.fire('click');
    await Promise.resolve();
    inTap = true;                  // the "I agree" tap
    try { agree.onclick(); } finally { inTap = false; }
    await done;
  } else {
    answer = { status: 'reauth', reauth_url: 'https://e/connect/r' };
    await refresh.fire('click');
  }
  process.stdout.write(JSON.stringify({ log, href: page.location.href,
                                        opener: page.opener }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run(which: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(HOME_JS), which],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _steps(got: dict, url_part: str) -> list:
    return [e for e in got["log"]
            if e[0] == "open" or (e[0] == "fetch" and url_part in e[1])]


def test_the_connect_tile_opens_the_window_on_i_agree_before_the_request():
    """MUTATION: move `page = openBlank()` after `await post(...)` -> the
    open runs outside the tap, after the request, and this goes red."""
    got = _run("tile")
    assert _steps(got, "/api/connections/fasten") == [
        ["open", "", True],
        ["fetch", "/api/connections/fasten", False]]
    # Then pointed at the page the answer named, with the opener cut.
    assert got["href"] == "https://e/connect/t"
    assert got["opener"] is None


def test_refresh_opens_the_window_on_the_tap_before_the_request():
    got = _run("refresh")
    assert _steps(got, "/refresh") == [
        ["open", "", True],
        ["fetch", "/api/connections/c1/refresh", True]]
    assert got["href"] == "https://e/connect/r"
    assert got["opener"] is None
