"""The approval page must never report an unknown outcome as a rejection.

#220 and #416 established the three-answer contract for a clinical approval:

    confirmed True   the engine confirmed it
    confirmed False  nothing was sent; retrying is safe
    confirmed None   the engine never answered; it MAY have run

`careagents/app.py` gets this right and answers 502 with `confirmed: null`
and a `message` explaining the uncertainty.

The page then threw it away. Its submit handler read:

    gateMsg.textContent = res.b.error || 'Submission rejected.';

The 502 body carries `message`, not `error`. So the one outcome the backend
took two issues to model honestly rendered to the patient as the words
"Submission rejected." — a definite failure, for an approval that may
already have executed. Told their approval was rejected, a reasonable person
approves again. That is the double-send #416 exists to prevent, reintroduced
in the last four lines of the journey.

#419 filed the other half: the message told the patient to "check the
request's status first", and nothing in CareAgents let them do that. The
status endpoint existed with no caller.

These tests cover both, and the third defect found alongside them: the shell
never defined `.alert-success`, so the happy path rendered unstyled.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "templates/action_review.html"
SHELL = ROOT / "templates/review_base.html"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def _submit_handler(page: str) -> str:
    """The body of the fetch().then() that renders the submit response."""
    start = page.index("form.addEventListener('submit'")
    return page[start:page.index("evaluate();", start)]


def _code_only(js: str) -> str:
    """Strip `//` comments.

    The guards below look for strings the page must not PRINT. A comment
    quoting the old wording to explain why it was removed is documentation,
    not a defect, and the first version of this file went red on its own
    explanation. Scan what runs.
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.splitlines())


def test_there_is_a_submit_handler_to_examine(page):
    """A pass over a handler that no longer exists is not a pass."""
    body = _submit_handler(page)
    assert "fetch(" in body and "gateMsg" in body, "the submit handler moved"


# --- the severe one: unknown must not render as rejected -------------------

def test_the_failure_branch_renders_the_message_the_server_sent(page):
    """MUTATION: drop `res.b.message` from the fallback chain -> red.

    The 502 body sets `message`. Reading only `error` fell through to the
    literal string below.
    """
    body = _code_only(_submit_handler(page))
    # Presence anywhere is not enough: `res.b.message` also appears in the
    # confirmed-null branch, so the first version of this guard stayed green
    # when the failure branch was reverted to reading `error` alone. Pin the
    # ORDER of the fallback chain instead.
    assert re.search(r"res\.b\.message\s*\|\|\s*res\.b\.error", body), (
        "the failure branch does not fall back through `message` before "
        "`error`; `message` is the field the server uses when it has "
        "something specific to say")


def test_no_branch_can_call_an_unknown_outcome_a_rejection(page):
    """MUTATION: restore `'Submission rejected.'` as an unguarded fallback -> red.

    A fallback string is fine. A fallback string that asserts an OUTCOME is
    not, because it fires exactly when the server declined to assert one.
    """
    body = _code_only(_submit_handler(page))
    for claim in ("Submission rejected", "was rejected", "did not go through",
                  "nothing was sent"):
        assert claim not in body, (
            f"the page can print {claim!r} without knowing it. On a 502 the "
            f"server said `confirmed: null` on purpose; the page must not "
            f"convert that into a definite failure.")


def test_the_unconfirmed_case_is_handled_separately_from_a_plain_error(page):
    """null is a third answer, not a flavour of false.

    MUTATION: delete the `confirmed === null` branch -> red.
    """
    body = _code_only(_submit_handler(page))
    assert re.search(r"if\s*\(\s*res\.b\.confirmed\s*===\s*null\s*\)", body), (
        "no branch distinguishes `confirmed: null` from an ordinary failure, "
        "so the three-answer contract collapses back to two")


# --- #419: every instruction must be one the patient can carry out ---------

def test_the_page_can_actually_check_the_status_it_talks_about(page):
    """The filed defect. The copy said "check the request's status first"
    and `form_status` had no caller anywhere in CareAgents.

    MUTATION: remove the status fetch -> red.
    """
    body = _code_only(_submit_handler(page))
    assert "/api/form/" in body, (
        "nothing on this page calls the status endpoint, so an instruction "
        "to check the status has no destination (#419)")


def test_the_status_call_passes_the_agent_the_endpoint_requires(page):
    """`form_status` 404s without ?agent=. A call that always 404s is not a
    status surface, it is a status-shaped hole.

    MUTATION: drop the agent query parameter -> red.
    """
    body = _code_only(_submit_handler(page))
    call = body[body.index("/api/form/"):]
    assert "agent=" in call[:400], (
        "the status call omits ?agent=, which form_status requires; it would "
        "404 every time and look like 'no status available'")


def test_the_page_does_not_instruct_an_action_with_no_destination(page):
    """The general form of #419, as a property of the whole page.

    Any imperative pointing the patient somewhere must correspond to
    something on the page. This is deliberately narrow: it checks the
    specific instruction that shipped, not English generally.

    MUTATION: reinstate the copy without adding the status call -> red.
    """
    body = _submit_handler(page)
    instructs_lookup = "check the request's status" in body.lower()
    if instructs_lookup:
        assert "/api/form/" in body, (
            "the page tells the patient to check the status and provides no "
            "way to do it")


# --- the third defect, found alongside -------------------------------------

def test_every_class_the_javascript_assigns_is_defined_by_the_shell(page, shell):
    """The class-coverage guard read the HTML and not the script.

    `gateMsg.className = 'alert alert-success'` is set from JS on the happy
    path, and `.alert-success` was never defined in the self-contained shell,
    so a successful approval rendered unstyled. The existing guard could not
    see it because the class name appears only inside a string literal.

    MUTATION: delete `.alert-success` from review_base.html -> red.
    """
    assigned = set()
    for m in re.finditer(r"""className\s*=\s*['"]([^'"]+)['"]""", page):
        assigned.update(m.group(1).split())
    # Ternaries assign from either arm: className = ok ? 'a b' : 'c d'
    for m in re.finditer(r"""className\s*=\s*[^;]*?\?\s*['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]""", page):
        assigned.update(m.group(1).split())
        assigned.update(m.group(2).split())

    assert assigned, "no JS-assigned classes found; the guard is looking at nothing"

    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)\s*(?:,|\{)", shell))
    missing = sorted(c for c in assigned if c not in defined)
    assert not missing, (
        f"the script assigns classes the shell never defines: {missing}. "
        f"The relayed page loads no other stylesheet, so these render as "
        f"nothing at all.")
