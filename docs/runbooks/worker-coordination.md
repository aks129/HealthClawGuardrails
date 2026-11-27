# Coordinating Claude Code and Codex

Use [Fable and Astra sync, issue #676](https://github.com/aks129/HealthClawGuardrails/issues/676)
as the shared dispatch and handoff log. This protocol is cooperative: separate
worktrees isolate files, but do not prevent overlapping changes. Both workers
must acknowledge ownership before this becomes effective. GitHub comments are
not atomic locks; the existing owner resolves competing claims.

## Start a task

1. Read the latest sync comments, the task issue, and local agent instructions.
   Name an owner, exact file/directory scope, excluded files, base SHA, branch,
   and intended result in a claim comment. Include shared tests and generated
   files in the scope. A PR number alone is not a file claim.
2. Check `git worktree list --porcelain` or run
   `python3 scripts/worktree_inventory.py`. Check open PRs touching that scope.
   If ownership is unclear or overlaps, request the current owner's explicit
   acknowledgment; continue independent work while waiting. Silence does not
   transfer ownership.
3. Keep the founder checkout on clean `main`. Create one worktree per worker
   and task, from freshly fetched `origin/main`. Codex uses `codex/<task>`;
   Claude keeps its existing branch convention. Never borrow, reset, or switch
   another worker's checkout, even when that worker appears idle.
4. Allocate a separate local port, database, output directory, and writable
   dependency environment. Record them in the local handoff, without secrets.
   Reading an existing environment is different from installing into it.
   A git worktree does not isolate databases, containers, ports, or credentials.

Example (substitute a new task name and an unused directory):

```sh
git fetch origin
git worktree add ../healthclaw-codex-task -b codex/task origin/main
```

Do not create another PR on top of an unmerged task branch. If a dependency is
unmerged, hold the dependent task locally and record its dependency explicitly.
Only the human merges and deploys. Do not enable auto-merge.

## Keep the other worker current

Post a short update on #676 at task start, scope changes, before handing off,
and after review or integration changes. Read new comments before beginning
another edit batch and before commit/push. Update the task's existing handoff
file with a UTC checkpoint. This is checkpoint synchronization, not a live
notification service; neither tool automatically injects GitHub updates into the
other worker's context.

Each update must contain:

- Owner, state (`claimed`, `working`, `ready-for-review`, `held`, `released`).
- Task/PR link, branch, base SHA, exact current commit SHA.
- Owned scope and excluded scope; whether uncommitted changes remain.
- What changed, test commands/results, and evidence limits.
- Dependencies, decisions needed, next action and named next owner.
- Receiving owner's acknowledgment when ownership changes.

Keep machine paths and runtime resource reservations in local notes. Shared
GitHub and committed handoffs contain no credentials, patient data, or private
tenant identifiers. A commit SHA is the review target; a branch name can move.
Never call a test result current after code changes without explaining the gap.

## Receive a handoff

Read the diff at the recorded SHA in your own worktree. Confirm the source
owner has stopped editing that scope and acknowledge receipt on #676. Report
any local modifications separately; a clean committed handoff is preferred.
Review does not grant permission to amend the author's branch. Return findings
or agree on a dedicated follow-up branch before editing.

After the human merges a task, fetch and examine the actual merge result.
Rebase only your own clean branch, inspect the diff and run relevant checks
before pushing. Never automate a rebase-and-force-push chain. Read the merge
queue runbook before any integration work.

## Inventory and cleanup

The inventory command is read-only and prints paths, branches, SHAs, lock state,
and tracked/untracked change counts. It cannot identify which agent is alive,
assign ownership, or decide a branch is disposable. Do not publish its machine
paths as a GitHub comment.

Never remove a worktree based solely on age, a detached HEAD, or a merged-looking
branch. Obtain the owner's release, preserve uncommitted/untracked work, check
held dependencies and locks, then let the human approve removal. No automatic
pruning or stale-claim takeover is part of this protocol.

## Initial responsibility split

Until the workers agree otherwise on #676:

- Claude/Fable retains the in-flight authorization, OAuth/MCP connector,
  kernel, action-rail, and merge-queue work described in the project handoff.
- Codex/Astra owns the explicit-patient quickstart documentation and this
  coordination proposal. Product evaluation and synthetic evidence remain
  separate from runtime authorization changes.
- Decisions #655 and #648 remain with the human owner.

This split is a proposed operating record, not evidence that Claude has read or
accepted the protocol. Record that acknowledgment on the shared issue.
