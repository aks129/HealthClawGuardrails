"""What the PR-review workflow is allowed to mail, and what it is not.

Every comment a workflow posts is an email. `auto-merge-when-satisfied` runs
on `synchronize`, so it ran on every push, and until this file existed it
commented on every one of those runs: "standards review passed, auto-merge is
staged". Measured on 2026-09-04 over the 25 most recent pull requests — 65 of
those comments, on 23 of the 25, six on one and two four minutes apart on
another. None of them named anything the maintainer could act on.

The rule the workflow now follows: a comment is for a state the maintainer
must know about or act on; every other state goes to the job summary, which
notifies nobody. And a state that has not changed is not news, so it is not
sent twice.

The behavioural tests here run the workflow's real shell block — extracted
from the YAML, not paraphrased — against a stub `gh` that records calls and
lets a posted comment be read back by the next run. A sequence of runs is
therefore a real sequence, which is the only way to ask the question that
matters: does pushing five times send five mails?
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "claude-pr-review.yml"
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)

# YAML 1.1 reads a bare `on:` as the boolean True, which is why this is not
# WORKFLOW["on"].
TRIGGERS = WORKFLOW[True]


def _arming_step() -> str:
    """The shell block of the auto-merge job, verbatim."""
    steps = WORKFLOW["jobs"]["auto-merge"]["steps"]
    blocks = [s["run"] for s in steps if "run" in s]
    assert len(blocks) == 1, f"expected one shell step, found {len(blocks)}"
    return blocks[0]


def _arming_code() -> str:
    """The same block with its comments stripped — the part that executes.

    Written after `test_a_failed_comment_is_not_swallowed` went red on the
    comment explaining why there is no `|| true`. A guard that cannot tell
    code from prose about the code reports on the wrong thing.
    """
    return "\n".join(line for line in _arming_step().splitlines()
                      if not line.lstrip().startswith("#"))


# A stand-in for `gh`. It logs the verb of every call, answers the three reads
# the step makes, and — the part that makes a sequence of runs real — appends a
# posted comment to the same store the next run reads its previous state from.
GH_STUB = r"""#!/bin/bash
echo "$1 $2" >> "$GH_LOG"
case "$1 $2" in
  "pr view")
    case "$*" in
      *"--json title"*)    printf '%s\n' "$FAKE_TITLE" ;;
      *"--json reviews"*)  printf '%s\n' "${FAKE_APPROVED:-0}" ;;
      *"--json comments"*) cat "$FAKE_COMMENTS" ;;
    esac ;;
  "pr comment")
    for a in "$@"; do last="$a"; done
    printf '%s\n' "$last" >> "$FAKE_COMMENTS" ;;
esac
exit 0
"""


class _PullRequest:
    """One pull request, across as many workflow runs as a test wants."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.comments = tmp_path / "comments.txt"
        self.log = tmp_path / "gh.log"
        self.summary = tmp_path / "summary.md"
        for f in (self.comments, self.log, self.summary):
            f.write_text("")

    def push(self, *, author: str = "contributor", approved: int = 0,
             title: str = "") -> subprocess.CompletedProcess:
        """One run of the workflow, as a push to this PR would trigger it."""
        before = len(self.calls())
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.dir),
            "GH_TOKEN": "stub",
            "PR": "7",
            "REPO": "owner/repo",
            "AUTHOR": author,
            # Read from the workflow, not hard-coded: flipping it there is a
            # policy change this test should follow rather than mask.
            "AUTO_MERGE_EXTERNAL": WORKFLOW["env"]["AUTO_MERGE_EXTERNAL"],
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GH_LOG": str(self.log),
            "FAKE_COMMENTS": str(self.comments),
            "FAKE_APPROVED": str(approved),
            "FAKE_TITLE": title,
        }
        proc = subprocess.run(["bash", "-c", _arming_step()], env=env,
                              capture_output=True, text=True, cwd=self.dir)
        assert proc.returncode == 0, proc.stderr
        # Catalogue §0: a harness that drove nothing must not read as a pass.
        # Every path makes at least one `gh pr view`, so no new calls means
        # the block exited before doing anything and every count below would
        # be zero for the wrong reason.
        assert len(self.calls()) > before, (
            f"the step made no gh call at all:\n{proc.stdout}{proc.stderr}")
        return proc

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln.strip()]

    def count(self, verb: str) -> int:
        return self.calls().count(verb)

    def comment_text(self) -> str:
        return self.comments.read_text()


@pytest.fixture()
def pr(tmp_path: Path) -> _PullRequest:
    return _PullRequest(tmp_path)


# --- the block has to be runnable at all ------------------------------------

def test_the_arming_step_is_plain_shell_so_this_file_runs_the_real_thing():
    """MUTATION: put an Actions expression in the block -> red.

    The tests below execute this block outside Actions. One `${{ ... }}` in it
    and they would be running something that cannot be substituted — and,
    worse, an empty expression is a workflow syntax error, so the file would
    fail to parse in production while every test here stayed green.
    """
    step = _arming_step()
    assert "${{" not in step, "the block is no longer executable outside Actions"
    assert "gh pr merge" in step and "gh pr comment" in step, (
        "the extracted block is not the arming step")


# --- what earns a mail ------------------------------------------------------

def test_a_pull_request_merely_waiting_for_the_maintainer_is_never_mailed(pr):
    """The offender. Five pushes, no approval, and nothing to act on.

    MUTATION: restore the `gh pr comment` in the waiting branch -> red.
    """
    for _ in range(5):
        pr.push()
    assert pr.count("pr comment") == 0
    assert pr.count("pr merge") == 0
    assert "waiting-approval" in pr.summary.read_text(), (
        "a state nobody is mailed about must still be recorded somewhere")


def test_an_armed_auto_merge_is_mailed_once_however_many_pushes(pr):
    """A pending merge is worth knowing about. It is worth knowing once.

    MUTATION: delete the PREV check -> red at the second push.
    """
    for _ in range(5):
        pr.push(approved=1)
    assert pr.count("pr merge") == 5, (
        "arming is idempotent and must keep running on every push — it is the "
        "notification that is deduplicated, not the arming")
    assert pr.count("pr comment") == 1


def test_the_mail_goes_at_the_state_change_and_not_before(pr):
    """Two silent pushes, then an approval, then silence again."""
    pr.push()
    pr.push()
    assert pr.count("pr comment") == 0
    pr.push(approved=1)
    assert pr.count("pr comment") == 1, "arming is the news"
    pr.push(approved=1)
    assert pr.count("pr comment") == 1, "and it is only news once"


@pytest.mark.parametrize("title,state,merges", [
    ("chore(deps): bump actions/checkout from 4.1.1 to 4.2.0",
     "armed-dependabot", 3),
    ("chore(deps): bump actions/checkout from 4.1.1 to 5.0.0",
     "held-dependabot-major", 0),
    # Half of this repo's dependabot pull requests look like this one: a
    # grouped or multi-package bump with no single from-X-to-Y in the title.
    ("chore(deps-dev): bump jest and @types/jest in /services/agent-orchestrator",
     "held-unparsed-bump", 0),
])
def test_each_dependabot_outcome_is_stated_once_and_only_once(pr, title, state,
                                                              merges):
    for _ in range(3):
        pr.push(author="dependabot[bot]", title=title)
    assert pr.count("pr merge") == merges
    assert pr.count("pr comment") == 1
    # The marker is what the dedupe reads back. A comment posted without one
    # would notify every run and nothing else here would notice.
    assert pr.comment_text().count(f"automerge-state: {state}") == 1


def test_a_held_bump_and_an_armed_one_are_different_news(pr):
    """The dedupe key is the state, not "have we ever commented".

    MUTATION: collapse the three dependabot states into one -> red.
    """
    pr.push(author="dependabot[bot]",
            title="chore(deps): bump x from 1.0.0 to 2.0.0")
    pr.push(author="dependabot[bot]",
            title="chore(deps): bump x from 1.0.0 to 1.1.0")
    assert pr.count("pr comment") == 2
    assert "automerge-state: held-dependabot-major" in pr.comment_text()
    assert "automerge-state: armed-dependabot" in pr.comment_text()


def test_every_run_records_its_state_in_the_job_summary(pr):
    """The summary is where the quiet states go, so it must always be written."""
    pr.push()
    pr.push(approved=1)
    summary = pr.summary.read_text()
    assert "### auto-merge: waiting-approval" in summary
    assert "### auto-merge: armed-approved" in summary


# --- what the wording is allowed to claim -----------------------------------

def test_the_wording_matches_the_triggers_it_describes():
    """The removed comment said the PR merges "the moment a maintainer
    approves and CI is green". It does not: there is no trigger on reviews, so
    an approval on its own runs nothing at all — the merge arms on the next
    push, reopen or ready-for-review after an approval exists.

    The replacement says that instead, which makes it a claim about this
    trigger list. Add `pull_request_review` here and the sentence becomes
    wrong, so this test goes red and the wording has to move with it.

    MUTATION: add `pull_request_review:` to the triggers -> red.
    """
    assert "the moment a maintainer approves" not in WORKFLOW_TEXT
    assert "auto-merge is staged" not in WORKFLOW_TEXT

    assert set(TRIGGERS) == {"pull_request_target"}, (
        "a new trigger changes when auto-merge arms, which the waiting-state "
        "wording describes verbatim")
    assert TRIGGERS["pull_request_target"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review"]

    step = _arming_step()
    assert "has no trigger on reviews" in step
    assert "next push, reopen or ready-for-review" in step


def test_the_arming_step_does_not_claim_a_review_verdict_it_cannot_see():
    """Measured on 2026-09-04: with ANTHROPIC_API_KEY unset, both the review
    step and the verdict gate are skipped, the job succeeds, and auto-merge
    runs anyway. Three sampled pull requests carried the "Standards review
    passed (claude:approve)" comment and no `claude:approve` label, because no
    review had run. This job has no way to tell the two apart, so it is not
    allowed to speak for the reviewer at all.

    MUTATION: put "standards review passed" back in the headline -> red.
    """
    code = _arming_code().lower()
    assert "review passed" not in code
    assert "claude:approve" not in code


def test_a_failed_comment_is_not_swallowed():
    """Catalogue §5. A notification that did not post must fail the run, not
    return zero quietly.

    MUTATION: append `|| true` to the `gh pr comment` -> red.
    """
    assert "|| true" not in _arming_code()


def test_runs_on_one_pull_request_are_serialised():
    """The dedupe reads a comment the other run may not have posted yet.

    Two pushes four minutes apart is what was observed; two close enough to
    overlap is what breaks read-then-write with no concurrency group.

    MUTATION: delete the concurrency stanza -> red.
    """
    concurrency = WORKFLOW["concurrency"]
    assert "pull_request.number" in concurrency["group"], (
        "the group must be per pull request, or every PR queues behind every "
        "other one")
    assert concurrency["cancel-in-progress"] is False, (
        "cancelling mid-run would abandon the arming step half-done")
