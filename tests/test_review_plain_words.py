"""The engine's review page, read by a non-technical person on a phone
(#884 patient tester, G7). Synthetic data only.

Its own file: these render the engine's template, so they use conftest's
engine `app`, which tests/test_careagents.py's fixture of the same name
would shadow.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from flask import render_template

ROOT = pathlib.Path(__file__).resolve().parents[1]
HANDLERS = (ROOT / "templates" / "_review_handlers.html").read_text()


def _render(app, **ctx):
    base = {"action_id": "act-1", "demographics": [], "meds": [],
            "allergies": [], "conditions": [], "record_readable": True,
            "record_reason": ""}
    base.update(ctx)
    with app.test_request_context():
        return render_template("action_review.html", **base)


def _gate(html):
    m = re.search(r'id="review-gate-msg"[^>]*>(.*?)</div>', html, re.S)
    return " ".join(m.group(1).split())


@pytest.mark.parametrize("meds,allergies,says,never", [
    ([], [], 'Check "I have no known allergies"',
     ("confirm an allergy", "confirm one", "medication")),
    ([{"name": "Lisinopril"}], [], "Answer each medication.",
     ("confirm an allergy", "confirm one", "each allergy")),
    ([], [{"allergen": "Penicillin"}], "Answer each allergy.",
     ("medication",)),
])
def test_the_gate_names_only_what_the_page_lists(app, meds, allergies, says,
                                                 never):
    gate = _gate(_render(app, meds=meds, allergies=allergies))
    assert says in gate
    for word in never:
        assert word not in gate, gate


def test_the_ready_line_and_the_button_are_plain(app):
    html = _render(app)
    assert "Ready. We'll check each item again when you approve." in html
    assert re.search(r'id="approve-btn"[^>]*>\s*Approve\s*</button>', html)
    assert "Approve &amp; generate" not in html
    assert "'Ready to generate" not in html     # a comment may still name it


def test_about_you_reads_like_a_person_wrote_it():
    from r6.actions.review import _demographics
    qr = {"item": [{"linkId": "demographics", "item": [
        {"linkId": "demographics.birth-date",
         "answer": [{"valueDate": "1985-03-15"}]},
        {"linkId": "demographics.gender",
         "answer": [{"valueCoding": {"code": "female"}}]},
        {"linkId": "demographics.address-city",
         "answer": [{"valueString": "Springfield"}]}]}]}
    assert _demographics(qr) == [("Date of birth", "Mar 15, 1985"),
                                 ("Gender", "Female"),
                                 ("City", "Springfield")]


def test_decline_goes_with_an_approval_that_may_have_gone_through():
    """A failed approval left Decline live beside a locked Approve: a second,
    contrary answer to a request that may have run."""
    after = HANDLERS[HANDLERS.index("'We could not record your review. "
                                    "Please try again.';"):]
    after = after[:after.index("});\n    });")]
    assert "if (btn.disabled || btn.hidden)" in after
    assert "document.getElementById('decline-btn').disabled = true;" in after


def test_an_answered_review_offers_the_way_back_to_chat():
    fn = HANDLERS[HANDLERS.index("function showBackToChat()"):]
    fn = fn[:fn.index("\n    }\n")]
    assert "'/chat?agent=' + encodeURIComponent(parts[2])" in fn
    assert "'Back to chat'" in fn
    # Only on the relay, whose form action names the assistant.
    assert "parts[1] !== 'review'" in fn
    decline = HANDLERS[HANDLERS.index("res.b.declined === true"):]
    assert decline.index("showBackToChat();") < decline.index("return;")
    renderer_tail = HANDLERS[HANDLERS.index("if (btn.disabled || btn.hidden)"):]
    assert "showBackToChat();" in renderer_tail[:200]
