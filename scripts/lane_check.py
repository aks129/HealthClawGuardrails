#!/usr/bin/env python3
"""Answer one question before you start: is anybody else already in here?

Two agents work this repository, in different tools, and neither can see the
other's session. What they share is git and GitHub, so that is where the
answer has to come from. Give this the paths you are about to change and it
reports every open pull request, every checked-out worktree and every claimed
issue that already touches them, and exits non-zero when it finds one.

    scripts/lane_check.py r6/access.py careagents/
    scripts/lane_check.py --branch feat/my-thing
    scripts/lane_check.py --no-fetch r6/          # offline, may be stale

Exit codes: 0 nothing else is in those paths, 1 something is (the report says
what), 2 the question could not be answered — a failed fetch, no `gh`. Two is
not a green light: an unanswered question is the one case where you ask
rather than assume.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

MAIN = "origin/main"


def run(cmd: list[str], check: bool = True) -> str:
    """Run a command and return stdout, or "" when it failed and check is off."""
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        if check:
            raise RuntimeError(
                "%s failed: %s" % (" ".join(cmd), done.stderr.strip()[:200]))
        return ""
    return done.stdout


def touches(query: list[str], changed: list[str]) -> list[str]:
    """The changed paths that fall inside any queried path.

    A query naming a directory covers everything under it; a query naming a
    file matches that file. Both are compared as path segments, so `r6/a.py`
    is not taken to be inside a query for `r6/a`.
    """
    hits = []
    for path in changed:
        for want in query:
            want = want.rstrip("/")
            if path == want or path.startswith(want + "/"):
                hits.append(path)
                break
    return sorted(set(hits))


def open_pull_requests(runner=run) -> list[dict]:
    listing = runner(["gh", "pr", "list", "--state", "open", "--limit", "100",
                      "--json", "number,title,headRefName"], check=False)
    if not listing:
        return []
    return json.loads(listing)


def pull_request_files(number: int, runner=run) -> list[str]:
    body = runner(["gh", "api", "--paginate",
                   "repos/{owner}/{repo}/pulls/%d/files" % number,
                   "--jq", ".[].filename"], check=False)
    return [line for line in body.splitlines() if line]


def worktree_branches(runner=run) -> list[tuple[str, str]]:
    """(branch, worktree path) for every worktree not sitting on main."""
    out, path, found = [], None, runner(["git", "worktree", "list",
                                         "--porcelain"], check=False)
    for line in found.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].replace("refs/heads/", "")
            if branch != "main" and path:
                out.append((branch, path))
    return out


def branch_files(branch: str, runner=run) -> list[str]:
    ahead = runner(["git", "rev-list", "--count", "%s..%s" % (MAIN, branch)],
                   check=False).strip()
    if not ahead or ahead == "0":
        return []
    diff = runner(["git", "diff", "--name-only", "%s...%s" % (MAIN, branch)],
                  check=False)
    return [line for line in diff.splitlines() if line]


def claimed_issues(query: list[str], runner=run) -> list[dict]:
    """Open issues that name one of these paths and already have an assignee.

    An unassigned issue naming the path is not a collision — it is the work
    itself. An assigned one means somebody said they were taking it.
    """
    listing = runner(["gh", "issue", "list", "--state", "open", "--limit",
                      "100", "--json", "number,title,body,assignees"],
                     check=False)
    if not listing:
        return []
    out = []
    for issue in json.loads(listing):
        if not issue.get("assignees"):
            continue
        haystack = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
        if any(want.rstrip("/") in haystack for want in query):
            out.append(issue)
    return out


def report(query: list[str], runner=run) -> tuple[int, list[str]]:
    lines, collisions = [], 0

    for pull in open_pull_requests(runner):
        hit = touches(query, pull_request_files(pull["number"], runner))
        if hit:
            collisions += 1
            lines.append("PR #%s (%s) already changes: %s"
                         % (pull["number"], pull["headRefName"],
                            ", ".join(hit[:6])))
            lines.append("    %s" % pull["title"])

    for branch, path in worktree_branches(runner):
        hit = touches(query, branch_files(branch, runner))
        if hit:
            collisions += 1
            lines.append("worktree %s is on %s, which changes: %s"
                         % (path, branch, ", ".join(hit[:6])))

    for issue in claimed_issues(query, runner):
        who = ", ".join(a.get("login", "?") for a in issue["assignees"])
        collisions += 1
        lines.append("issue #%s is claimed by %s and names these paths: %s"
                     % (issue["number"], who, issue["title"]))

    return collisions, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="paths you intend to change")
    parser.add_argument("--branch", help="use this branch's diff against main "
                                         "as the paths")
    parser.add_argument("--no-fetch", action="store_true",
                        help="skip the fetch; the answer may be stale")
    args = parser.parse_args(argv)

    if not args.no_fetch:
        try:
            run(["git", "fetch", "-q", "origin"])
        except RuntimeError as failure:
            print("could not fetch, so this cannot answer the question: %s"
                  % failure, file=sys.stderr)
            return 2

    query = list(args.paths)
    if args.branch:
        query += branch_files(args.branch)
    if not query:
        parser.error("name the paths you are about to change, or pass --branch")

    if not run(["which", "gh"], check=False).strip():
        print("gh is not on PATH, so open pull requests cannot be read",
              file=sys.stderr)
        return 2

    collisions, lines = report(query)
    if not collisions:
        print("nothing else in flight touches: %s" % ", ".join(query))
        return 0
    print("%d overlap%s with work already in flight:"
          % (collisions, "" if collisions == 1 else "s"))
    for line in lines:
        print("  " + line)
    print("\nTalk to the owner, or pick paths nobody else is holding. "
          "docs/agent-task-guide.md section 8 has the lanes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
