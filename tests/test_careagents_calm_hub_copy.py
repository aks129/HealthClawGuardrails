"""Plain words (calm hub spec section 7)."""

from __future__ import annotations

import pathlib
import re

from careagents import connectors
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture

_CA = pathlib.Path(__file__).resolve().parents[1] / "careagents"


def _banner(html):
    sentence = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', html,
                         re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", "", sentence).split())


def test_the_hub_banner_matches_the_account(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: sample records. Things will break, tell us.")
    with c.session_transaction() as s:
        aid = s["account_id"]
    svc.add_connection(aid, "direct", "ca-real", "Upload", status="active")
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: your records are connected. Things will break, tell us.")


def test_the_landing_banner_keeps_its_words_without_the_em_dash(app):
    assert _banner(app.test_client().get("/").get_data(as_text=True)) == (
        "Beta: made-up records only. Things will break, tell us.")


def test_the_stale_telegram_handler_is_gone():
    js = (_CA / "static" / "home.js").read_text()
    assert "tg-surface" not in js and "tg-state" not in js


def test_imessage_instructions_name_no_deployment():
    src = (_CA / "app.py").read_text()
    assert "iMessage isn't configured on this deployment" not in src


def test_a_new_provider_connection_is_named_in_plain_words(cfg):
    plan = connectors.start("fasten", None, cfg, FakeClient(),
                            real_records=True)
    assert plan["label"] == "Records from your doctor"
    # chat.html reads "Your records from {{ intake.provider }} haven't
    # arrived yet", so the provider must read after "from".
    assert plan["provider"] == "your doctor"


def test_the_sample_refresh_reason_has_no_em_dash(cfg):
    """Patient copy is em-dash-free (QA, calm hub PR 2)."""
    out = connectors.refresh("sample", "t-1", None, cfg, FakeClient())
    assert out["unsupported"] is True
    assert "—" not in out["reason"]
    assert out["reason"].startswith("Sample records are made up")


def test_the_waiting_chat_notice_reads_as_a_sentence(cfg, svc, monkeypatch):
    class _NotYet(FakeClient):
        def search(self, tenant, resource_type, params=None):
            return {"total": 0, "entry": []}

    from careagents.app import create_app
    a = create_app(config=cfg, client=_NotYet(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    with c.session_transaction() as s:
        aid = s["account_id"]
    agent = svc.create_agent(aid, "Juniper", "calm", conn["id"])
    page = " ".join(c.get(f"/chat?agent={agent}").get_data(as_text=True).split())
    assert "Your records from your doctor haven't arrived yet" in page
    assert "from Records from" not in page


# --- Patient-tester findings on PR #843 (V4/G7) ---------------------------


def _without_comments(path: pathlib.Path) -> str:
    """What a person can see: the file with its developer comments removed.

    Whole-line `//` comments and `/* */` blocks in JS, `<!-- -->` and
    `{# #}` in templates. A trailing `//` after code is kept, so a word
    hidden there still fails the scan rather than slipping past it.
    """
    text = path.read_text()
    if path.suffix == ".js":
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("//"))
    else:
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return text


def _patient_facing_files():
    return (sorted((_CA / "templates").glob("**/*.html"))
            + sorted((_CA / "static").glob("*.js")))


def test_no_patient_facing_copy_says_phi():
    """"PHI" is our word, not the patient's. Every CareAgents template and
    script is scanned for it, comments aside."""
    files = _patient_facing_files()
    assert len(files) >= 10
    hits = [f"{p.relative_to(_CA)}:{n}"
            for p in files
            for n, line in enumerate(_without_comments(p).splitlines(), 1)
            if re.search(r"\bPHI\b", line)]
    assert hits == []


def test_the_scan_sees_a_phi_word_that_is_not_in_a_comment(tmp_path):
    js = tmp_path / "x.js"
    js.write_text('// PHI in a comment\nsay("a PHI-free log");\n')
    assert "PHI" in _without_comments(js)
    html = tmp_path / "x.html"
    html.write_text("<!-- PHI -->{# PHI #}<p>PHI</p>")
    assert _without_comments(html) == "<p>PHI</p>"


_LOG_LINE = ("We keep a log of who looked at your records, with no health "
             "details in it.")


def _flat(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _box(page: str, box_id: str) -> str:
    return re.search(rf'id="{box_id}".*?</div>\s*</div>', page,
                     re.S).group(0)


def test_the_account_delete_box_talks_about_the_account(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    box = _box(c.get("/settings").get_data(as_text=True), "delete-modal")
    assert re.search(r'<h3 id="delete-title">\s*Delete your account\?\s*</h3>',
                     box)
    assert re.search(r'id="delete-confirm"[^>]*>\s*Delete my account\s*<', box)
    words = _flat(box)
    assert ("This deletes your sign-in, your assistants and your "
            "connections, with the records behind them.") in words
    assert "Delete these records" not in words
    assert _LOG_LINE in words
    assert "delete-label" not in box


def test_the_records_delete_box_still_names_the_records(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    box = _box(c.get("/home").get_data(as_text=True), "delete-modal")
    assert "Delete these records?" in box
    assert 'id="delete-label"' in box
    assert _LOG_LINE in _flat(box)
    assert "Delete your account" not in box


def test_the_account_button_passes_no_records_label():
    js = (_CA / "static" / "home.js").read_text()
    assert "your account and all its records" not in js


def test_the_settings_and_goodbye_lines_say_what_is_kept_plainly(
        app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert _LOG_LINE in _flat(c.get("/settings").get_data(as_text=True))
    landing = _flat(app.test_client().get("/?deleted=1")
                    .get_data(as_text=True))
    assert "Your account is deleted." in landing
    assert _LOG_LINE in landing


def test_deleting_an_assistant_does_not_promise_its_chat_stays(
        app, svc, monkeypatch):
    """The transcript is keyed to the assistant's id in HealthClaw, and a
    new assistant gets a new id, so the old chat cannot be opened again."""
    c = app.test_client()
    _login(c, svc, monkeypatch)
    c.post("/api/connections/sample")
    box = _flat(_box(c.get("/home").get_data(as_text=True),
                     "agent-delete-modal"))
    assert "conversation stay" not in box
    assert ("Your records stay. This assistant goes, and you won't be able "
            "to open its chat again.") in box
