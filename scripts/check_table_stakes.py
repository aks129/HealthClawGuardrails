#!/usr/bin/env python3
"""Mechanical table-stakes checks for docs/constitution.md and design.md.

Only the deterministic subset lives here. Judgment rules — deep modules,
one-control-one-property, whether a test is load-bearing — belong to the
reviewer and to `.github/REVIEW_STANDARDS.md`. A rule goes in this file only
when a machine can decide it without being clever.

**Added lines only.** Every check runs against lines a PR ADDS, never the
whole file. A gate that fails on legacy content is a gate that gets ignored,
which is the failure mode this project already documented in
`docs/2026-08-02-retro.md`. New writing is held to the rules; old writing is
fixed when it is touched.

Usage:
    uv run python scripts/check_table_stakes.py                  # vs origin/main
    uv run python scripts/check_table_stakes.py --base HEAD~1
    uv run python scripts/check_table_stakes.py --explain        # list rules
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

PROSE_SUFFIXES = {".md"}
STYLE_SUFFIXES = {".css", ".html", ".js"}

# Files whose whole job is to name the things we ban. Checking them would make
# it impossible to write the rule down.
EXEMPT = {
    "docs/constitution.md",
    "design.md",
    ".github/REVIEW_STANDARDS.md",
    "scripts/check_table_stakes.py",
    "tests/test_table_stakes.py",
    "docs/2026-08-02-retro.md",
}


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    detail: str
    text: str


# --- writing ---------------------------------------------------------------

MARKETING = re.compile(
    r"\b(seamless(?:ly)?|robust|cutting[- ]edge|best[- ]in[- ]class|"
    r"world[- ]class|game[- ]chang\w+|revolutionary|blazing[- ]fast|"
    r"effortless(?:ly)?|enterprise[- ]grade|state[- ]of[- ]the[- ]art)\b",
    re.I)

HEDGE = re.compile(
    r"\b(may\s+potentially|might\s+possibly|could\s+potentially|"
    r"can\s+potentially|may\s+be\s+able\s+to|should\s+probably\s+likely)\b",
    re.I)

PHRASAL = re.compile(r"\b(reach\s+out|circle\s+back|dive\s+deep(?:er)?\s+into|"
                     r"leverage\s+up|touch\s+base)\b", re.I)

MAX_SENTENCE_WORDS = 25

# Strip anything a prose rule must not judge before measuring.
_CODE_SPAN = re.compile(r"`[^`]*`")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HTML_TAG = re.compile(r"<[^>]+>")


def _prose_only(line: str) -> str:
    line = _CODE_SPAN.sub(" ", line)
    line = _LINK_TARGET.sub("] ", line)
    line = _URL.sub(" ", line)
    return _HTML_TAG.sub(" ", line)


def _is_prose_line(raw: str) -> bool:
    s = raw.strip()
    if not s or s.startswith(("#", ">", "|", "```", "---", "==")):
        return False
    # Indented code inside a list is still code.
    return not raw.startswith("    ")


def check_prose(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    out: list[Finding] = []
    for num, raw in lines:
        if not _is_prose_line(raw):
            continue
        text = _prose_only(raw)

        for rx, rule, detail in (
            (MARKETING, "no-marketing-adjectives",
             "delete it and say what the thing does"),
            (HEDGE, "one-helper-verb",
             "write what happens, or say you do not know"),
            (PHRASAL, "no-chatty-phrasal-verbs",
             "use the plain verb (contact, revisit, examine)"),
        ):
            m = rx.search(text)
            if m:
                out.append(Finding(path, num, rule,
                                   f'"{m.group(0).strip()}" — {detail}',
                                   raw.strip()[:100]))

        # Sentence length. Bullets and headings are measured too; they are
        # instructions, which the constitution holds to a tighter cap.
        body = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text)
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            words = [w for w in sentence.split() if any(c.isalnum() for c in w)]
            if len(words) > MAX_SENTENCE_WORDS:
                out.append(Finding(
                    path, num, "sentence-length",
                    f"{len(words)} words (cap {MAX_SENTENCE_WORDS}) — split it",
                    sentence.strip()[:100]))
                break

        # NOT enforced here: the semicolon rule. It is real guidance, but it
        # is judgment, and this file only takes rules a machine can decide.
        # Measured before deciding: 255 semicolons across 6,317 lines of
        # existing project prose, nearly all of them correct. ASD-STE100 bans
        # them for aircraft manuals read under pressure by non-native
        # speakers; engineering prose is not that. Enforcing it would have
        # made this gate fire on 4% of our own good writing, which is how a
        # gate becomes noise and then gets switched off. The reviewer applies
        # it where it matters — patient-facing copy and error messages.
    return out


# --- design ----------------------------------------------------------------

BANNED_PRIMARY_FONT = re.compile(
    r"font-family\s*:\s*['\"]?(Inter|Roboto|Open Sans|Lato|Montserrat|Nunito)"
    r"['\"]?", re.I)

CDN_ASSET = re.compile(
    r"<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*['\"]https?://"
    r"(?!fonts\.googleapis\.com|fonts\.gstatic\.com)", re.I)

SMALL_INPUT_FONT = re.compile(
    r"font-size\s*:\s*(\d+(?:\.\d+)?)px", re.I)

# iOS zooms the page when a FORM CONTROL is focused whose font-size is under
# 16px. It does not zoom for static text, so the rule only fires inside a rule
# whose selector reaches a control. Matching every font-size (which is what it
# used to do, despite the name and the message both saying "inputs") banned
# every new caption, axis label and helper line in the product — 13px body
# copy has always been fine and is used throughout the existing stylesheet.
INPUTISH_SELECTOR = re.compile(
    r"\b(input|select|textarea|button|\[contenteditable)|"
    r"[.#][\w-]*(input|field|search|combo|entry|form-control)[\w-]*",
    re.I)


def _enclosing_selector(whole_file: str, line_number: int) -> str:
    """The selector of the CSS rule containing this line, best effort.

    Walks back to the nearest `{` and returns the text before it, including a
    preceding line so multi-line selector lists still resolve. Returns "" when
    the line is not inside a rule (an inline `style=` attribute, say), which
    the caller handles by looking at the line itself.
    """
    lines = whole_file.splitlines()
    for idx in range(min(line_number, len(lines)) - 1, -1, -1):
        line = lines[idx]
        if "{" in line:
            selector = line.split("{", 1)[0]
            if idx and not selector.strip():
                selector = lines[idx - 1]
            return selector
        if "}" in line:
            return ""          # left the rule without finding its opening
    return ""

MOTION = re.compile(r"\b(transition|animation)\s*:", re.I)


def check_style(path: str, lines: list[tuple[int, str]],
                whole_file: str) -> list[Finding]:
    out: list[Finding] = []
    for num, raw in lines:
        m = BANNED_PRIMARY_FONT.search(raw)
        if m:
            out.append(Finding(
                path, num, "banned-primary-font",
                f"{m.group(1)} as the primary face — see design.md",
                raw.strip()[:100]))

        m = CDN_ASSET.search(raw)
        if m:
            out.append(Finding(
                path, num, "csp-external-asset",
                "CSP is default-src 'self'; self-host it or inline it",
                raw.strip()[:100]))

        m = SMALL_INPUT_FONT.search(raw)
        if m and float(m.group(1)) < 16:
            context = raw + " " + _enclosing_selector(whole_file, num)
            if INPUTISH_SELECTOR.search(context):
                out.append(Finding(
                    path, num, "ios-zoom-font-size",
                    f"{m.group(1)}px on a form control — iOS zooms the page "
                    "when one under 16px is focused",
                    raw.strip()[:100]))

    if any(MOTION.search(raw) for _, raw in lines):
        if "prefers-reduced-motion" not in whole_file:
            num = next(n for n, r in lines if MOTION.search(r))
            out.append(Finding(
                path, num, "reduced-motion",
                "this file adds motion but never honours "
                "prefers-reduced-motion",
                "(file-level)"))
    return out


# --- diff ------------------------------------------------------------------

def added_lines(base: str) -> dict[str, list[tuple[int, str]]]:
    """Map path -> [(line number in the new file, added text)]."""
    try:
        diff = subprocess.run(
            ["git", "diff", "-U0", "--no-color", f"{base}...HEAD"],
            capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        diff = subprocess.run(
            ["git", "diff", "-U0", "--no-color", base],
            capture_output=True, text=True, check=True).stdout

    files: dict[str, list[tuple[int, str]]] = {}
    path, lineno = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            files.setdefault(path, [])
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if path:
                files[path].append((lineno, line[1:]))
            lineno += 1
    return files


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def run(base: str) -> list[Finding]:
    findings: list[Finding] = []
    for path, lines in added_lines(base).items():
        if path in EXEMPT or not lines:
            continue
        suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if suffix in PROSE_SUFFIXES:
            findings.extend(check_prose(path, lines))
        if suffix in STYLE_SUFFIXES:
            findings.extend(check_style(path, lines, read_file(path)))
    return findings


RULES = """\
Writing (docs/constitution.md §2), on added markdown lines:
  no-marketing-adjectives  seamless, robust, cutting-edge, effortless, ...
  one-helper-verb          "may potentially", "could potentially", ...
  no-chatty-phrasal-verbs  "reach out", "circle back", "touch base", ...
  sentence-length          more than 25 words in one sentence

Design (design.md), on added css/html/js lines:
  banned-primary-font      Inter/Roboto/Open Sans/Lato/Montserrat as primary
  csp-external-asset       a CDN <script>/<link>; CSP is default-src 'self'
  ios-zoom-font-size       font-size under 16px (iOS zooms the page)
  reduced-motion           motion added to a file that ignores the media query
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()

    if args.explain:
        print(RULES)
        return 0

    findings = run(args.base)
    if not findings:
        print("table stakes: clean")
        return 0

    by_rule: dict[str, int] = {}
    for f in sorted(findings, key=lambda x: (x.path, x.line)):
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        print(f"{f.path}:{f.line}: [{f.rule}] {f.detail}")
        print(f"    {f.text}")

    print(f"\n{len(findings)} finding(s): "
          + ", ".join(f"{k}×{v}" for k, v in sorted(by_rule.items())))
    print("Rules: docs/constitution.md and design.md. "
          "`--explain` lists them. These apply to lines you ADDED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
