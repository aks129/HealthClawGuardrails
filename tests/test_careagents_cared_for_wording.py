"""Someone the holder cares for is named in their own chat (FHI-44), and
the hub polish that followed person cards (FHI-45).

FHI-44: Mom's chat said "made-up records, not yours", offered "Make my
visit brief", and a refused removal said "This is your assistant". Each
now names the person when they are not the account holder. The made-up
findings themselves stay the sample person's, who stands in for them: never
advice about the real person (patient test on #952). Anything that can reach the model (the system prompt,
a starter tap, the sample line every answer opens with) names them by
relationship only ("your parent"): the nickname is the holder's own typed
text and never leaves the page.

FHI-45: deleting a cared-for person's only source says, in the dialog,
that they leave the hub too; Settings says "Your assistants" over several;
the empty approvals list says "assistant", not "agent".

Synthetic data only.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import pathlib
import re

from careagents import beta
from careagents import hub as hub_view
from careagents.personas import system_prompt
from tests.careagents_legacy_helpers import legacy_agent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, cfg, svc)
from tests.test_careagents_hub_person_cards import _client
from tests.test_careagents_sample_framing import _answer

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "careagents"
_HOME_JS = (_STATIC / "static" / "home.js").read_text()
_MODAL = (_STATIC / "templates" / "_delete_modal.html").read_text()

NICK = "Zorblax"          # a nickname no fixed string could ever contain
MOM_FRAME = ("These are made-up records. Nothing here is about your "
             "parent, or about you.")


def _with_mom(cfg, svc, monkeypatch, hc=None):
    c, aid, hc = _client(cfg, svc, monkeypatch, people=True, hc=hc)
    mine = c.post("/api/connections/sample").get_json()["agent_id"]
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": NICK}).get_json()
    return c, aid, hc, mine, mom


def _starters(page: str) -> list[str]:
    block = page[page.index('id="starters"'):page.index('id="composer"')]
    return re.findall(r'class="starter">([^<]*)<', block)


def _banner(page: str) -> str:
    m = re.search(r'class="beta-banner sample-banner" role="note">([^<]*)<',
                  page)
    return m.group(1) if m else ""


# --- FHI-44: one relationship-only name, for everything model-bound ---------

def test_relation_of_names_the_relationship_never_the_nickname():
    me = {"id": "p1", "kind": "self", "relationship": None, "nickname": None}
    assert hub_view.relation_of(me) == ""
    mom = {"id": "p2", "kind": "other", "relationship": "parent",
           "nickname": NICK}
    assert hub_view.relation_of(mom) == "your parent"
    assert hub_view.relation_of({"kind": "other", "relationship": "child",
                                 "nickname": NICK}) == "your child"
    # "other", or a value outside the enum, is never shown raw.
    assert hub_view.relation_of({"kind": "other", "relationship": "other",
                                 "nickname": NICK}) == "someone you care for"
    assert hub_view.relation_of({"kind": "other", "relationship": "boss",
                                 "nickname": None}) == "someone you care for"


def test_the_sample_line_names_the_person_when_they_are_not_the_holder():
    assert beta.sample_frame("") == beta.SAMPLE_FRAME
    assert beta.sample_frame("your parent") == MOM_FRAME
    assert "not yours" not in MOM_FRAME
    # The page strips the person's own line from a replayed answer.
    assert beta.under_banner(f"{MOM_FRAME}\n\nHi.", MOM_FRAME) == "Hi."
    assert beta.under_banner(f"{MOM_FRAME}\n\nHi.") == f"{MOM_FRAME}\n\nHi."


def test_an_answer_stored_before_the_person_line_loses_the_old_line_too():
    """Answers stored before #952 open with the holder's "not yours" line.
    In Mom's chat it would show under her banner; it goes by the same rule:
    an exact leading copy only, and never down to nothing (QA/sec L1)."""
    old = f"{beta.SAMPLE_FRAME}\n\nHi."
    assert beta.under_banner(old, MOM_FRAME) == "Hi."
    # Only the exact copy at the start, never a reworded or later one.
    assert beta.under_banner(f"Note.\n\n{old}", MOM_FRAME) == (
        f"Note.\n\n{old}")
    loose = beta.SAMPLE_FRAME.lower() + "\n\nHi."
    assert beta.under_banner(loose, MOM_FRAME) == loose
    # An answer that is only the old line is shown, never emptied.
    assert beta.under_banner(beta.SAMPLE_FRAME + "\n\n", MOM_FRAME) == (
        beta.SAMPLE_FRAME + "\n\n")
    assert beta.under_banner(beta.SAMPLE_FRAME, MOM_FRAME) == (
        beta.SAMPLE_FRAME)


def test_moms_replayed_history_drops_the_old_line(cfg, svc, monkeypatch):
    hc = FakeClient()
    c, _, hc, _, mom = _with_mom(cfg, svc, monkeypatch, hc=hc)
    old = f"{beta.SAMPLE_FRAME}\n\nThe A1c is 5.4."
    new = f"{MOM_FRAME}\n\nThe LDL is 96."
    hc.recent_messages = lambda *a, **k: [
        {"role": "user", "content": "How are the labs?"},
        {"role": "assistant", "content": old},
        {"role": "user", "content": "And the LDL?"},
        {"role": "assistant", "content": new}]
    page = c.get(f"/chat?agent={mom['agent_id']}").get_data(as_text=True)
    assert "not yours" not in page
    assert "The A1c is 5.4." in page and "The LDL is 96." in page
    # Her own line shows once, in the banner, not again in a bubble.
    assert page.count(MOM_FRAME) == 1


def test_moms_chat_banner_and_starters_name_her_by_relationship(
        cfg, svc, monkeypatch):
    c, _, _, mine, mom = _with_mom(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={mom['agent_id']}").get_data(as_text=True)
    assert _banner(page) == MOM_FRAME
    assert "not yours" not in page
    starters = _starters(page)
    assert starters == [
        "Fill out a new doctor&#39;s intake form for my parent",
        "Make a visit brief for my parent",
        "Explain the latest labs for my parent"]
    # A starter tap is a message to the model: never the nickname.
    assert not [s for s in starters if NICK in s]
    # The holder's own chat reads as before.
    own = c.get(f"/chat?agent={mine}").get_data(as_text=True)
    assert _banner(own) == beta.SAMPLE_FRAME
    assert _starters(own) == ["Fill out my intake form for a new doctor",
                              "Make my visit brief",
                              "Explain my latest labs"]


def test_moms_turn_is_prompted_about_your_parent_never_the_nickname(
        cfg, svc, monkeypatch):
    c, _, hc, _, mom = _with_mom(cfg, svc, monkeypatch, hc=FakeClient())
    prompts = []
    answer = _answer(c, hc, mom["agent_id"], cfg, svc, monkeypatch, prompts)
    assert prompts
    for p in prompts:
        assert NICK not in p
        # The findings stay with the sample person (patient test on #952).
        assert "say it about the sample person" in p
        assert "say it about your parent" not in p
        assert "stands in for your parent" in p
    # The line every answer ends with is model-visible history too.
    assert answer.endswith("\n\n" + MOM_FRAME)
    assert NICK not in answer


def test_the_worker_is_never_handed_the_nickname(cfg, svc, monkeypatch):
    """Structural: the worker cannot leak what it is never given."""
    _, _, _, mine, mom = _with_mom(cfg, svc, monkeypatch)
    ctx = svc.get_worker_agent_context(mom["agent_id"])
    assert ctx["person"] == {"kind": "other", "relationship": "parent"}
    assert NICK not in repr(ctx)
    assert svc.get_worker_agent_context(mine)["person"]["kind"] == "self"


def test_the_holders_turn_prompt_is_unchanged(cfg, svc, monkeypatch):
    c, _, hc, mine, _ = _with_mom(cfg, svc, monkeypatch, hc=FakeClient())
    prompts = []
    answer = _answer(c, hc, mine, cfg, svc, monkeypatch, prompts)
    assert prompts and all(
        p == system_prompt("Juniper", "calm", sample=True) for p in prompts)
    assert answer.endswith("\n\n" + beta.SAMPLE_FRAME)


def test_the_prompt_says_whose_records_for_someone_you_care_for():
    holder = system_prompt("Rowan", "calm", sample=True)
    assert system_prompt("Rowan", "calm", sample=True, who="") == holder
    sample = system_prompt("Rowan", "calm", sample=True, who="your parent")
    assert "your parent" in sample and "made up" in sample
    assert "say it about the sample person" in sample
    # Real records for someone you care for (after owner decision D1):
    # whose they are, and nothing about made-up records.
    real = system_prompt("Rowan", "calm", who="your child")
    assert "your child" in real and "made up" not in real
    assert system_prompt("Rowan", "calm", who="") == system_prompt(
        "Rowan", "calm")


def test_refusing_to_remove_moms_assistant_names_her_by_relationship(
        cfg, svc, monkeypatch):
    """By relationship, never the raw nickname: a JSON error is a string
    any client may one day render (security review of #952, I1)."""
    c, _, _, mine, mom = _with_mom(cfg, svc, monkeypatch)
    r = c.delete(f"/api/agents/{mom['agent_id']}")
    assert r.status_code == 409
    err = r.get_json()["error"]
    assert err == ("This is the assistant for your parent, so it can't be "
                   "removed on its own. To remove it, delete their records "
                   "on your hub.")
    assert NICK not in r.get_data(as_text=True)
    r = c.delete(f"/api/agents/{mine}")
    assert r.status_code == 409
    assert r.get_json()["error"].startswith("This is your assistant")


def test_without_a_nickname_the_refusal_names_the_relationship(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    kid = c.post("/api/people", json={"relationship": "child"}).get_json()
    err = c.delete(f"/api/agents/{kid['agent_id']}").get_json()["error"]
    assert "the assistant for your child" in err


# --- FHI-45.1: the delete dialog says the person leaves the hub -------------

def test_deleting_moms_only_source_says_she_leaves_the_hub(
        cfg, svc, monkeypatch):
    c, aid, _, _, mom = _with_mom(cfg, svc, monkeypatch)
    rows = {r["id"]: r for r in hub_view.build(svc.list_home(aid), 0.0)
            ["records"]}
    assert rows[mom["connection_id"]]["leaves"] == (
        f"{NICK} will be removed from your hub too.")
    assert [r["leaves"] for r in rows.values()
            if r["id"] != mom["connection_id"]] == [""]
    page = c.get("/home").get_data(as_text=True)
    assert (f'data-leaves="{NICK} will be removed from your hub too."'
            in page)


def test_a_second_source_keeps_the_person_and_says_nothing():
    home = {"people": [{"id": "p2", "kind": "other",
                        "relationship": "child", "nickname": None}],
            "agents": [],
            "connections": [
                {"id": "c1", "kind": "sample", "label": "Sample records",
                 "status": "active", "person_id": "p2"},
                {"id": "c2", "kind": "sample", "label": "Sample records",
                 "status": "active", "person_id": "p2"}]}
    assert {r["leaves"] for r in hub_view.build(home, 0.0)["records"]} == {""}
    home["connections"].pop()
    assert hub_view.build(home, 0.0)["records"][0]["leaves"] == (
        "Your child will be removed from your hub too.")


def test_the_delete_dialog_shows_the_leaves_line():
    assert 'id="delete-leaves"' in _MODAL
    ask = _HOME_JS.split("function askToDelete(")[1].split("\n  }\n")[0]
    assert '$("delete-leaves")' in ask
    click = _HOME_JS.split('document.querySelectorAll(".conn-delete")')[1]
    assert "btn.dataset.leaves" in click[:900]


# --- FHI-45.2 and 45.3: wording ---------------------------------------------

def test_settings_heading_is_plural_over_several_assistants(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    one = c.get("/settings").get_data(as_text=True)
    assert "<h2>Your assistant</h2>" in one
    c.post("/api/people", json={"relationship": "parent", "nickname": "Mom"})
    two = c.get("/settings").get_data(as_text=True)
    assert "<h2>Your assistants</h2>" in two
    assert "<h2>Your assistant</h2>" not in two


def test_a_legacy_extra_also_makes_the_heading_plural(cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    legacy_agent(svc, conn)
    assert "<h2>Your assistants</h2>" in c.get("/settings").get_data(
        as_text=True)


def test_the_empty_approvals_list_says_assistant(cfg, svc, monkeypatch):
    hc = FakeClient()
    hc.pending_actions = lambda tenant: []
    c, _, _ = _client(cfg, svc, monkeypatch, hc=hc)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    page = " ".join(c.get(f"/agents/{agent}/approvals").get_data(
        as_text=True).split())
    assert "When an assistant proposes something on these records" in page
    assert "an agent" not in page


def test_moms_review_card_says_whose_records_it_was_filled_from(
        cfg, svc, monkeypatch):
    """The card a waiting intake form draws said "filled it in from your
    records" in Mom's chat. Page text only, never sent to a model."""
    chat_js = (_STATIC / "static" / "chat.js").read_text()
    card = chat_js.split("function addReviewCard(")[1].split("\n  }\n")[0]
    assert '" filled it in from " + (WHOSE || "your records")' in card
    assert 'WHOSE ? "Review & approve the intake form"' in card
    pdf = chat_js.split("function addPdfCard(")[1].split("\n  }\n")[0]
    assert 'WHOSE ? "The intake form is ready"' in pdf
    c, _, _, mine, mom = _with_mom(cfg, svc, monkeypatch)
    page = c.get(f"/chat?agent={mom['agent_id']}").get_data(as_text=True)
    assert f'window.CARE_WHOSE = "{NICK}\\u0027s records";' in page
    own = c.get(f"/chat?agent={mine}").get_data(as_text=True)
    assert 'window.CARE_WHOSE = "";' in own


# --- #952 patient test: made-up findings stay with the sample person -------

def test_made_up_findings_for_someone_you_care_for_stay_with_the_sample():
    """Mom's labs answer said "Contact your parent's doctor promptly" under
    a banner saying nothing here is about your parent. The made-up records
    stand in for the person; findings and advice are the sample person's,
    as in the holder's own sample voice."""
    p = system_prompt("Rowan", "calm", sample=True, who="your parent")
    assert "a made-up sample person, who stands in for your parent" in p
    assert "say it about the sample person" in p
    assert '"The sample person should contact their doctor"' in p
    assert ("Never tell the person you are talking to to contact a doctor "
            "for your parent, or anyone, about these results") in p
    assert ("never ask them to check or compare these records with what "
            "they know about your parent") in p
    # A name the person types ("Mom") does not make the records hers.
    assert "even when they call the records by a name" in p
    assert "say it about your parent" not in p
    assert "made-up records for your parent" not in p


# --- #952 patient test: the intake journey in Mom's chat -------------------

_HANDLERS = (pathlib.Path(__file__).resolve().parents[1] / "templates"
             / "_review_handlers.html").read_text()
_ENGINE_REVIEW = (pathlib.Path(__file__).resolve().parents[1] / "templates"
                  / "action_review.html").read_text()


def _engine_like_review(action_id):
    """The engine's sample review, reduced to the strings CareAgents words
    for someone the holder cares for. Each is pinned against the engine's
    own template below, so engine drift fails here, not in a browser."""
    return 200, (
        "<html><title>Review — HealthClaw Guardrails</title>"
        '<div class="alert alert-warning mb-3" role="note">'
        f"{beta.SAMPLE_FRAME}</div>"
        f"<form action=\"/r6/actions/{action_id}/review\"></form>"
        "<script>gateMsg.textContent = 'Confirmed. Your form is ready. ';"
        "a.textContent = 'Open your form (PDF)';"
        "gateMsg.appendChild(document.createTextNode("
        "' to save it, print it or send it to your new doctor.'));"
        "</script></html>")


def test_the_strings_moms_review_rewords_are_the_engines_own():
    from r6 import voice
    assert voice.SAMPLE_BANNER == beta.SAMPLE_FRAME
    assert 'role="note">{{ sample_banner }}</div>' in _ENGINE_REVIEW
    assert "'Confirmed. Your form is ready. '" in _HANDLERS
    assert "'Open your form (PDF)'" in _HANDLERS
    assert ("' to save it, print it or send it to your new doctor.'"
            in _HANDLERS)


def test_moms_review_page_names_her_records_not_yours(cfg, svc, monkeypatch):
    hc = FakeClient()
    hc.fetch_review_page = lambda tenant, action_id, voice=None: (
        _engine_like_review(action_id))
    c, _, hc, mine, mom = _with_mom(cfg, svc, monkeypatch, hc=hc)
    page = c.get(f"/review/{mom['agent_id']}/act-1").get_data(as_text=True)
    assert MOM_FRAME in page and "not yours" not in page
    assert "Confirmed. The form is ready. " in page
    assert "Open the form (PDF)" in page
    assert "your new doctor" not in page and "Your form" not in page
    assert NICK not in page
    own = c.get(f"/review/{mine}/act-1").get_data(as_text=True)
    assert beta.SAMPLE_FRAME in own
    assert "Confirmed. Your form is ready. " in own
    assert "send it to your new doctor" in own


def test_moms_finished_form_page_says_whose_it_is(cfg, svc, monkeypatch):
    hc = FakeClient()
    hc.fetch_review_page = lambda tenant, action_id, voice=None: (409, "")
    hc.action_status = lambda tenant, action_id: {
        "status": "completed", "kind": "form-fill",
        "outcome_summary": '{"delivery_link": "https://example.test/f.pdf"}'}
    c, _, hc, mine, mom = _with_mom(cfg, svc, monkeypatch, hc=hc)
    page = " ".join(c.get(f"/review/{mom['agent_id']}/act-1").get_data(
        as_text=True).split())
    assert "<h1>The form is ready</h1>" in page
    assert f"From {NICK}&#39;s records. Save it or print it." in page
    assert "Open the form (PDF)" in page
    assert "your new doctor" not in page
    own = " ".join(c.get(f"/review/{mine}/act-1").get_data(
        as_text=True).split())
    assert "<h1>Your form is ready</h1>" in own
    assert "send it to your new doctor" in own


def test_the_hubs_recent_line_says_whose_form_it_is(cfg, svc, monkeypatch):
    hc = FakeClient()
    hc.recent_actions = lambda tenant: [
        {"id": "act-9", "kind": "form-fill", "status": "completed",
         "updated_at": "2026-10-13T12:00:00Z"}]
    c, _, hc, mine, mom = _with_mom(cfg, svc, monkeypatch, hc=hc)
    recent = c.get("/api/approvals/count").get_json()["recent"]
    whose = {r["chat"].split("agent=")[1]: r.get("whose", "")
             for r in recent}
    assert whose[mom["agent_id"]] == f"{NICK}'s records"
    assert whose[mine] == ""          # as the pending queues name it
    line = _HOME_JS.split("const recentLine = (r) => {")[1][:300]
    assert '(r.whose ? " (" + r.whose + ")" : "")' in line


# --- #952 patient test: polish ---------------------------------------------

def test_deleting_moms_records_says_whose_and_that_she_left_the_hub(
        cfg, svc, monkeypatch):
    hc = FakeClient()
    hc.purge_tenant = lambda tenant: {"deleted": True, "rows_deleted": 3}
    c, _, hc, _, mom = _with_mom(cfg, svc, monkeypatch, hc=hc)
    svc.mark_synced(mom["connection_id"], 21)
    msg = c.delete(f"/api/connections/{mom['connection_id']}",
                   json={"confirm": "DELETE"}).get_json()["message"]
    assert msg.startswith(f"All 21 records in Sample records ({NICK}'s "
                          "records) were deleted.")
    assert f"{NICK} was removed from your hub too." in msg


def test_settings_names_each_assistant_and_says_for_you(
        cfg, svc, monkeypatch):
    c, _, _, _, _ = _with_mom(cfg, svc, monkeypatch)
    page = " ".join(c.get("/settings").get_data(as_text=True).split())
    assert "your assistant pays attention" not in page
    assert "These change what Rowan pays attention to" in page
    assert "These change what Juniper pays attention to" in page
    juniper = page[page.index("Juniper"):page.index("Rowan")]
    assert "For you" in juniper


def test_each_source_menu_names_whose_records(cfg, svc, monkeypatch):
    c, _, _, _, _ = _with_mom(cfg, svc, monkeypatch)
    page = c.get("/home").get_data(as_text=True)
    assert ('aria-label="More for Sample records (Your records)"' in page)
    assert (f'aria-label="More for Sample records ({NICK}&#39;s records)"'
            in page)
