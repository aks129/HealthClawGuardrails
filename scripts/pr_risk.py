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
# GitHub rejects a comment over 65,536 characters; a rejected comment must
# never be the reason a stale label survives, so stay well under it.
COMMENT_MAX = 50_000
CODE_SPAN_MAX = 160

SIGNALS = (
    "step_up_truthiness",
    "tenant_filter_removed",
    "human_confirmed_header",
    "new_write_route",
    "new_env_secret",
    "display_text_touched",
    "test_deleted",
    "test_skipped",
    "test_patches_engine",
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

    Lines split on "\\n" only, as git and GitHub do. str.splitlines() also
    breaks on U+2028, \\x0b, a lone \\r and others, which would let one added
    line carry a forged `diff --git` header. Paths git C-quotes are decoded;
    a header that does not parse, or quotes a path git would not have
    quoted, marks the file `unparsed` so it fails toward the owner.
    """
    files: list[dict] = []
    cur = None
    old_no = new_no = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "added": [], "removed": []}
            files.append(cur)
            in_hunk = False
            pair = _split_pair(raw[len("diff --git "):], "a/", "b/")
            if pair is None:
                cur["unparsed"] = True
            else:
                cur["old"], cur["new"] = pair
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
            for prefix, key, strip in (("--- ", "old", "a/"), ("+++ ", "new", "b/"),
                                       ("rename from ", "old", ""),
                                       ("rename to ", "new", ""),
                                       ("copy from ", "old", ""),
                                       ("copy to ", "new", "")):
                if raw.startswith(prefix):
                    path = _header_path(raw[len(prefix):].rstrip("\t"), strip, cur)
                    if path is not None:
                        cur[key] = path
                    break
            else:
                if raw.startswith("Binary files ") and raw.endswith(" differ"):
                    pair = _split_pair(raw[len("Binary files "):-len(" differ")],
                                       "a/", "b/", sep=" and ")
                    if pair is None:
                        cur["unparsed"] = True
                    else:
                        cur["old"] = pair[0] or cur["old"]
                        cur["new"] = pair[1] or cur["new"]
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
        if not f["old"] and not f["new"]:
            f["unparsed"] = True
    return files


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
              '"': 34, "\\": 92}


def _needs_quoting(path: str) -> bool:
    """Would git (default core.quotePath) C-quote this path?"""
    return any(c in '"\\' or ord(c) < 0x20 or ord(c) >= 0x7f for c in path)


def _read_token(text: str) -> tuple[str, str, bool] | None:
    """One path token from the front of `text`: (path, rest, was_quoted).

    A quoted token is git's C-quoting: backslash escapes and \\ooo octal
    bytes, decoded as UTF-8. An unquoted token is the whole text.
    """
    if not text.startswith('"'):
        return text, "", False
    out = bytearray()
    i = 1
    while i < len(text):
        c = text[i]
        if c == '"':
            return out.decode("utf-8", "replace"), text[i + 1:], True
        if c == "\\":
            if re.match(r"[0-7]{3}", text[i + 1:i + 4]):
                out.append(int(text[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            nxt = text[i + 1:i + 2]
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
            return None
        out.extend(c.encode("utf-8"))
        i += 1
    return None


def _split_pair(text: str, a: str, b: str, sep: str = " ") -> tuple | None:
    """Two paths from `a/X b/Y` (or `A and B` on a Binary line), either side
    C-quoted or /dev/null. None when the pair cannot be read unambiguously."""
    if text.startswith('"'):
        first = _read_token(text)
        if first is None or not first[1].startswith(sep):
            return None
        left, rest, lq = first[0], first[1][len(sep):], True
    else:
        # Unquoted left side: the right side starts at a separator followed
        # by the b/ prefix, a quote, or /dev/null. A name with spaces is only
        # unambiguous when old == new, so prefer the split that gives that.
        cands = [i for i in range(len(text)) if text.startswith(sep, i)
                 and (text.startswith(b, i + len(sep))
                      or text.startswith('"', i + len(sep))
                      or text.startswith("/dev/null", i + len(sep)))]
        if not cands:
            return None
        even = [i for i in cands if text[len(a):i] == text[i + len(sep) + len(b):]]
        i = (even or cands)[0]
        left, rest, lq = text[:i], text[i + len(sep):], False
    second = _read_token(rest)
    if second is None or second[1]:
        return None
    right, rq = second[0], second[2]
    out = []
    for path, quoted, prefix in ((left, lq, a), (right, rq, b)):
        if path == "/dev/null" and not quoted:
            out.append(None)
            continue
        if not path.startswith(prefix) or (quoted and not _needs_quoting(path)):
            return None
        out.append(path[len(prefix):])
    return tuple(out)


def _header_path(text: str, prefix: str, cur: dict) -> str | None:
    """A path from a ---/+++/rename/copy header. Marks `cur` unparsed on a
    malformed or non-canonical quote, or a missing a/ b/ prefix."""
    if text == "/dev/null":
        return text
    if text.startswith('"'):
        tok = _read_token(text)
        if tok is None or tok[1] or not _needs_quoting(tok[0]):
            cur["unparsed"] = True
            return None
        text = tok[0]
    if prefix:
        if not text.startswith(prefix):
            cur["unparsed"] = True
            return None
        text = text[len(prefix):]
    return text


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
    r"\bmark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail|importorskip)\("
    r"|#\s*noqa\b|\bunittest\.skip|\bpytest_(?:collection_modifyitems|ignore_collect)\b"
    r"|\bpytest\.exit\(|\b(?:it|test|describe)\.skip\(|\bxit\(")
# A test that swaps out engine code can make a guardrail test pass against a
# stub. Aliased imports and getattr evade this; it is a tripwire, not a proof.
_PATCHES_ENGINE = re.compile(
    r"\b(?:monkeypatch\.(?:setattr|setitem|delattr)|patch(?:\.object|\.dict)?)"
    r"\(\s*[\"']?r6\.")


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
            if is_test and _PATCHES_ENGINE.search(text):
                hit("test_patches_engine", f, no)

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
    for f in files:
        if f.get("unparsed"):
            # The parser lost track of a file: fail toward the owner.
            reasons.append({"level": 3, "kind": "unparsed",
                            "rule": "a diff header the scorer could not read",
                            "where": _where(f)})
            level = 3
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
    text = re.sub(r"[`\r\n<>\u2028\u2029]", "?", text)
    if len(text) > CODE_SPAN_MAX:
        text = text[:CODE_SPAN_MAX - 1] + "…"
    return "`" + text + "`"


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
    lines.append("Rubric: `.github/pr-risk.yml` on `main`. "
                 "Scorer: `scripts/pr_risk.py`.")
    body = "\n".join(lines) + "\n"
    if len(body) > COMMENT_MAX:
        cut = "\n\n… comment truncated; the full reason list is in the workflow run.\n"
        body = body[:COMMENT_MAX - len(cut)] + cut
    return body


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
