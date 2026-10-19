"""PR risk scorer (scripts/pr_risk.py) and its shadow-mode workflow.

Table-driven over synthetic diffs. Each signal row has a near-miss negative,
and every assertion names the reason kind and path, so a scorer that returns
R3 for everything goes red as surely as one that returns R0.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pr_risk", ROOT / "scripts" / "pr_risk.py")
pr_risk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_risk)

RUBRIC = pr_risk.load_rubric()


def diff(path, added=(), removed=(), *, new=False, deleted=False, old_path=None):
    """A minimal unified diff for one file."""
    old = old_path or path
    head = [f"diff --git a/{old} b/{path}"]
    if new:
        head += ["new file mode 100644", "--- /dev/null", f"+++ b/{path}"]
    elif deleted:
        head += ["deleted file mode 100644", f"--- a/{old}", "+++ /dev/null"]
    else:
        head += [f"--- a/{old}", f"+++ b/{path}"]
    body = [f"@@ -1,{len(removed)} +1,{len(added)} @@"]
    body += ["-" + line for line in removed] + ["+" + line for line in added]
    return "\n".join(head + body) + "\n"


def signals(result):
    return {(r["rule"], r["where"].split(":")[0])
            for r in result["reasons"] if r["kind"] == "signal"}


# ------------------------------------------------------------- path floors

@pytest.mark.parametrize("path, level", [
    # R3: the guardrails, the rules that score them, and deploy.
    ("r6/access.py", 3), ("r6/redaction.py", 3), ("r6/audit.py", 3),
    ("r6/stepup.py", 3), ("r6/oauth.py", 3), ("r6/read_auth.py", 3),
    ("r6/internal_auth.py", 3), ("r6/terminology.py", 3),
    ("r6/health_compliance.py", 3), ("r6/routes.py", 3),
    ("r6/actions/review.py", 3), ("r6/actions/rails/x.py", 3),
    ("careagents/accounts.py", 3), ("careagents/config.py", 3),
    ("services/agent-orchestrator/src/oauth-path.ts", 3),
    (".env.example", 3), ("tests/test_guardrail_conformance.py", 3),
    (".github/REVIEW_STANDARDS.md", 3), (".github/pr-risk.yml", 3),
    ("scripts/pr_risk.py", 3), ("tests/test_pr_risk.py", 3),
    (".github/workflows/pr-risk.yml", 3), (".github/workflows/publish.yml", 3),
    (".github/workflows/claude-pr-review.yml", 3),
    # R2: logic, CI, dependencies, schema, container config.
    ("careagents/agent.py", 2), ("r6/smbp/x.py", 2),
    ("services/agent-orchestrator/src/index.ts", 2),
    (".github/workflows/ci.yml", 2), ("pyproject.toml", 2), ("uv.lock", 2),
    ("migrations/versions/abc.py", 2), ("Dockerfile", 2), ("railway.toml", 2),
    ("tests/test_ci_hardening.py", 2),
    # R1: UI, scripts, specs, and anything unlisted.
    ("careagents/templates/auth.html", 1), ("careagents/static/careagents.css", 1),
    ("scripts/demo_smoke.py", 1), ("docs/qa/sign-off-standard.md", 1),
    ("docs/specs/x.md", 1), ("somewhere/new_thing.txt", 1),
    # R0: prose and tests.
    ("docs/notes.md", 0), ("README.md", 0), ("tests/test_something.py", 0),
    ("tests/actions/test_x.py", 0),
    # Round 2: helpers under tests/, runner config, what gate pages pull in.
    ("tests/approval_helpers.py", 1), ("tests/evals/data.json", 1),
    ("tests/pytest.ini", 2), ("setup.cfg", 2), ("tox.ini", 2),
    ("services/x/pyproject.toml", 2), ("conftest.py", 2),
    ("templates/_review_handlers.html", 3), ("templates/review_base.html", 3),
    ("careagents/templates/base.html", 3), ("templates/action_approve.html", 3),
])
def test_path_floor(path, level):
    result = pr_risk.score(diff(path, added=["x = 1"]), RUBRIC)
    assert result["level"] == level, result["reasons"]
    assert result["label"] == f"risk:R{level}"
    assert any(r["kind"] == "path" and r["where"] == path and r["level"] == level
               for r in result["reasons"])


def test_glob_star_does_not_cross_directories_but_double_star_does():
    assert pr_risk.glob_to_regex("*.md").match("README.md")
    assert not pr_risk.glob_to_regex("*.md").match("docs/README.md")
    assert pr_risk.glob_to_regex("**/*.md").match("docs/a/README.md")
    assert pr_risk.glob_to_regex("**/*.md").match("README.md")
    assert not pr_risk.glob_to_regex("r6/access.py").match("r6/access.pyc")
    assert not pr_risk.glob_to_regex("r6/access.py").match("xr6/access.py")


def test_deleting_a_guarded_file_scores_on_its_old_path():
    result = pr_risk.score(diff("r6/redaction.py", removed=["x = 1"], deleted=True), RUBRIC)
    assert result["level"] == 3
    assert "r6/redaction.py" in result["paths"]


def test_renaming_a_guarded_file_away_scores_on_its_old_path():
    text = ("diff --git a/r6/audit.py b/docs/old.md\n"
            "similarity index 100%\nrename from r6/audit.py\nrename to docs/old.md\n")
    result = pr_risk.score(text, RUBRIC)
    assert result["level"] == 3
    assert set(result["paths"]) == {"r6/audit.py", "docs/old.md"}


def test_empty_diff_is_not_r0():
    result = pr_risk.score("", RUBRIC)
    assert result["level"] == RUBRIC["default_level"] == 1
    assert result["reasons"][0]["kind"] == "empty"


# ------------------------------------------------------------- diff signals
# (path, added, removed, expected signal or None). Paths are R0/R1 so the
# signal alone must lift the level.

SIGNAL_ROWS = [
    # validate_step_up_token: anything but a two-name destructure.
    ("scripts/x.py", ["    if validate_step_up_token(tok, tid):"], [], "step_up_truthiness"),
    ("scripts/x.py", ["    if not stepup.validate_step_up_token(tok, tid):"], [], "step_up_truthiness"),
    ("scripts/x.py", ["    ok = validate_step_up_token(tok, tid)"], [], "step_up_truthiness"),
    ("scripts/x.py", ["    return validate_step_up_token(tok, tid)"], [], "step_up_truthiness"),
    ("scripts/x.py", ["    assert validate_step_up_token(tok, tid)"], [], "step_up_truthiness"),
    ("scripts/x.py", ["    valid, error = validate_step_up_token(tok, tid)"], [], None),
    ("scripts/x.py", ["    (ok, why) = _m.validate_step_up_token("], [], None),
    ("scripts/x.py", ["def validate_step_up_token(token, tenant_id):"], [], None),
    ("docs/x.md", ["`if validate_step_up_token(t):` is a bypass"], [], None),
    # tenant_id filters removed and not re-added.
    ("scripts/x.py", [], ["    q = Resource.query.filter_by(tenant_id=tid, id=rid)"], "tenant_filter_removed"),
    ("scripts/x.py", [], ["    .filter(Resource.tenant_id == tid)"], "tenant_filter_removed"),
    ("scripts/x.py", [], ["    'SELECT * FROM r WHERE tenant_id = ?'"], "tenant_filter_removed"),
    ("scripts/x.py", ["    q = Resource.query.filter_by(tenant_id=tid, id=rid)"],
     ["    q = Resource.query.filter_by(tenant_id=tid, id=rid)"], None),
    ("scripts/x.py", [], ["    log.info('tenant_id', tid)"], None),
    ("scripts/x.py", ["    .filter(Resource.tenant_id == tid)"], [], None),
    # X-Human-Confirmed in any spelling, outside prose.
    ("scripts/x.py", ["    h = request.headers.get('X-Human-Confirmed')"], [], "human_confirmed_header"),
    ("scripts/x.py", ["    v = environ['HTTP_X_HUMAN_CONFIRMED']"], [], "human_confirmed_header"),
    ("scripts/x.py", ["    h = request.headers.get('X-Human-Approved')"], [], None),
    ("docs/x.md", ["The X-Human-Confirmed header is gap #214."], [], None),
    # New write routes.
    ("scripts/x.py", ["@bp.route('/x', methods=['POST'])"], [], "new_write_route"),
    ("scripts/x.py", ["@app.route(", "    '/x',", "    methods=[\"GET\", \"DELETE\"],", ")"], [], "new_write_route"),
    ("scripts/x.py", ["@app.post('/x')"], [], "new_write_route"),
    ("scripts/x.py", ["    @bp.patch('/x/<id>')"], [], "new_write_route"),
    ("scripts/x.ts", ["app.put('/x', handler);"], [], "new_write_route"),
    ("scripts/x.py", ["@bp.route('/x', methods=['GET'])"], [], None),
    ("scripts/x.py", ["@app.get('/x')"], [], None),
    ("scripts/x.py", ["    r = client.post('/x')"], [], None),
    ("tests/test_x.py", ["@app.post('/x')"], [], None),
    # New secrets read from the environment.
    ("scripts/x.py", ["KEY = os.environ['STRIPE_SECRET_KEY']"], [], "new_env_secret"),
    ("scripts/x.py", ["t = os.environ.get('BOT_TOKEN', '')"], [], "new_env_secret"),
    ("scripts/x.py", ["p = os.getenv(\"DB_PASSWORD\")"], [], "new_env_secret"),
    ("scripts/x.ts", ["const k = process.env.API_KEY;"], [], "new_env_secret"),
    ("scripts/x.yml", ["  K: ${{ secrets.DEPLOY_TOKEN }}"], [], "new_env_secret"),
    ("scripts/x.py", ["port = os.environ.get('PORT', '5000')"], [], None),
    ("scripts/x.py", ["t = os.environ.get('BOT_TOKEN', None)"],
     ["t = os.environ.get('BOT_TOKEN', '')"], None),
    # Tests deleted or skipped.
    ("tests/test_x.py", [], ["def test_redaction_strips_names():"], "test_deleted"),
    ("tests/test_x.py", ["def test_a(client):"], ["def test_a():"], None),
    ("tests/test_x.py", ["@pytest.mark.skip(reason='flaky')"], [], "test_skipped"),
    ("tests/test_x.py", ["@pytest.mark.xfail(strict=True)"], [], "test_skipped"),
    ("scripts/x.py", ["import os  # noqa: F401"], [], "test_skipped"),
    ("tests/test_x.py", ["def test_b():"], [], None),
    ("tests/test_x.py", ["    pytest.exit('done', returncode=0)"], [], "test_skipped"),
    ("tests/test_x.py", ["    monkeypatch.setattr('r6.access.require_grant', fake)"], [], "test_patches_engine"),
    ("tests/test_x.py", ["    with patch(\"r6.redaction.apply_redaction\"):"], [], "test_patches_engine"),
    ("tests/test_x.py", ["    with mock.patch.object(r6.stepup, 'x'):"], [], "test_patches_engine"),
    ("tests/test_x.py", ["    monkeypatch.setattr('careagents.llm.call', fake)"], [], None),
    ("scripts/x.py", ["    monkeypatch.setattr('r6.access.require_grant', fake)"], [], None),
]


@pytest.mark.parametrize("path, added, removed, expected", SIGNAL_ROWS)
def test_diff_signal(path, added, removed, expected):
    result = pr_risk.score(diff(path, added=added, removed=removed), RUBRIC)
    floor = pr_risk.path_level(path, RUBRIC)[0]
    found = signals(result)
    if expected is None:
        assert found == set(), found
        assert result["level"] == floor
    else:
        assert found == {(expected, path)}, found
        assert result["level"] == max(floor, RUBRIC["signals"][expected])
        assert result["level"] > floor, "the row must prove the signal lifts the level"


def test_display_text_signal_is_application_code_only():
    hit = pr_risk.score(diff("r6/smbp/x.py", added=["  label = c.get('display')"]), RUBRIC)
    assert ("display_text_touched", "r6/smbp/x.py") in signals(hit)
    miss = pr_risk.score(diff("scripts/x.py", added=["  label = c.get('display')"]), RUBRIC)
    assert signals(miss) == set()


def test_a_deleted_test_file_is_a_signal():
    result = pr_risk.score(diff("tests/test_x.py", removed=["x = 1"], deleted=True), RUBRIC)
    assert ("test_deleted", "tests/test_x.py") in signals(result)
    assert result["level"] == 2


def test_a_moved_tenant_filter_across_files_is_not_a_removal():
    text = (diff("scripts/a.py", removed=["    .filter(R.tenant_id == tid)"])
            + diff("scripts/b.py", added=["    .filter(R.tenant_id == tid)"]))
    assert signals(pr_risk.score(text, RUBRIC)) == set()


def test_signal_reasons_carry_line_numbers_and_never_the_line():
    secret = "sk_live_should_never_be_echoed"
    text = diff("scripts/x.py", added=["x = 1", f"KEY = os.environ['STRIPE_SECRET_KEY'] or '{secret}'"])
    result = pr_risk.score(text, RUBRIC)
    hit = [r for r in result["reasons"] if r["rule"] == "new_env_secret"]
    assert hit and hit[0]["where"] == "scripts/x.py:2"
    assert secret not in json.dumps(result)
    assert secret not in pr_risk.render_comment(result)


@pytest.mark.parametrize("parts, level", [
    ([("docs/x.md", ["ok"]), ("scripts/x.py", ["if validate_step_up_token(t, i):"])], 3),
    ([("r6/access.py", ["x = 1"])], 3),
    ([("careagents/agent.py", ["x = 1"]), ("tests/test_x.py", ["@pytest.mark.skip"])], 2),
    ([("docs/x.md", ["ok"]), ("careagents/static/a.css", ["a{}"])], 1),
    ([("docs/x.md", ["ok"]), ("README.md", ["ok"])], 0),
])
def test_level_is_the_max_over_paths_and_signals(parts, level):
    text = "".join(diff(p, added=a) for p, a in parts)
    assert pr_risk.score(text, RUBRIC)["level"] == level


def test_a_diff_with_context_lines_keeps_line_numbers_right():
    text = ("diff --git a/scripts/x.py b/scripts/x.py\n--- a/scripts/x.py\n+++ b/scripts/x.py\n"
            "@@ -10,3 +10,4 @@\n a = 1\n-b = 2\n+b = 3\n+ok = validate_step_up_token(t, i)\n c = 4\n")
    hit = [r for r in pr_risk.score(text, RUBRIC)["reasons"] if r["kind"] == "signal"]
    assert [r["where"] for r in hit] == ["scripts/x.py:12"]


# ------------------------------------------------------------- rubric itself

def test_every_signal_has_a_level_and_a_missing_one_is_an_error():
    assert set(RUBRIC["signals"]) == set(pr_risk.SIGNALS)
    broken = {**RUBRIC, "signals": {k: v for k, v in RUBRIC["signals"].items()
                                    if k != "step_up_truthiness"}}
    with pytest.raises(ValueError, match="step_up_truthiness"):
        pr_risk.score("", broken)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(default_level=7),
    lambda r: r.update(paths=[]),
    lambda r: r["paths"].append({"level": 4, "glob": "x"}),
    lambda r: r["signals"].update(new_write_route="high"),
])
def test_a_malformed_rubric_is_an_error_not_a_zero(mutate):
    rubric = yaml.safe_load((ROOT / ".github" / "pr-risk.yml").read_text())
    mutate(rubric)
    with pytest.raises(ValueError):
        pr_risk.validate_rubric(rubric)


def test_the_five_named_r3_signals_are_r3():
    for name in ("step_up_truthiness", "tenant_filter_removed", "human_confirmed_header",
                 "new_write_route", "new_env_secret"):
        assert RUBRIC["signals"][name] == 3, name


# ------------------------------------------------------------- owner look

@pytest.mark.parametrize("level, unavailable, error, look", [
    (0, False, False, False), (1, False, False, False),
    (2, False, False, True), (3, False, False, True),
    (0, True, False, True), (1, False, True, True), (None, False, False, True),
])
def test_owner_look(level, unavailable, error, look):
    assert pr_risk.owner_look(level, unavailable, error) is look


def test_comment_has_marker_shadow_banner_and_verdict():
    result = pr_risk.score(diff("README.md", added=["ok"]), RUBRIC)
    body = pr_risk.render_comment(result, head_sha="abc1234def5678")
    assert body.startswith(pr_risk.COMMENT_MARKER)
    assert "Shadow mode" in body
    assert "**Risk R0**" in body and "owner look needed: **no**" in body
    assert "`abc1234def56`" in body
    body = pr_risk.render_comment(result, review_unavailable=True)
    assert "owner look needed: **YES**" in body and "ANTHROPIC_API_KEY" in body
    body = pr_risk.render_comment(None, error="boom")
    assert "scorer error" in body and "owner look needed: **YES**" in body


def test_comment_keeps_hostile_paths_inert():
    text = diff("docs/`x`<img src=y>.md", added=["ok"])
    body = pr_risk.render_comment(pr_risk.score(text, RUBRIC))
    assert "<img" not in body and "`x`" not in body


def test_cli_scores_a_diff_file(tmp_path, capsys):
    f = tmp_path / "pr.diff"
    f.write_text(diff("r6/access.py", added=["x = 1"]))
    assert pr_risk.main(["--diff-file", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == 3 and out["owner_look"] is True and out["shadow"] is True
    f.write_text(diff("README.md", added=["x"]))
    pr_risk.main(["--diff-file", str(f)])
    assert json.loads(capsys.readouterr().out)["owner_look"] is False
    pr_risk.main(["--diff-file", str(f), "--review-unavailable"])
    assert json.loads(capsys.readouterr().out)["owner_look"] is True


# ------------------------------------------------------------- workflow

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pr-risk.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())


def test_workflow_runs_only_on_pull_request_target():
    triggers = WORKFLOW.get(True) or WORKFLOW["on"]
    assert set(triggers) == {"pull_request_target"}


def test_workflow_permissions_are_exactly_comment_and_label():
    assert WORKFLOW["permissions"] == {
        "contents": "read", "pull-requests": "write", "issues": "write"}
    for job in WORKFLOW["jobs"].values():
        assert "permissions" not in job


def test_workflow_never_checks_out_or_runs_pr_code():
    """The scorer and rubric come from the default branch; the PR is read as
    a diff through the API. A checkout of the head, or a fetch of the PR ref,
    would put PR-controlled code next to a write token."""
    text = WORKFLOW_PATH.read_text()
    checkouts = [s for job in WORKFLOW["jobs"].values() for s in job["steps"]
                 if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "main"
    assert checkouts[0]["with"].get("persist-credentials") is False
    for forbidden in ("pull_request.head.ref", "refs/pull/", "head.repo", "git fetch",
                      "git checkout"):
        assert forbidden not in text, forbidden
    assert "gh pr diff" in text


def test_workflow_is_shadow_only():
    """It labels and comments. It must not merge, arm, approve or fail a check."""
    text = WORKFLOW_PATH.read_text()
    for forbidden in ("gh pr merge", "--auto", "enablePullRequestAutoMerge",
                      "merge(", "createReview", "APPROVE", "setFailed", "exit 1"):
        assert forbidden not in text, forbidden
    assert "Shadow mode" in text
    for label in ("risk:R", "owner-look:"):
        assert label in text


def test_workflow_fails_toward_owner_look_yes():
    text = WORKFLOW_PATH.read_text()
    assert "--review-unavailable" in text
    assert re.search(r"secrets\.ANTHROPIC_API_KEY\s*!=\s*''", text)
    script = [s for s in WORKFLOW["jobs"]["score"]["steps"]
              if s.get("uses", "").startswith("actions/github-script@")][0]["with"]["script"]
    catch = script[script.index("catch (e)"):]
    assert "look = true" in catch and "scorer error" in catch


def test_workflow_rescores_on_a_base_change_and_pins_third_party_actions():
    triggers = WORKFLOW.get(True) or WORKFLOW["on"]
    assert "edited" in triggers["pull_request_target"]["types"]
    for step in WORKFLOW["jobs"]["score"]["steps"]:
        uses = str(step.get("uses", ""))
        if uses and not uses.startswith(("actions/checkout@", "actions/github-script@")):
            assert re.search(r"@[0-9a-f]{40}$", uses), uses


def test_workflow_forces_owner_look_when_the_default_branch_is_not_main():
    steps = WORKFLOW["jobs"]["score"]["steps"]
    first = steps[0]
    assert first["if"].replace(" ", "") == "${{github.event.repository.default_branch!='main'}}"
    assert "FORCE_OWNER_LOOK=true" in first["run"]
    script = steps[-1]["with"]["script"]
    assert "FORCE_OWNER_LOOK" in script and "default_branch !== 'main'" in script


def test_the_label_step_runs_even_when_scoring_fails():
    last = WORKFLOW["jobs"]["score"]["steps"][-1]
    assert last["uses"].startswith("actions/github-script@")
    assert "always()" in last["if"]


def test_a_quoted_path_git_would_not_quote_is_unparsed():
    result = pr_risk.score('diff --git "a/docs/x.md" "b/docs/x.md"\n', RUBRIC)
    assert result["level"] == 3
    assert any(r["kind"] == "unparsed" for r in result["reasons"])


def test_quoted_and_spaced_paths_decode():
    files = pr_risk.parse_diff(
        'diff --git "a/r6/\\303\\251 x.py" "b/r6/\\303\\251 x.py"\n'
        "diff --git a/docs/a b.md b/docs/a b.md\n")
    assert [(f["old"], f["new"]) for f in files] == [
        ("r6/é x.py", "r6/é x.py"), ("docs/a b.md", "docs/a b.md")]
    assert not any(f.get("unparsed") for f in files)


def test_a_comment_over_the_cap_is_truncated_with_the_marker_first():
    text = "".join(diff(f"docs/{i:04d}{'d' * 150}.md", added=["x"]) for i in range(600))
    body = pr_risk.render_comment(pr_risk.score(text + diff("r6/access.py", added=["x"]), RUBRIC))
    assert len(body) <= pr_risk.COMMENT_MAX
    assert body.startswith(pr_risk.COMMENT_MARKER)
