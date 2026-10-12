"""The tester terms make promises; these pin the code to them (#565, QA of
#904). Each test names the sentence it holds. If one fails, either the code
broke a promise or the promise changed: the second needs a new
TERMS_VERSION and CHANGE_SUMMARY, so every tester accepts it again."""

from __future__ import annotations

import re
from pathlib import Path

from careagents import beta, config, sendblue_surface, tester_terms

TERMS = (Path(__file__).resolve().parents[1] / "careagents" / "templates"
         / tester_terms.TEMPLATE).read_text()
TEXT = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", TERMS))

# "The AI comes from Anthropic, OpenAI, Google or Groq."
_HOST_NAMES = {
    "api.anthropic.com": "Anthropic",
    "api.openai.com": "OpenAI",
    "generativelanguage.googleapis.com": "Google",
    "api.groq.com": "Groq",
}


def test_the_named_model_providers_are_exactly_the_vetted_hosts():
    """A host added to the vetted set without naming it in the terms (or
    named in the terms and dropped from the set) fails here."""
    assert set(config._VETTED_MODEL_HOSTS) == set(_HOST_NAMES)
    for name in _HOST_NAMES.values():
        assert name in TEXT, name


def test_texting_with_real_records_points_to_the_app_by_default():
    """"Texting uses made-up records only." True for Sendblue only while
    SENDBLUE_REAL_RECORDS stays off; turning it on changes the promise."""
    cfg = config.Config(env={})
    assert cfg.sendblue_real_records is False
    for kind in ("fasten", "direct", "wearable", "anything-new"):
        assert sendblue_surface.real_records_blocked(
            cfg, {"connection": {"kind": kind}}) is True
    assert sendblue_surface.real_records_blocked(
        cfg, {"connection": {"kind": "sample"}}) is False
    assert "Texting uses made-up records only" in TEXT


def test_a_connection_consented_before_these_terms_must_accept_again():
    """"If they do, we will ask you to accept them again before your
    assistant uses your records." With the shipped version (no monkeypatch),
    a real connection consented at the pre-terms version is held, and one
    at the shipped version is not."""
    shipped = tester_terms.CONSENT_VERSION
    assert tester_terms.approved() and shipped == tester_terms.TERMS_VERSION
    old = {"kind": "fasten", "consent_version": tester_terms.BASE_VERSION}
    new = {"kind": "fasten", "consent_version": shipped}
    assert beta.turn_block(old, False, shipped) == beta.TERMS_TEXT
    assert beta.turn_block(new, False, shipped) is None


def test_the_tell_us_link_the_terms_name_is_on_the_hub_and_the_chat():
    """"Use the “Tell us” link in the app." The label the terms quote is a
    link to /feedback on both screens a connected tester uses (#905)."""
    assert "“Tell us” link in the app" in TEXT
    templates = Path(__file__).resolve().parents[1] / "careagents" / "templates"
    for page in ("home.html", "chat.html"):
        assert '<a href="/feedback">Tell us</a>' in (
            templates / page).read_text(), page
