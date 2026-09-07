"""The collision check answers the question two agents cannot ask each other.

Two agents work this repository in different tools, and neither can see the
other's session. `scripts/lane_check.py` reads what they do share — open pull
requests, checked-out worktrees, claimed issues — and refuses a path somebody
else is already holding.

These tests drive it with a stand-in for the commands rather than the real
repository, because a check that only passes while today's pull requests
happen to exist would say nothing tomorrow.
"""
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "lane_check.py"

_spec = importlib.util.spec_from_file_location("lane_check", SCRIPT)
lane_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lane_check)


def _runner(pulls=(), pull_files=None, worktrees="", branch_diffs=None,
            issues=()):
    """A stand-in for git and gh that answers from canned data."""
    pull_files = pull_files or {}
    branch_diffs = branch_diffs or {}

    def run(cmd, check=True):
        if cmd[:3] == ["gh", "pr", "list"]:
            return json.dumps(list(pulls))
        if cmd[:2] == ["gh", "api"]:
            url = next(part for part in cmd if "/pulls/" in part)
            number = int(url.split("/pulls/")[1].split("/")[0])
            return "\n".join(pull_files.get(number, []))
        if cmd[:3] == ["git", "worktree", "list"]:
            return worktrees
        if cmd[:3] == ["git", "rev-list", "--count"]:
            branch = cmd[3].split("..")[1]
            return "1" if branch_diffs.get(branch) else "0"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            branch = cmd[3].split("...")[1]
            return "\n".join(branch_diffs.get(branch, []))
        if cmd[:3] == ["gh", "issue", "list"]:
            return json.dumps(list(issues))
        raise AssertionError("unexpected command: %s" % cmd)

    return run


def test_a_directory_query_covers_what_is_under_it():
    assert lane_check.touches(["r6/"], ["r6/access.py"]) == ["r6/access.py"]
    assert lane_check.touches(["r6"], ["r6/access.py"]) == ["r6/access.py"]


def test_a_query_never_matches_a_neighbour_that_merely_starts_the_same():
    """`r6/access` must not claim `r6/access_kernel.py`, and `careagents`
    must not claim `careagents-old/`."""
    assert lane_check.touches(["r6/access"], ["r6/access_kernel.py"]) == []
    assert lane_check.touches(["careagents"], ["careagents-old/a.py"]) == []


def test_an_open_pull_request_on_the_same_file_is_a_collision():
    run = _runner(
        pulls=[{"number": 12, "title": "something", "headRefName": "x"}],
        pull_files={12: ["r6/access.py", "tests/test_access.py"]})

    collisions, lines = lane_check.report(["r6/access.py"], run)

    assert collisions == 1
    assert "PR #12" in lines[0] and "r6/access.py" in lines[0]


def test_a_pull_request_elsewhere_is_not_a_collision():
    run = _runner(
        pulls=[{"number": 12, "title": "elsewhere", "headRefName": "x"}],
        pull_files={12: ["careagents/app.py"]})

    assert lane_check.report(["r6/access.py"], run) == (0, [])


def test_a_worktree_holding_the_path_is_a_collision():
    """The case that proves it works across tools: the other agent's tree is
    on disk and its branch is pushed, whatever editor made it."""
    worktrees = ("worktree /tmp/other\nbranch refs/heads/codex/thing\n")
    run = _runner(worktrees=worktrees,
                  branch_diffs={"codex/thing": ["docs/quickstarts/README.md"]})

    collisions, lines = lane_check.report(["docs/quickstarts/"], run)

    assert collisions == 1
    assert "/tmp/other" in lines[0] and "codex/thing" in lines[0]


def test_a_worktree_sitting_on_main_is_not_a_collision():
    run = _runner(worktrees="worktree /tmp/root\nbranch refs/heads/main\n",
                  branch_diffs={"main": ["r6/access.py"]})

    assert lane_check.report(["r6/access.py"], run) == (0, [])


def test_a_claimed_issue_naming_the_path_is_a_collision():
    run = _runner(issues=[{"number": 9, "title": "rework r6/access.py",
                           "body": "", "assignees": [{"login": "someone"}]}])

    collisions, lines = lane_check.report(["r6/access.py"], run)

    assert collisions == 1
    assert "#9" in lines[0] and "someone" in lines[0]


def test_an_unclaimed_issue_naming_the_path_is_the_work_not_a_collision():
    """An open issue about a file is why you are here. Only a claim — an
    assignee — means somebody else said they were taking it.

    MUTATION: count unassigned issues too -> red.
    """
    run = _runner(issues=[{"number": 9, "title": "rework r6/access.py",
                           "body": "", "assignees": []}])

    assert lane_check.report(["r6/access.py"], run) == (0, [])


def test_the_script_documents_its_own_exit_codes():
    """Two is not a green light, and the docstring has to keep saying so:
    the whole point is that an unanswered question is not an answer."""
    assert "Exit codes" in lane_check.__doc__
    assert "not a green light" in lane_check.__doc__
