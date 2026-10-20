"""The Haiku tier's how-to card (#935).

Haiku answered "thanks so much!" in a new conversation with "I'm glad the
sample records were useful" (context it never had), and "how do i delete my
account" with "I have no tool for that" and a guess. It now gets a short,
fixed card of the app's real how-to paths, ahead of the system prompt. Each
path on the card is pinned here to the template that actually shows it, so
a renamed button turns this red instead of the card going stale.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from careagents import llm, routing
from careagents.config import Config
from tests.test_careagents_model_routing import TOOLS, _cfg, _fake, _resp, _Text

TEMPLATES = Path(__file__).resolve().parents[1] / "careagents" / "templates"

# (what the card must say, template file, the same words in that template)
PATHS = [
    # Delete the account: hub -> Settings -> "Delete my account", DELETE.
    ("Settings", "home.html", 'href="/settings">Settings</a>'),
    ("Delete my account", "settings.html", "Delete my account</button>"),
    ("Sign out or leave", "settings.html", "<h2>Sign out or leave</h2>"),
    ("type the word delete", "_delete_modal.html",
     "Type the word delete to confirm"),
    # Passkeys: add from Settings, confirmed by an emailed code.
    ("Add a passkey", "settings.html", 'href="/auth?enroll=1">Add a passkey'),
    ("code we email", "auth.html",
     "To add a passkey, confirm it's you with a code we email."),
    # Own records: the hub's card, or a code sign-in first after a link.
    ("Connect your records", "home.html",
     '<div class="hub-card-name">Connect your records</div>'),
    ("Sign in again to connect your records", "home.html",
     "Sign in again to connect your records</a>"),
    # Invites.
    ("Invite friends and family", "settings.html",
     "<h2>Invite friends and family</h2>"),
    ("Copy link", "settings.html", 'id="invite-copy">Copy link</button>'),
    # Texting.
    ("Where you can reach your assistant", "settings.html",
     "<h2>Where you can reach your assistant</h2>"),
    ("iMessage", "settings.html", "<b>iMessage</b>"),
    ("coming soon", "settings.html", "<span>coming soon</span>"),
    # Feedback, sign out, safety.
    ("Tell us", "chat.html", '<a href="/feedback">Tell us</a>'),
    ("Tell us", "home.html", '<a href="/feedback">Tell us</a>'),
    ("Sign out", "chat.html", '<button type="submit">Sign out</button>'),
    ("Sign out", "settings.html", '<button class="pill">Sign out</button>'),
    ("Home", "chat.html", '<a href="/home">Home</a>'),
    ("How we keep you safe", "chat.html",
     'href="/safety">How we keep you safe</a>'),
]


@pytest.mark.parametrize("card_text,template,needle", PATHS)
def test_every_path_on_the_card_is_the_real_one(card_text, template, needle):
    assert card_text in routing.HAIKU_CARD
    assert needle in (TEMPLATES / template).read_text()


def test_the_invite_cap_on_the_card_is_the_deployment_default():
    assert Config(env={}).invite_link_cap == 5
    assert "up to 5 people" in routing.HAIKU_CARD


def test_the_card_says_removing_a_passkey_is_not_in_the_app():
    # No route or button removes one passkey; the card must not invent one.
    assert "Removing a passkey isn't available" in routing.HAIKU_CARD
    page = (TEMPLATES / "settings.html").read_text()
    section = page.split('id="passkeys-section"', 1)[1].split("</section>")[0]
    assert "remove" not in section.lower() and "delete" not in section.lower()


def test_the_card_rules():
    card = routing.HAIKU_CARD
    assert "never refer to" in card.lower()
    assert "earlier" in card
    assert "Do not call tools" in card
    assert "neutral" in card
    assert routing.HAIKU_PASS in card


def test_the_card_stays_short():
    # It is sent ahead of every Haiku call.
    assert len(routing.HAIKU_CARD) < 3000


# --- the two messages from #935 -------------------------------------------------

@pytest.mark.parametrize("text", ["thanks so much!", "how do i delete my account"])
def test_the_issue_messages_reach_haiku_with_the_card_first(monkeypatch, text):
    """MUTATION: drop the card from the Haiku call -> red."""
    calls = _fake(monkeypatch, plain=[_resp([_Text("ok")])])
    llm.complete(_cfg(), "sys", [{"role": "user", "content": text}], TOOLS)
    [(_kind, kw)] = calls
    assert kw["model"] == routing.HAIKU_MODEL
    assert kw["system"].startswith(routing.HAIKU_CARD)
    assert kw["system"].endswith("sys")


def test_sonnet_never_gets_the_card(monkeypatch):
    calls = _fake(monkeypatch, beta=[_resp([_Text("Your A1c rose.")])])
    llm.complete(_cfg(), "sys",
                 [{"role": "user", "content": "How is my A1c?"}], TOOLS)
    [(_kind, kw)] = calls
    assert routing.HAIKU_CARD not in kw["system"]


@pytest.mark.parametrize("text", [
    "how do i remove a passkey",
    "how do I invite friends",
    "how can I invite my family",
    "how do you keep my data safe",
    "how do I sign out",
])
def test_the_new_card_questions_reach_haiku(text):
    assert routing.classify(text) == routing.TIER_HAIKU


@pytest.mark.parametrize("text", [
    "how do I invite friends to see my labs",
    "how do you keep me safe from my A1c",
    "how do you keep me safe",  # could be about the person's own safety
    "how do you keep me safe tonight",
    "how do I remove a passkey and check my potassium",
])
def test_the_new_templates_admit_nothing_more(text):
    assert routing.classify(text) == routing.TIER_SONNET


# --- a question the card does not cover ----------------------------------------

def test_haiku_passing_a_question_is_rerun_on_sonnet(monkeypatch):
    """MUTATION: show Haiku's pass line as the answer -> red."""
    calls = _fake(monkeypatch,
                  plain=[_resp([_Text(routing.HAIKU_PASS)])],
                  beta=[_resp([_Text("Here is how.")])])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "what is this"}], TOOLS)
    assert [k for k, _ in calls] == ["plain", "beta"]
    assert turn.text == "Here is how."
    assert turn.tier == routing.TIER_SONNET
    assert turn.escalated is True


def test_a_pass_line_with_a_curly_apostrophe_still_escalates(monkeypatch):
    calls = _fake(monkeypatch,
                  plain=[_resp([_Text(routing.HAIKU_PASS.replace("'", "’"))])],
                  beta=[_resp([_Text("Here is how.")])])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "what is this"}], TOOLS)
    assert [k for k, _ in calls] == ["plain", "beta"]
    assert turn.escalated is True


def test_an_ordinary_haiku_answer_is_kept(monkeypatch):
    calls = _fake(monkeypatch, plain=[_resp([_Text("You're welcome.")])])
    turn = llm.complete(_cfg(), "sys",
                        [{"role": "user", "content": "thanks"}], TOOLS)
    assert [k for k, _ in calls] == ["plain"]
    assert turn.text == "You're welcome."
    assert turn.escalated is False
