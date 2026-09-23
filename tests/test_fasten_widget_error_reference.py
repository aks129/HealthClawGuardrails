"""A failed Fasten connection shows the person an id they can quote (#461).

Fasten asked, on support thread FAS-864, for "a request id so we can
correlate the error in our logs". #462 added the server record, but the
connect page could still never produce that id, for three reasons read out
of the widget bundle the iframe actually loads
(embed.connect.fastenhealth.com, main-*.js):

- The widget posts `JSON.stringify(event)` to the parent, so `event.data`
  is a string. The handler read `.type` off it and matched nothing.
- The discriminator is `event_type`, not `type`.
- `widget.config_error` carries only `{api_mode, event_type}`. The error
  code and `request_id` ride on `patient.connection_failed`, in `data`,
  next to `error_description` and our own external id.

These run the page's real script (rendered by Flask) under node with a
stub DOM, post the shape the bundle emits, and read what the person sees
and what goes to the server. The rule both sides follow: only the error
code and the request id leave the event. Nothing else is displayed or sent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

_ORIGIN = "https://embed.connect.fastenhealth.com"
_REQUEST_ID = "req_01J8XKQ2M4YB3"
_CODE = "fasten_unauthorized_client"
_DESCRIPTION = "An error occurred while retrieving vault profile"
_REFERENCE = "ccd_0123456789ab"

#: `patient.connection_failed` exactly as the bundle builds it
#: (publishOrgConnectionComplete with `error` set), then JSON.stringify'd.
_CONNECTION_FAILED = json.dumps({
    "api_mode": "live",
    "event_type": "patient.connection_failed",
    "data": {
        "external_state": "st_abc",
        "vault_profile_connection_id": "vpc_abc",
        "external_id": "demo-tenant",
        "request_id": _REQUEST_ID,
        "error": _CODE,
        "error_description": _DESCRIPTION,
        "patient_auth_type": "tefca_direct",
    },
})

#: `widget.config_error` as the bundle builds it: no code, no request id.
_CONFIG_ERROR = json.dumps({"api_mode": "live",
                            "event_type": "widget.config_error"})

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const message = JSON.parse(process.argv[2]);
function el() {
  return {
    textContent: '', className: '', style: {}, children: [],
    classList: { add() {}, remove() {} },
    addEventListener() {},
    parentNode: { insertBefore() {} },
  };
}
const els = {};
globalThis.document = {
  getElementById(id) { return (els[id] = els[id] || el()); },
  createElement: el,
};
let handler = null;
globalThis.window = globalThis;
window.addEventListener = (name, fn) => { if (name === 'message') handler = fn; };
const fetches = [];
globalThis.fetch = async (url, opts) => {
  fetches.push({ url, body: opts && opts.body });
  return { ok: true, status: 202, json: async () => ({ reference: '%REF%' }) };
};
console.log = () => {};
new Function(src)();
(async () => {
  await handler(message);
  process.stdout.write(JSON.stringify({
    status: document.getElementById('stitch-status').textContent,
    fetches,
  }));
})();
""".replace("%REF%", _REFERENCE)


def _script(client, monkeypatch) -> str:
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_fake")
    r = client.get("/connect/demo-tenant")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    handler = [s for s in scripts if "stitch-status" in s]
    assert len(handler) == 1, "expected exactly one widget handler script"
    return handler[0]


def _run(client, monkeypatch, tmp_path, data):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    path = tmp_path / "handler.js"
    path.write_text(_script(client, monkeypatch), encoding="utf-8")
    out = subprocess.run(
        ["node", "-e", _HARNESS, "--", str(path),
         json.dumps({"origin": _ORIGIN, "data": data})],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _diagnostic_body(result):
    posts = [f for f in result["fetches"] if "connect-diagnostic" in f["url"]]
    assert len(posts) == 1, f"expected one diagnostic POST: {result['fetches']}"
    return json.loads(posts[0]["body"])


def test_a_failed_connection_shows_the_request_id(client, monkeypatch, tmp_path):
    """The whole point: something the person can paste into an email.

    MUTATION: drop the request id from the status message -> red.
    """
    result = _run(client, monkeypatch, tmp_path, _CONNECTION_FAILED)
    assert _REQUEST_ID in result["status"], result["status"]
    assert _CODE in result["status"], result["status"]
    assert _REFERENCE in result["status"], result["status"]


def test_only_the_code_and_request_id_leave_the_event(
        client, monkeypatch, tmp_path):
    """The event also carries `error_description`, our external id and the
    vault connection id. None of it is ours to show or to log.

    MUTATION: post `{payload: data}` (the whole event) -> red.
    """
    result = _run(client, monkeypatch, tmp_path, _CONNECTION_FAILED)
    assert _diagnostic_body(result) == {"payload": {
        "event_type": "patient.connection_failed",
        "error": _CODE,
        "request_id": _REQUEST_ID,
    }}
    for leaked in (_DESCRIPTION, "vpc_abc", "st_abc"):
        assert leaked not in result["status"], leaked


def test_a_config_error_without_an_id_still_gives_a_reference(
        client, monkeypatch, tmp_path):
    """widget.config_error carries no request id. The person still gets our
    reference, and the page does not invent a request id."""
    result = _run(client, monkeypatch, tmp_path, _CONFIG_ERROR)
    assert _REFERENCE in result["status"], result["status"]
    assert "request id" not in result["status"], result["status"]
    assert _diagnostic_body(result) == {"payload": {
        "event_type": "widget.config_error"}}


def test_a_request_id_that_is_not_token_shaped_is_dropped(
        client, monkeypatch, tmp_path):
    """The payload shape is not a published contract. A request id that is
    free text could be anything, so it is neither shown nor sent.

    MUTATION: remove the token-shape check -> red.
    """
    event = json.loads(_CONNECTION_FAILED)
    event["data"]["request_id"] = "Jane Doe <img src=x>"
    result = _run(client, monkeypatch, tmp_path, json.dumps(event))
    assert "Jane Doe" not in result["status"]
    assert "request_id" not in _diagnostic_body(result)["payload"]
