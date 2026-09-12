"""Static regression tests for CI and supply-chain security controls."""

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def test_dependency_audits_are_enforcing_and_do_not_mutate_lockfiles():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    dependency_job = text.split("  dependency-audit:", 1)[1].split(
        "  compliance-gates:", 1
    )[0]

    assert "uv add" not in dependency_job
    assert "|| true" not in dependency_job
    assert "npm audit --audit-level=high" in dependency_job
    assert "pip-audit" in dependency_job


def test_reusable_security_workflow_is_pinned_to_commit_sha():
    workflow = _workflow("security-baseline.yml")
    reusable = workflow["jobs"]["scan"]["uses"]

    assert not reusable.endswith("@main")
    assert re.search(r"@[0-9a-f]{40}$", reusable), reusable


def test_dependabot_updates_both_node_projects():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    npm_directories = {
        entry["directory"]
        for entry in config["updates"]
        if entry["package-ecosystem"] == "npm"
    }

    assert npm_directories == {"/services/agent-orchestrator", "/e2e"}


def test_python_quality_gates_include_measured_coverage_and_gradual_typing():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert "--cov-fail-under=80" in workflow
    assert "uv run mypy" in workflow
    assert "pytest-cov" in pyproject
    assert "mypy" in pyproject
    assert "[tool.mypy]" in pyproject


def test_local_compose_security_defaults_are_documented_and_loopback_bound():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    example_env = (ROOT / ".env.example").read_text()

    assert compose["services"]["fhir-mcp-guardrails"]["ports"][0].startswith(
        "127.0.0.1:"
    )
    assert compose["services"]["agent-orchestrator"]["ports"][0].startswith(
        "127.0.0.1:"
    )
    assert "MCP_AUTH_TOKEN=" in example_env
    assert "READ_AUTH_ENABLED=" in example_env


def test_ci_runs_on_every_pull_request_including_stacked_ones():
    """No base-branch filter on `pull_request`, or stacked PRs get no tests.

    A `branches:` list under `pull_request` filters on the *base* branch, so a
    PR stacked on another feature branch matched nothing and the whole test
    pipeline was skipped — 19 checks on a PR into main, 6 on a stacked one,
    none of the 6 a test, while the job that arms auto-merge ran on both (#585).

    PINNED BY SHAPE, NOT BY THE ONE WORD THAT WAS DELETED. `branches` was how
    it was spelled; `branches-ignore: ['fix/**']` recreates #585 exactly and
    is a different word, and `paths` / `paths-ignore` reach the same end state
    on a different axis — the workflow skipped, no test run, the pull request
    page clean. A test that only forbids `branches` would pass through all
    three, which is the failure mode #585 is about.
    """
    workflow = _workflow("ci.yml")
    # PyYAML resolves the bare `on:` key to the boolean True (YAML 1.1).
    triggers = workflow.get(True) or workflow["on"]

    assert "pull_request" in triggers, (
        "ci.yml no longer runs on pull requests at all: "
        f"{sorted(map(str, triggers))}"
    )
    pull_request = triggers["pull_request"] or {}

    reinstated = sorted({"branches", "branches-ignore",
                         "paths", "paths-ignore"} & set(pull_request))
    assert not reinstated, (
        f"ci.yml scopes its pull_request trigger with {reinstated}; a pull "
        f"request the filter does not match runs no test at all: "
        f"{pull_request}"
    )

    # And nothing may put the filter back one level down. A job gated on
    # `github.base_ref` is the same filter wearing an `if:`, and it skips
    # green — which reads as a pass rather than as an absence.
    #
    # Only `if:` is inspected. The `lint` job legitimately READS
    # `${GITHUB_BASE_REF}` in a `run:` step to pick the diff base for
    # scripts/check_table_stakes.py; that is the job doing its work on a
    # stacked base, not refusing to.
    for job_name, job in workflow["jobs"].items():
        gates = [("job", job.get("if"))]
        gates += [(f"step {n}", step.get("if"))
                  for n, step in enumerate(job.get("steps") or [])]
        for where, gate in gates:
            if not gate:
                continue
            assert "base_ref" not in str(gate), (
                f"{job_name} ({where}) gates itself on the base branch, which "
                f"is the #585 filter one level down: {gate}")
            assert "pull_request.base" not in str(gate), (
                f"{job_name} ({where}) gates itself on the base branch, which "
                f"is the #585 filter one level down: {gate}")


def test_nothing_that_runs_on_a_pull_request_can_write_anything():
    """The safety argument for dropping the base filter, stated as a test.

    Removing the filter is only safe because every job it re-enables reads.
    Now that `ci.yml` fires on a pull request into ANY base, a workflow that
    publishes an image, deploys, files an issue or comments would start doing
    it from an unreviewed feature branch — worse than the bug #585 describes.
    So: every workflow triggered by `pull_request` gets read-only
    permissions, at the workflow level and at every job.

    `pull_request_target` is deliberately NOT in scope. `claude-pr-review.yml`
    needs `pull-requests: write` to post its review and label its verdict, and
    is safe on a fork because it checks out the DEFAULT branch and never
    executes pull-request code. It also already ran on stacked pull requests
    before this change — it was two of the six.

    MUTATION: add `packages: write` to ci.yml's `permissions:`, or give any
    single job its own write scope -> red naming the workflow and the scope.
    """
    workflow_dir = ROOT / ".github" / "workflows"
    checked = []
    for path in sorted(workflow_dir.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on")
        if isinstance(triggers, str):
            triggers = [triggers]
        if isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        if "pull_request" not in (triggers or {}):
            continue
        checked.append(path.name)

        scopes = [("workflow", workflow.get("permissions"))]
        scopes += [(name, job.get("permissions"))
                   for name, job in workflow["jobs"].items()]
        for where, permissions in scopes:
            if permissions is None:
                # No declaration at a job means it inherits the workflow's,
                # which this same loop has already checked.
                continue
            if permissions == "read-all":
                # The string form GitHub also accepts at this position.
                # `read-all` is read-only and passes; `write-all` is caught
                # by the assertion below, which is why this only whitelists
                # the one value rather than every string.
                continue
            assert isinstance(permissions, dict), (
                f"{path.name} ({where}) grants permissions wholesale: "
                f"{permissions!r}")
            writes = sorted(scope for scope, level in permissions.items()
                            if level != "read" and level != "none")
            assert not writes, (
                f"{path.name} ({where}) can write {writes} and runs on every "
                f"pull request, into any base branch")

    # The loop above is vacuous if it matched nothing, and a workflow file
    # renamed out of the glob would make it so silently.
    assert sorted(checked) == ["ci.yml", "security-baseline.yml"], checked


def test_every_event_a_job_condition_names_is_one_the_workflow_listens_for():
    """A job whose `if:` tests `github.event_name == 'x'` while the workflow
    never triggers on `x` can never run; it is skipped before its first step,
    and a skipped job reads as green in a check list. That is how #643 shipped
    with a reviewer that never reviewed: the trigger moved to
    pull_request_target and the condition still said pull_request.

    MUTATION: put `github.event_name == 'pull_request'` back into
    pr-agent.yml's job `if:` -> red naming the workflow, the job and the event.
    """
    import re
    workflow_dir = ROOT / ".github" / "workflows"
    checked = 0
    for path in sorted(workflow_dir.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on")
        if isinstance(triggers, str):
            triggers = [triggers]
        if isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        listens_for = set(triggers or {})
        for job_name, job in workflow["jobs"].items():
            condition = job.get("if")
            if not isinstance(condition, str):
                continue
            named = set(re.findall(r"github\.event_name\s*==\s*'([a-z_]+)'", condition))
            for event in sorted(named):
                checked += 1
                assert event in listens_for, (
                    f"{path.name} job {job_name!r} runs only on "
                    f"github.event_name == {event!r}, but the workflow never "
                    f"triggers on {event!r} (it listens for "
                    f"{sorted(listens_for)}); the job can never run")
    assert checked >= 1, "no job condition names an event; the scan is broken"


def test_a_cancelling_concurrency_group_is_keyed_on_the_event_when_a_workflow_has_several():
    """cancel-in-progress cancels whatever else is running in the same group,
    and a workflow with several triggers puts several kinds of run in it. The
    second reviewer keyed its group on the pull-request number alone, so the
    issue_comment run that any comment fires (a bot comment lands seconds
    after a pull request opens) cancelled the review that had just started;
    the comment run then skipped itself, and the check list showed a cancelled
    reviewer on every pull request (#653). A group that names the event keeps
    the runs of one kind from cancelling another.

    MUTATION: drop `${{ github.event_name }}` from pr-agent.yml's group -> red
    naming the workflow and its triggers.
    """
    workflow_dir = ROOT / ".github" / "workflows"
    checked = 0
    for path in sorted(workflow_dir.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on")
        if isinstance(triggers, str):
            triggers = [triggers]
        if isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        events = sorted(triggers or {})
        concurrency = workflow.get("concurrency")
        if not isinstance(concurrency, dict) or not concurrency.get("cancel-in-progress"):
            continue
        if len(events) < 2:
            continue
        checked += 1
        group = str(concurrency.get("group", ""))
        assert "github.event_name" in group, (
            f"{path.name} cancels in-progress runs and triggers on {events}, "
            f"but its concurrency group {group!r} does not name the event, so "
            f"a run of one kind cancels a run of another")
    assert checked >= 1, "no workflow cancels in-progress across several triggers; the scan is broken"
