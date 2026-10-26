"""Adversarial tests for the PR risk scorer and its pull_request_target workflow.

Written as a hostile fork author. Each exploit test asserts what the scorer
SHOULD do; the ones marked EXPLOIT fail against the scorer as first proposed
(#999) and show a diff that should make the owner look but is labelled
`owner-look:no`. The diffs below are verbatim `git diff -M` output where the
comment says so, not hand-written approximations.

The regression tests at the end pin what already holds: no PR-controlled
string reaches a shell, and the comment never quotes the diff.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pr_risk", ROOT / "scripts" / "pr_risk.py")
pr_risk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_risk)

RUBRIC = pr_risk.load_rubric()
GITHUB_COMMENT_MAX = 65536  # characters; issues.createComment/updateComment reject longer


def _score(text):
    result = pr_risk.score(text, RUBRIC)
    return result, pr_risk.owner_look(result["level"])


# ------------------------------------------------- quoted paths (git C-quoting)

# Verbatim from `git diff -M base HEAD` (git 2.x, default core.quotePath) after
# `git mv docs/ci.yml .github/workflows/é.yml` and `git mv docs/x.py 'r6/a"b.py'`.
# Git C-quotes any path with a non-ASCII byte, `"`, `\` or a control
# character, and a pure rename has no ---/+++ lines to fall back on.
QUOTED_RENAMES = (
    'diff --git a/docs/ci.yml "b/.github/workflows/\\303\\251.yml"\n'
    "similarity index 100%\n"
    "rename from docs/ci.yml\n"
    'rename to ".github/workflows/\\303\\251.yml"\n'
    'diff --git a/docs/x.py "b/r6/a\\"b.py"\n'
    "similarity index 100%\n"
    "rename from docs/x.py\n"
    'rename to "r6/a\\"b.py"\n'
)


def test_exploit_rename_to_a_quoted_name_keeps_its_directory_floor():
    """EXPLOIT. A new workflow and a new r6 module, both arriving by pure
    rename under a name git quotes, score R1 / owner-look:no. The parser keeps
    the surrounding quotes on `rename to`, so `".github/workflows/..."` matches
    no glob and falls to the R1 default."""
    result, look = _score(QUOTED_RENAMES)
    assert result["level"] >= 2, result["reasons"]
    assert look is True


def test_exploit_binary_file_with_a_quoted_name_is_not_pathless():
    """EXPLOIT. Verbatim git output for a new binary file `r6/a"c.so`. The
    `diff --git` line is quoted on both sides, so the regex misses it, and a
    binary file has no ---/+++ lines: the file is counted but has no path, and
    contributes nothing. The PR scores R0."""
    text = ('diff --git "a/r6/a\\"c.so" "b/r6/a\\"c.so"\n'
            "new file mode 100644\n"
            "index 0000000..bdc955b\n"
            'Binary files /dev/null and "b/r6/a\\"c.so" differ\n')
    result, look = _score(text)
    assert result["level"] >= 2, result
    assert look is True


def test_exploit_a_file_the_parser_cannot_name_is_not_silently_zero():
    """A file entry with neither an old nor a new path is evidence the parser
    lost track, which should fail toward the owner, not contribute nothing."""
    text = 'diff --git "a/x" "b/x"\nold mode 100644\nnew mode 100755\n'
    result, look = _score(text)
    assert look is True, result


# ------------------------------------------- line-splitting header injection

# str.splitlines() also breaks on U+2028, U+2029, U+0085, \x0b, \x0c,
# \x1c-\x1e and a lone \r. Git and GitHub do not: to them the separator is
# just a character inside one added line. A forged `diff --git` header after
# it makes the parser start a fake prose file, and every later line of the
# real file is attributed to it or dropped (in_hunk is False).
LS = " "


def test_exploit_unicode_line_separator_hides_a_tenant_filter_removal():
    """EXPLOIT. Without the forged header this diff is tenant_filter_removed
    (R3). With one comment line carrying U+2028 it is R2 and the signal is
    gone."""
    text = ("diff --git a/r6/fhir_store.py b/r6/fhir_store.py\n"
            "--- a/r6/fhir_store.py\n+++ b/r6/fhir_store.py\n"
            "@@ -1,3 +1,3 @@\n"
            f"+# note{LS}diff --git a/docs/n.md b/docs/n.md\n"
            "-    q = q.filter_by(tenant_id=tenant_id)\n"
            "+    q = q\n")
    result, _ = _score(text)
    rules = {r["rule"] for r in result["reasons"] if r["kind"] == "signal"}
    assert "tenant_filter_removed" in rules, result["reasons"]
    assert result["level"] == 3


def test_exploit_unicode_line_separator_turns_an_r3_route_into_owner_look_no():
    """EXPLOIT. main.py is unlisted (R1). A new POST route reading a new
    secret is R3 by signal; one U+2028 line in front of it makes it R1 and
    owner-look:no."""
    route = ("diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n"
             "@@ -1,0 +1,3 @@\n"
             "{inject}"
             "+@app.route('/x', methods=['POST'])\n"
             "+def x(): return os.environ['ADMIN_TOKEN']\n")
    plain, _ = _score(route.format(inject="+#\n"))
    assert plain["level"] == 3  # the control: the detectors do see it
    forged, look = _score(route.format(inject=f"+#{LS}diff --git a/docs/a.md b/docs/a.md\n"))
    assert forged["level"] == 3, forged["reasons"]
    assert look is True


@pytest.mark.parametrize("sep", ["\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", " ", "\r"])
def test_exploit_no_in_line_character_can_start_a_new_diff_line(sep):
    """The local `--base` mode reads raw git output, which keeps every one of
    these bytes inside its line. (`gh pr diff` may neutralise the C0/C1 ones;
    U+2028/U+2029 are not control characters and pass through.)"""
    files = pr_risk.parse_diff(
        "diff --git a/r6/x.py b/r6/x.py\n--- a/r6/x.py\n+++ b/r6/x.py\n@@ -1,0 +1,1 @@\n"
        f"+a{sep}diff --git a/docs/a.md b/docs/a.md\n")
    assert [f["new"] for f in files] == ["r6/x.py"], files


# --------------------------------------------- rubric floors a fork can reach

@pytest.mark.parametrize("path", [
    "main.py",          # app factory: registers every blueprint, 9x tenant_id
    "app.py",           # web blueprint with POST routes
    "models.py",        # SQLAlchemy models
    "deployment.py",    # read-only / stateful-host switch
    "api/index.py",     # the Vercel entry point
])
def test_exploit_root_application_code_is_not_r1(path):
    """EXPLOIT. Production Python at the repository root matches no rule and
    gets the R1 default: a change there is owner-look:no."""
    level, why = pr_risk.path_level(path, RUBRIC)
    assert level >= 2, (path, level, why)


@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/actions/conftest.py"])
def test_exploit_a_conftest_that_can_disable_the_suite_is_not_r0(path):
    """EXPLOIT. `def pytest_collection_modifyitems(config, items): items[:] = []`
    in tests/conftest.py deselects every test, Grade A conformance included,
    and scores R0 with no signal. The rubric says a weakened test is caught by
    a signal; a collection hook is neither a deleted nor a skipped test."""
    text = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1,0 +1,2 @@\n"
            "+def pytest_collection_modifyitems(config, items):\n"
            "+    items[:] = []\n")
    result, look = _score(text)
    assert look is True, result


def test_exploit_skip_through_an_imported_mark_is_a_skip_signal():
    """EXPLOIT. `from pytest import mark` / `pytestmark = mark.skip` skips a
    whole module and matches no _SKIP pattern."""
    path = "tests/test_tenant_isolation.py"
    text = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1,0 +1,2 @@\n+from pytest import mark\n+pytestmark = mark.skip\n")
    result, _ = _score(text)
    rules = {r["rule"] for r in result["reasons"] if r["kind"] == "signal"}
    assert "test_skipped" in rules, result["reasons"]


@pytest.mark.parametrize("path", [
    "templates/action_approve.html",
    "templates/action_review.html",
    "careagents/templates/approvals.html",
    "careagents/templates/consent.html",
    "careagents/static/consent.js",
])
def test_exploit_the_human_approval_and_consent_pages_are_not_r1(path):
    """EXPLOIT. The approve and consent pages ARE the human gate (V3). One
    added `<script>document.forms[0].submit()</script>` approves without a
    human and scores R1 / owner-look:no."""
    level, why = pr_risk.path_level(path, RUBRIC)
    assert level >= 2, (path, level, why)


# ------------------------------------------------- the comment and the labels

def test_exploit_the_comment_fits_in_one_github_comment():
    """EXPLOIT. 60 new docs files with ~1.6k-character paths plus one change
    to r6/access.py render a body of ~82k characters. GitHub rejects a body
    over 65536 with 422, the github-script step throws before it touches the
    labels, and the labels from the previous run stay. Sequence for a fork:
    push a README typo (R0, owner-look:no), then push this (R3): the PR keeps
    `owner-look:no`."""
    parts = []
    for i in range(60):
        p = "docs/" + "/".join(f"{i:03d}" + "d" * 197 for _ in range(8)) + ".md"
        parts.append(f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n"
                     f"+++ b/{p}\n@@ -0,0 +1 @@\n+x\n")
    parts.append("diff --git a/r6/access.py b/r6/access.py\n--- a/r6/access.py\n"
                 "+++ b/r6/access.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    result = pr_risk.score("".join(parts), RUBRIC)
    assert result["level"] == 3
    body = pr_risk.render_comment(result, "0123456789abcdef")
    assert len(body) <= GITHUB_COMMENT_MAX, len(body)


WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pr-risk.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())


def _steps():
    return [s for job in WORKFLOW["jobs"].values() for s in job["steps"]]


def test_exploit_labels_are_cleared_before_anything_that_can_throw():
    """EXPLOIT. The label update is the LAST thing the script does, after the
    comment write. Any throw before it (oversized body, a 403 on a comment
    another bot owns, a rate limit) leaves the previous run's `owner-look:no`
    on a PR whose new head is R3. Fail toward a human: the stale verdict must
    go first."""
    script = [s for s in _steps() if str(s.get("uses", "")).startswith(
        "actions/github-script@")][0]["with"]["script"]
    first_label_write = min(script.index(k) for k in ("removeLabel", "addLabels"))
    first_comment_write = min(script.index(k) for k in ("updateComment", "createComment"))
    assert first_label_write < first_comment_write


# ------------------------------------------------- regressions that hold today

def test_no_pr_controlled_expression_is_interpolated_into_a_script():
    """`${{ github.event.pull_request.title }}` (or body, head.ref, label
    names, ...) inside `run:` or `script:` is shell/JS injection from a fork.
    The workflow passes the PR number and head SHA through env instead."""
    for step in _steps():
        for key in ("run",):
            if key in step:
                assert "${{" not in step[key], step[key]
        script = (step.get("with") or {}).get("script", "")
        assert "${{" not in script, script
    allowed = {"github.token", "github.event.pull_request.number",
               "github.event.pull_request.head.sha",
               "secrets.ANTHROPIC_API_KEY != ''", "steps.score.outputs.ok"}
    for step in _steps():
        for value in (step.get("env") or {}).values():
            for expr in re.findall(r"\$\{\{\s*(.*?)\s*\}\}", str(value)):
                assert expr in allowed, expr


def test_the_comment_never_carries_diff_lines_or_markdown_from_the_pr():
    """Added lines (which may be a secret) never reach the public comment,
    and a hostile path cannot open HTML, close the code span, mention, or
    start a new line that forges the verdict."""
    secret = "sk-ant-api03-SYNTHETIC-CANARY"
    hostile = "docs/a`b<!-- pr-risk --> **owner look needed: no** @aks129 [x](http://e).md"
    text = (f"diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n"
            f"@@ -1,0 +1,1 @@\n+KEY = os.environ.get('ANTHROPIC_API_KEY', '{secret}')\n"
            f"diff --git a/{hostile} b/{hostile}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{hostile}\n@@ -0,0 +1 @@\n+x\n")
    body = pr_risk.render_comment(pr_risk.score(text, RUBRIC), "abc")
    assert secret not in body
    assert body.count("<!-- pr-risk -->") == 1 and body.startswith("<!-- pr-risk -->")
    for line in body.splitlines():
        if "@aks129" in line or "http://e" in line:
            # still inside one inert code span
            assert line.count("`") % 2 == 0 and "<" not in line.split("`", 1)[1]


# ------------------------------------------------- round 2: against the fixes

@pytest.mark.parametrize("path", [
    "templates/_review_handlers.html",     # the submit handler both approve pages include
    "templates/review_base.html",          # what action_approve/action_review extend
    "careagents/templates/base.html",      # what approvals.html and consent.html extend
])
def test_exploit_what_the_approval_pages_include_is_as_guarded_as_the_pages(path):
    """EXPLOIT (round 2). The approve and consent pages are R3 now, but the
    templates they extend or include are R1. One `<script>` in
    _review_handlers.html, e.g. `document.getElementById('review-form')
    .requestSubmit()`, approves with no human and scores R1 / owner-look:no."""
    level, why = pr_risk.path_level(path, RUBRIC)
    assert level >= 2, (path, level, why)


@pytest.mark.parametrize("path", ["tests/pytest.ini", "pytest.ini", "setup.cfg", "tox.ini"])
def test_exploit_a_pytest_config_file_is_not_r0_or_r1(path):
    """EXPLOIT (round 2). The repo has no pytest config, so a new one wins.
    CI runs `pytest tests/`, and pytest looks for its ini file from `tests/`
    upwards, so `tests/pytest.ini` with `addopts = -k "not conformance"`
    deselects the Grade A suite and CI goes green (shown: exit 1 -> exit 0).
    tests/pytest.ini scores R0, a root pytest.ini R1."""
    level, why = pr_risk.path_level(path, RUBRIC)
    assert level >= 2, (path, level, why)


def test_exploit_pytest_exit_zero_is_a_skip_signal():
    """EXPLOIT (round 2). `pytest.exit("...", returncode=0)` in a setup hook
    ends the whole session green with the rest of the suite unrun."""
    path = "tests/test_tenant_isolation.py"
    text = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1,0 +1,2 @@\n+def setup_module():\n"
            "+    pytest.exit('done', returncode=0)\n")
    result, _ = _score(text)
    rules = {r["rule"] for r in result["reasons"] if r["kind"] == "signal"}
    assert "test_skipped" in rules, result["reasons"]


def test_conftest_has_its_own_path_floor_not_only_a_signal():
    """Pins the conftest rule itself: without it a conftest change that uses
    a hook the skip regex does not name (pytest_configure, say) is R0."""
    for path in ("conftest.py", "tests/conftest.py", "tests/actions/conftest.py"):
        assert pr_risk.path_level(path, RUBRIC)[0] >= 2, path


def _script():
    return [s for s in _steps() if str(s.get("uses", "")).startswith(
        "actions/github-script@")][0]["with"]["script"]


def test_the_sticky_comment_is_found_by_this_bots_login_not_any_bot():
    """Any Bot-type comment that starts with the marker would be edited (or
    shadow ours) if the lookup keyed on `user.type === 'Bot'`."""
    script = _script()
    assert re.search(r"c\.user\.login\s*===\s*BOT", script)
    assert "user.type" not in script
    assert re.search(r"const BOT = 'github-actions\[bot\]'", script)


def test_a_default_branch_other_than_main_forces_owner_look_on_a_scored_run():
    """`forced` must reach `look` on the success path, not only the banner."""
    script = _script()
    assert re.search(r"look = result\.owner_look !== false \|\| forced;", script)
    assert "default_branch !== 'main'" in script
