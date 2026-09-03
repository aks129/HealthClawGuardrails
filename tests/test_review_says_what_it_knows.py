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


# --- #528 follow-on: the answers the page could receive but never rendered --
#
# PR #550 sealed the payload once a confirmation exists, and its QA follow-up
# made a resubmitted review answer 409 instead of raising the seal out of the
# handler as a Werkzeug HTML 500. That fixed the server half. The page still
# had two holes on the client half:
#
#   - `r.json()` had no `.catch`, so ANY non-JSON body — the HTML 500 that
#     started this, a gateway's own 502 page — rejected the promise and the
#     whole chain went silent. Nothing rendered and the button stayed
#     enabled, which is the invitation to double-tap that produced the error.
#     The 409 closed one cause of an unreadable body; it did not close the
#     branch.
#   - the 409 itself fell into the generic failure arm and rendered red, with
#     the button still enabled. A red failure for a request that WAS approved
#     is the #416 shape one status code over: told it failed, a reasonable
#     person approves again.
#
# The same template is what a CareAgents patient sees — careagents/app.py
# review() proxies it and rewrites only the submit URL, and review_submit()
# relays a 409 verbatim because _answered_about_data(409) is true. So these
# guard both surfaces.


def _block(js: str, anchor: str) -> str:
    """The brace-matched `{...}` block following `anchor`.

    Run this over COMMENT-STRIPPED code: a `{` inside the paragraph
    explaining a branch would otherwise unbalance the count. No string
    literal in this handler contains a brace.
    """
    start = js.index(anchor)
    open_at = js.index("{", start)
    depth = 0
    for i in range(open_at, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[open_at:i + 1]
    raise AssertionError(f"unbalanced block after {anchor!r}")


def test_the_json_parse_has_a_catch(page):
    """MUTATION: delete the `.catch` after `r.json()` -> red.

    Scoped to the parse itself. `checkStatus` has its own `.catch` further
    down the same handler, and a guard that searched the whole body stayed
    green with this one deleted.
    """
    body = _code_only(_submit_handler(page))
    parse = body[body.index("r.json()"):body.index(".then(function (res)")]
    assert ".catch(" in parse, (
        "the response parse has no .catch, so a body that is not JSON "
        "rejects the promise and nothing renders at all — the button stays "
        "enabled on a request that may already have run")


def test_an_unreadable_body_leaves_the_button_disabled(page):
    """MUTATION: drop `btn.disabled = true` from the unreadable branch -> red.

    We could not read the answer, so we do not know whether the approval ran.
    Re-arming Approve there is exactly the double-send #416 exists to prevent.
    """
    block = _block(_code_only(_submit_handler(page)), "!res.readable")
    assert re.search(r"btn\.disabled\s*=\s*true", block), (
        "the unreadable-body branch leaves Approve enabled on an outcome "
        "nobody observed")


def test_a_409_is_handled_as_its_own_answer(page):
    """MUTATION: delete the `res.r.status === 409` branch -> red."""
    body = _code_only(_submit_handler(page))
    assert re.search(r"res\.r\.status\s*===\s*409", body), (
        "no branch distinguishes 409 — already approved — from an ordinary "
        "failure, so a successful approval renders to the patient as one")


def test_the_409_branch_disables_the_button_and_checks_the_status(page):
    """The two things the 409 branch exists to do.

    MUTATION: remove either `btn.disabled = true` or `checkStatus()` -> red.
    Leaving Approve enabled invites the second tap that produced the 409;
    without the status call the page states a fact and then abandons it,
    which is the #419 shape.
    """
    block = _block(_code_only(_submit_handler(page)), "res.r.status === 409")
    assert re.search(r"btn\.disabled\s*=\s*true", block), (
        "the 409 branch leaves Approve enabled, which invites the double-tap "
        "that produced the 409")
    assert "checkStatus()" in block, (
        "the 409 branch never resolves where the request actually stands")


def test_the_409_branch_does_not_render_as_a_failure(page):
    """MUTATION: set `alert alert-danger` in the 409 branch -> red.

    The action carries a confirmation. Painting that red is a claim the page
    did not observe, and the person's reasonable response to it is to
    approve again.
    """
    block = _block(_code_only(_submit_handler(page)), "res.r.status === 409")
    assert "alert-danger" not in block, (
        "a 409 means the request was already approved; rendering it as an "
        "error tells the patient their approval failed when it did not")


def test_the_409_branch_runs_before_the_generic_failure_message(page):
    """MUTATION: move the 409 branch below the fallback -> red.

    The fallback assigns `alert alert-danger` and returns nothing, so a 409
    check placed after it is dead code that still reads as handled.
    """
    body = _code_only(_submit_handler(page))
    assert body.index("res.r.status === 409") < body.index("alert alert-danger"), (
        "the 409 branch sits below the generic failure arm and can never run")


# --- the outer fetch rejection ---------------------------------------------
#
# The parse `.catch` above covers a body that will not decode. A rejection of
# the fetch ITSELF — the connection dropped, the service unreachable — never
# produces a response to parse, and that path was silent too: nothing
# rendered and Approve stayed enabled with no explanation.
#
# RULED (coordinator, this PR): this branch alone leaves Approve ENABLED. A
# dropped connection most often means the request never left the device, and
# a disabled button with no way forward strands a patient mid-approval. Name
# the uncertainty, point at the check, let them act. The option ruled out is
# the silent failure it used to be.


def test_the_fetch_itself_has_a_catch(page):
    """MUTATION: delete the `.catch` between fetch() and the parse -> red.

    Scoped to the segment between the fetch call and the parse `then`, so the
    parse's own catch and checkStatus's cannot stand in for a missing one.
    """
    body = _code_only(_submit_handler(page))
    outer = body[body.index("fetch("):body.index(".then(function (r)")]
    assert ".catch(" in outer, (
        "a rejected fetch — a dropped connection — is unhandled, so the "
        "chain goes silent and Approve stays enabled with nothing said")


def test_an_unreachable_service_leaves_the_button_enabled(page):
    """The ruling, pinned. MUTATION: set `btn.disabled = true` here -> red.

    Deliberately the opposite of every sibling branch, which is exactly why
    it needs a guard: the next person to tidy these into one shape would
    disable it for consistency and strand the patient.
    """
    block = _block(_code_only(_submit_handler(page)), "!res.reached")
    assert "btn.disabled = false" in block, (
        "the unreachable-service branch does not explicitly leave Approve "
        "enabled")
    assert "btn.disabled = true" not in block, (
        "the unreachable-service branch disables Approve. A dropped "
        "connection usually means the request never left the device; "
        "disabling the only way forward strands the patient mid-approval")
    # Two reasons, and the second is the one that bites quietly.
    # checkStatus's terminal message says "Please do not approve a second
    # time", which would countermand the enabled button above. And its
    # awaiting_confirmation arm says "Your approval is recorded" — true for
    # every OTHER caller, false here, where nothing is known about whether
    # the request even left the device.
    assert "checkStatus()" not in block, (
        "the unreachable-service branch polls the status. Its terminal copy "
        "contradicts the enabled button, and its awaiting_confirmation arm "
        "would claim an approval this branch never established")


def test_the_unreachable_branch_claims_nothing_about_the_request(page):
    """It reached nothing, so it knows nothing — including whether the
    request arrived.

    The global forbidden-claims guard above already scans this branch. This
    pins the positive half: it must say we could not reach the service, not
    invent an outcome for the approval.
    """
    block = _block(_code_only(_submit_handler(page)), "!res.reached")
    assert "could not reach" in block, (
        "the unreachable-service branch does not say what actually happened")


# --- the terminus: a recorded approval is not an unknown outcome -----------
#
# The loop this PR closes ended here. The 409 branch and the relay's
# confirm-failure both point the patient at the status; checkStatus handled
# completed/sent/pending/approved and nothing else, so `awaiting_confirmation`
# — exactly where the action sits in both cases — fell to the terminal "we
# still cannot tell whether your approval went through". That is weaker than
# what is on file: the approval IS recorded.


def test_awaiting_confirmation_is_not_reported_as_an_unknown_outcome(page):
    """MUTATION: delete the `awaiting_confirmation` arm -> red.

    Without it the status the action actually holds falls through to the
    terminal unknown, and the instruction to go and check the status returns
    less than the branch that issued it already knew.
    """
    body = _code_only(_submit_handler(page))
    assert "awaiting_confirmation" in body, (
        "checkStatus does not handle awaiting_confirmation, which is the "
        "status the action holds after a review is submitted; it reports a "
        "recorded approval as an outcome nobody can determine")


def test_the_awaiting_confirmation_arm_never_invites_a_second_approval(page):
    """MUTATION: drop the "will not resend" clause -> red.

    The state is reachable precisely when a patient has been sent to check on
    an approval they already gave. Saying it is not finished, without saying
    that re-approving cannot help, is what produced the double-tap.
    """
    block = _block(_code_only(_submit_handler(page)), "'awaiting_confirmation'")
    assert "will not resend" in block, (
        "the awaiting_confirmation arm says the request is unfinished but "
        "not that approving again cannot resend it")
    assert "recorded" in block, (
        "the awaiting_confirmation arm does not say the approval is on file, "
        "which is the whole reason it is not an unknown outcome")


def test_the_relay_confirm_failure_gets_the_destination_it_points_at(page):
    """The other half of the loop.

    The relay's 502 tells the patient to check where the request stands. The
    page rendered that in the generic failure arm, which polls nothing and
    leaves Approve armed for a retry that now answers 409.

    MUTATION: remove the `status === 502` branch, or its `checkStatus()`,
    or its `btn.disabled` -> red.
    """
    body = _code_only(_submit_handler(page))
    assert re.search(r"res\.r\.status\s*===\s*502", body), (
        "no branch handles the relay's confirm-failure, so its instruction "
        "to check the status has nothing behind it")
    block = _block(body, "res.r.status === 502")
    assert "checkStatus()" in block, (
        "the confirm-failure branch points at the status and never reads it")
    assert re.search(r"btn\.disabled\s*=\s*true", block), (
        "the confirm-failure branch leaves Approve armed for a retry that "
        "answers 409")


def test_the_confirm_failure_branch_is_not_keyed_on_confirmed_alone(page):
    """`confirmed: false` is also sent by two 503s, for a review that never
    reached the engine. Polling the status there reports on a request that
    was never made.

    MUTATION: drop the status test and key on `confirmed === false` alone
    -> red.
    """
    body = _code_only(_submit_handler(page))
    assert re.search(
        r"res\.r\.status\s*===\s*502\s*&&\s*res\.b\.confirmed\s*===\s*false",
        body), (
        "the confirm-failure branch does not test BOTH the 502 and "
        "`confirmed === false`, so it cannot distinguish the answer that has "
        "an approval on file from the 503s that do not")


# --- QA #566: the precondition that is a comment, and the vocabulary gap ----
#
# The `awaiting_confirmation` arm says "Your approval is recorded". The status
# alone does not carry that: `awaiting_confirmation` is set at COMMIT
# (r6/actions/routes.py:415), before any human approval. The sentence is true
# only because of WHO calls `checkStatus` — a 200 review mints a confirmation,
# and the 409 fires only when one is present.
#
# That precondition was enforced by a paragraph. docs/2026-08-02-retro.md is
# about controls that look like one thing and quietly do another; a
# load-bearing invariant whose only enforcement is a comment is the same
# category. One `checkStatus()` added to the unreachable branch — the tidy-up
# a reviewer would call consistency, and which the author had to write a
# separate test to forbid — makes this page tell a patient their approval is
# recorded when nothing left the device.


def _checkstatus_call_sites(body: str) -> list[int]:
    """Offsets of every CALL to checkStatus, excluding its definition."""
    return [m.start() for m in re.finditer(r"checkStatus\(\)", body)
            if not body[:m.start()].rstrip().endswith("function")]


def test_check_status_is_only_called_where_an_approval_is_established(page):
    """The comment's invariant, made a check.

    MUTATION: add `checkStatus();` to the `!res.reached` branch -> red.

    Every caller must sit inside one of the three answers that establish a
    confirmation EXISTS: the 409 (fires only when one is present), the
    relay's 502 confirm-failure, and the 502 `confirmed: null`. All three
    follow a review the engine answered 200, which is what mints it.
    """
    body = _code_only(_submit_handler(page))
    anchors = ("res.r.status === 409",
               "res.b.confirmed === null",
               "res.r.status === 502 &&")
    allowed = []
    for anchor in anchors:
        block = _block(body, anchor)
        allowed.append((body.index(block, body.index(anchor)), len(block)))

    sites = _checkstatus_call_sites(body)
    assert sites, "no call to checkStatus at all; this guard reads nothing"

    for site in sites:
        assert any(off <= site < off + length for off, length in allowed), (
            "checkStatus is called from a branch that has NOT established a "
            "confirmation. Its awaiting_confirmation arm says 'Your approval "
            "is recorded' — a false statement about the human gate whenever "
            "the caller has not approved yet, because that status is set at "
            "commit, before any approval exists.")


def test_the_forbidden_claims_guard_is_not_defeated_by_capitalisation(page):
    """The claims the page must never print, matched case-INSENSITIVELY.

    The sibling guard compares with `in` against the raw source, so "Nothing
    was sent" at the start of a sentence walks straight past a list written
    in lower case — and every one of these reads most naturally capitalised,
    which is exactly how the copy it was written against did:
    "Nothing has been sent — please try approving again."

    MUTATION: set any branch's text to 'Nothing was sent.' -> red here,
    green on the original guard.
    """
    body = _code_only(_submit_handler(page)).lower()
    for claim in ("submission rejected", "was rejected", "did not go through",
                  "nothing was sent"):
        assert claim not in body, (
            f"the page can print {claim!r} without knowing it")


@pytest.mark.xfail(strict=True, reason=(
    "QA #566: checkStatus handles 'sent', 'pending' and 'approved', none of "
    "which _TRANSITIONS can produce, and handles none of 'executing', "
    "'failed', 'needs_review', 'expired' or 'unknown', all of which it can. "
    "A form-fill confirm reaches 'failed' on a deployment with no provider "
    "configured — verified against a running engine — so the #220 third "
    "answer, the case checkStatus exists for, terminates at 'we still cannot "
    "tell whether your approval went through' while the status says it ran. "
    "Fixing this reddens the pin: update it in the same PR."))
def test_the_poll_reads_statuses_the_state_machine_can_actually_produce(page):
    """The poll's vocabulary against the state machine's, both read from source.

    Reported per-status so a failure names the missing arm rather than just
    saying two sets differ.
    """
    table = re.search(r"_TRANSITIONS = \{(.*?)\n\}",
                      (ROOT / "r6/actions/models.py").read_text(), re.S)
    # 'proposed' is unreachable from this page: the review route 404s unless
    # the action is already awaiting_confirmation.
    real = set(re.findall(r"'([a-z_]+)'", table.group(1))) - {"proposed"}

    body = _code_only(_submit_handler(page))
    handled = set(re.findall(r"status === '([a-z_]+)'", body))

    impossible = sorted(handled - real)
    unhandled = sorted(real - handled)
    assert not impossible, (
        f"checkStatus branches on statuses this system cannot produce: "
        f"{impossible}. Dead arms read as coverage.")
    assert not unhandled, (
        f"checkStatus has no arm for {unhandled}, so each falls through to "
        f"the terminal 'we still cannot tell whether your approval went "
        f"through' — an unknown outcome reported for a status that is known.")
