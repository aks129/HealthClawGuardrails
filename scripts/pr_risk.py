#!/usr/bin/env python3
"""Score a pull request's risk level R0-R3 from its diff.

    risk = max(path floor, diff signals)

The path floors and the level each signal raises a PR to live in
`.github/pr-risk.yml`. The detectors live here. Output is JSON.

SHADOW MODE: the pr-risk workflow turns this into a label and a comment. It
arms nothing, blocks nothing and merges nothing.

Reasons name the rule or signal and `path:line`. They never quote the diff:
a "new env secret" hit may be the secret itself, and the comment is public.

Usage:
    scripts/pr_risk.py --base origin/main --head HEAD
    scripts/pr_risk.py --diff-file pr.diff [--review-unavailable]
    scripts/pr_risk.py --diff-file pr.diff --comment   # markdown for the PR
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC = ROOT / ".github" / "pr-risk.yml"
OWNER_LOOK_LEVEL = 2
COMMENT_MARKER = "<!-- pr-risk -->"

SIGNALS = (
    "step_up_truthiness",
    "tenant_filter_removed",
    "human_confirmed_header",
    "new_write_route",
    "new_env_secret",
    "display_text_touched",
    "test_deleted",
    "test_skipped",
)


# ---------------------------------------------------------------- rubric


def load_rubric(path: Path = DEFAULT_RUBRIC) -> dict:
    import yaml

    rubric = yaml.safe_load(Path(path).read_text())
    validate_rubric(rubric)
    return rubric


def validate_rubric(rubric: dict) -> None:
    """A malformed rubric is an error, never a silent R0."""
    if not isinstance(rubric, dict):
        raise ValueError("rubric is not a mapping")
    if rubric.get("default_level") not in (0, 1, 2, 3):
        raise ValueError("rubric default_level must be 0-3")
    paths = rubric.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("rubric has no path rules")
    for rule in paths:
        if rule.get("level") not in (0, 1, 2, 3) or not rule.get("glob"):
            raise ValueError(f"bad path rule: {rule!r}")
    signals = rubric.get("signals") or {}
    missing = [name for name in SIGNALS if name not in signals]
    if missing:
        raise ValueError(f"rubric has no level for signals: {missing}")
    for name, level in signals.items():
        if level not in (0, 1, 2, 3):
            raise ValueError(f"bad level for signal {name}: {level!r}")


def glob_to_regex(glob: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_level(path: str, rubric: dict) -> tuple[int, str]:
    """Highest level of every rule `path` matches, and the glob that set it."""
    best, why = rubric["default_level"], "no rule (default)"
    matched = False
    for rule in rubric["paths"]:
        if glob_to_regex(rule["glob"]).match(path):
            if not matched or rule["level"] > best:
                best, why = rule["level"], rule["glob"]
            matched = True
    return best, why


# ---------------------------------------------------------------- diff


def parse_diff(diff_text: str) -> list[dict]:
    """Split a unified diff into files: old/new path, added/removed lines.

    Each line is (line_number, text). Both paths are kept, so a deleted or
    renamed-away guarded file still scores on its old path.
    """
    files: list[dict] = []
    cur = None
    old_no = new_no = 0
    in_hunk = False
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "added": [], "removed": []}
            files.append(cur)
            in_hunk = False
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            continue
        if cur is None:
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(2))
                in_hunk = True
            continue
        if not in_hunk:
            if raw.startswith("--- "):
                cur["old"] = _strip_prefix(raw[4:], "a/")
            elif raw.startswith("+++ "):
                cur["new"] = _strip_prefix(raw[4:], "b/")
            elif raw.startswith("rename from "):
                cur["old"] = raw[len("rename from "):]
            elif raw.startswith("rename to "):
                cur["new"] = raw[len("rename to "):]
            elif raw.startswith("deleted file mode"):
                cur["deleted"] = True
            elif raw.startswith("new file mode"):
                cur["created"] = True
            continue
        if raw.startswith("+"):
            cur["added"].append((new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            cur["removed"].append((old_no, raw[1:]))
            old_no += 1
        elif raw.startswith(" "):
            old_no += 1
            new_no += 1
    for f in files:
        if f["old"] == "/dev/null":
            f["old"], f["created"] = None, True
        if f["new"] == "/dev/null":
            f["new"], f["deleted"] = None, True
    return files


def _strip_prefix(path: str, prefix: str) -> str:
    path = path.rstrip("\t")
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path[len(prefix):] if path.startswith(prefix) else path


def _paths(f: dict) -> list[str]:
    return [p for p in dict.fromkeys((f.get("old"), f.get("new"))) if p]


def _where(f: dict, line_no: int | None = None) -> str:
    path = f.get("new") or f.get("old") or "?"
    return f"{path}:{line_no}" if line_no else path


# ---------------------------------------------------------------- signals

_PROSE = re.compile(r"\.(md|rst|txt)\Z")
_TEST = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py\Z|\.test\.[jt]s\Z|(^|/)e2e/")
_APP_CODE = re.compile(r"\A(r6|careagents|services/agent-orchestrator/src)/.*\.(py|ts|js)\Z")

_STEP_UP_CALL = re.compile(r"\bvalidate_step_up_token\s*\(")
_STEP_UP_DEF = re.compile(r"^\s*def\s+validate_step_up_token\b")
_STEP_UP_DESTRUCTURE = re.compile(
    r"^\s*\(?\s*[A-Za-z_]\w*\s*,\s*[A-Za-z_]\w*\s*\)?\s*=\s*"
    r"(?:await\s+)?[\w.]*validate_step_up_token\s*\(")

_TENANT_FILTER = re.compile(
    r"filter_by\([^)]*\btenant_id\b"
    r"|\.tenant_id\s*(?:==|!=|\.in_\()"
    r"|\btenant_id\s*(?:==|!=)"
    r"|\bwhere\b.*\btenant_id\b\s*(?:=|in\b)",
    re.IGNORECASE)

_HUMAN_CONFIRMED = re.compile(r"x[-_]human[-_]confirmed", re.IGNORECASE)

_WRITE_METHOD = r"""["'](?:POST|PUT|PATCH|DELETE)["']"""
_ROUTE_WITH_METHODS = re.compile(
    r"\.(?:route|add_url_rule)\((?:[^)]|\n)*?methods\s*=\s*[\[(][^\])]*?" + _WRITE_METHOD,
    re.IGNORECASE)
_PY_METHOD_DECORATOR = re.compile(r"^\s*@[\w.]+\.(?:post|put|patch|delete)\(", re.MULTILINE)
_JS_ROUTE = re.compile(r"\b(?:app|router|\w+Router)\.(?:post|put|patch|delete)\s*\(")

_SECRET_NAME = r"[A-Z0-9_]*(?:SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)[A-Z0-9_]*"
_ENV_READ = re.compile(
    r"os\.environ\s*\[\s*[\"'](" + _SECRET_NAME + r")[\"']"
    r"|os\.environ\.(?:get|setdefault|pop)\(\s*[\"'](" + _SECRET_NAME + r")[\"']"
    r"|os\.getenv\(\s*[\"'](" + _SECRET_NAME + r")[\"']"
    r"|process\.env\.(" + _SECRET_NAME + r")\b"
    r"|process\.env\[\s*[\"'](" + _SECRET_NAME + r")[\"']"
    r"|\bsecrets\.(" + _SECRET_NAME + r")\b")

_DISPLAY_TEXT = re.compile(r"""["'](?:display|text)["']""")

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
_SKIP = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(|#\s*noqa\b"
    r"|\b(?:it|test|describe)\.skip\(|\bxit\(")


def _joined_added_runs(f: dict) -> list[tuple[int, str]]:
    """Contiguous added lines joined, so a decorator split over lines matches."""
    runs: list[tuple[int, str]] = []
    start = prev = None
    buf: list[str] = []
    for no, text in f["added"]:
        if prev is not None and no == prev + 1:
            buf.append(text)
        else:
            if buf:
                runs.append((start, "\n".join(buf)))
            start, buf = no, [text]
        prev = no
    if buf:
        runs.append((start, "\n".join(buf)))
    return runs


def detect_signals(files: list[dict]) -> list[dict]:
    """Every diff signal as {signal, where}. Order follows the diff."""
    hits: list[dict] = []

    def hit(signal: str, f: dict, line_no: int | None = None) -> None:
        hits.append({"signal": signal, "where": _where(f, line_no)})

    # Net tenant filter lines across the whole diff, so a moved filter (same
    # line removed in one place, added in another) is not a removal.
    added_filters = Counter(
        text.strip() for f in files for _, text in f["added"]
        if _TENANT_FILTER.search(text))
    added_secrets = Counter()
    removed_secrets = Counter()
    for f in files:
        for _, text in f["added"]:
            for m in _ENV_READ.finditer(text):
                added_secrets[next(g for g in m.groups() if g)] += 1
        for _, text in f["removed"]:
            for m in _ENV_READ.finditer(text):
                removed_secrets[next(g for g in m.groups() if g)] += 1
    added_tests = Counter(
        m.group(1) for f in files for _, text in f["added"]
        if (m := _TEST_DEF.match(text)))

    for f in files:
        path = f.get("new") or f.get("old") or ""
        prose = bool(_PROSE.search(path))
        is_test = bool(_TEST.search(path))

        for no, text in f["added"]:
            if (_STEP_UP_CALL.search(text) and not _STEP_UP_DEF.match(text)
                    and not _STEP_UP_DESTRUCTURE.match(text) and not prose):
                hit("step_up_truthiness", f, no)
            if _HUMAN_CONFIRMED.search(text) and not prose:
                hit("human_confirmed_header", f, no)
            if not prose:
                for m in _ENV_READ.finditer(text):
                    name = next(g for g in m.groups() if g)
                    if added_secrets[name] > removed_secrets[name]:
                        hit("new_env_secret", f, no)
                        # Report a name once, wherever it first appears.
                        removed_secrets[name] = added_secrets[name]
                        break
            if _APP_CODE.match(path) and _DISPLAY_TEXT.search(text):
                hit("display_text_touched", f, no)
            if _SKIP.search(text) and not prose:
                hit("test_skipped", f, no)

        for no, text in f["removed"]:
            key = text.strip()
            if _TENANT_FILTER.search(text) and not prose:
                if added_filters[key] > 0:
                    added_filters[key] -= 1
                else:
                    hit("tenant_filter_removed", f, no)
            m = _TEST_DEF.match(text)
            if m and is_test:
                if added_tests[m.group(1)] > 0:
                    added_tests[m.group(1)] -= 1
                else:
                    hit("test_deleted", f, no)

        if is_test and f.get("deleted") and not prose:
            hit("test_deleted", f)

        if not prose and not is_test:
            for start, run in _joined_added_runs(f):
                m = (_ROUTE_WITH_METHODS.search(run)
                     or _PY_METHOD_DECORATOR.search(run)
                     or (path.endswith((".ts", ".js")) and _JS_ROUTE.search(run)))
                if m:
                    hit("new_write_route", f, start + run.count("\n", 0, m.start()))
    return hits


# ---------------------------------------------------------------- score


def score(diff_text: str, rubric: dict) -> dict:
    """The pure function: diff text + rubric -> level and reasons."""
    validate_rubric(rubric)
    files = parse_diff(diff_text)
    reasons: list[dict] = []
    level = 0
    paths: list[str] = []
    for f in files:
        for p in _paths(f):
            paths.append(p)
            lvl, why = path_level(p, rubric)
            reasons.append({"level": lvl, "kind": "path", "rule": why, "where": p})
            level = max(level, lvl)
    for h in detect_signals(files):
        lvl = rubric["signals"][h["signal"]]
        reasons.append({"level": lvl, "kind": "signal", "rule": h["signal"],
                        "where": h["where"]})
        level = max(level, lvl)
    if not files:
        # An empty or unparseable diff is not evidence of a safe change.
        level = max(level, rubric["default_level"])
        reasons.append({"level": rubric["default_level"], "kind": "empty",
                        "rule": "no files in diff", "where": ""})
    reasons.sort(key=lambda r: (-r["level"], r["kind"], r["where"]))
    return {"level": level, "label": f"risk:R{level}", "files": len(files),
            "paths": sorted(set(paths)), "reasons": reasons, "shadow": True}


def owner_look(level: int | None, review_unavailable: bool = False,
               error: bool = False) -> bool:
    """R2/R3, a reviewer that could not run, or a scorer error -> yes."""
    if error or review_unavailable or level is None:
        return True
    return level >= OWNER_LOOK_LEVEL


def _code(text: str) -> str:
    # Paths come from the PR, so keep them inert inside a code span.
    return "`" + re.sub(r"[`\r\n<>]", "?", text) + "`"


def render_comment(result: dict | None, head_sha: str = "",
                   review_unavailable: bool = False, error: str = "") -> str:
    """The sticky PR comment. `result` is None when the scorer failed."""
    level = None if result is None else result["level"]
    look = owner_look(level, review_unavailable, bool(error))
    lines = [COMMENT_MARKER,
             "> **Shadow mode.** This label and comment change nothing: no "
             "merge is armed, blocked or approved because of them.",
             ""]
    if result is None:
        lines.append("**Risk: unknown (scorer error)** · owner look needed: **YES**")
        if error:
            lines.append("")
            lines.append(f"Error: {_code(error[:300])}")
    else:
        lines.append(f"**Risk R{level}** · owner look needed: "
                     f"**{'YES' if look else 'no'}** · {result['files']} file(s)")
    if head_sha:
        lines.append(f"Head {_code(head_sha[:12])}")
    if review_unavailable:
        lines.append("")
        lines.append("The standards reviewer is not configured "
                     "(ANTHROPIC_API_KEY unset), so no review stands behind this "
                     "PR; owner look is YES.")
    if result is not None:
        top = [r for r in result["reasons"] if r["level"] == level]
        rest = [r for r in result["reasons"] if r["level"] != level]
        lines.append("")
        lines.append(f"Why R{level}:")
        for r in top[:20]:
            lines.append(f"- {r['kind']} {_code(r['rule'])} at {_code(r['where'])}")
        if len(top) > 20:
            lines.append(f"- … and {len(top) - 20} more")
        if rest:
            lines.append("")
            lines.append(f"<details><summary>{len(rest)} lower-level reason(s)</summary>")
            lines.append("")
            for r in rest[:50]:
                lines.append(f"- R{r['level']} {r['kind']} {_code(r['rule'])} "
                             f"at {_code(r['where'])}")
            lines.append("")
            lines.append("</details>")
    lines.append("")
    lines.append("Rubric: `.github/pr-risk.yml` on the default branch. "
                 "Scorer: `scripts/pr_risk.py`.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def git_diff(base: str, head: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
         "--no-textconv", "-M", "--unified=0", f"{base}...{head}"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff-file", help="unified diff to score ('-' for stdin)")
    src.add_argument("--base", help="base ref; scores git diff BASE...HEAD")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    ap.add_argument("--review-unavailable", action="store_true",
                    help="the AI reviewer could not run; forces owner look")
    ap.add_argument("--comment", action="store_true",
                    help="print the PR comment markdown instead of JSON")
    ap.add_argument("--head-sha", default="")
    args = ap.parse_args(argv)

    if args.diff_file:
        diff = (sys.stdin.read() if args.diff_file == "-"
                else Path(args.diff_file).read_text(errors="replace"))
    else:
        diff = git_diff(args.base, args.head)
    result = score(diff, load_rubric(Path(args.rubric)))
    result["owner_look"] = owner_look(result["level"], args.review_unavailable)
    result["review_unavailable"] = args.review_unavailable
    if args.comment:
        sys.stdout.write(render_comment(result, args.head_sha, args.review_unavailable))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
