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
