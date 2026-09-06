# Merging the queue, one change at a time

Written 2026-09-05 against the sixty-three open pull requests of that day.
The order and the known stops are specific to that queue; the method is not.

## Why one at a time

Branch protection on `main` is strict: a branch must be up to date with
`main` before it can merge. Every merge therefore puts every other open
branch behind, and each one must be brought forward and re-checked before
its turn. Arming auto-merge across the queue does not help. GitHub does not
update branches on its own, and a stacked change runs no tests at all (#585),
so it cannot go red and would merge unverified.

## What "with care" means here

For each change, in order:

1. Confirm it still carries the standards-review verdict label.
2. Bring the branch up to date with `main` (`gh pr update-branch`).
3. Wait for every check to finish; stop on any red.
4. Squash-merge with branch deletion, so any change stacked on it retargets
   to `main` by itself.

The standards review has not run on any open change: the API-key secret is
unset, so the job skips and reports green (#608). With the secret set, the
update-branch push in step 2 runs it, and step 3 waits for it. Merging
without it is a decision, not a default; the script below makes it one.

## Measured before merging anything

All sixty were merged in the order below into a throwaway branch off
`89b42fb`. Fifty-five merged clean; five collided with an earlier change in
the same order. On the result, `ruff` was clean and the suite reported 3561
passed, 2 failed. Both failures are sums of changes that pass alone:

| What goes red | Where | Why | Fix |
|---|---|---|---|
| `test_the_god_module_only_shrinks` | at #583, once #541 is in | #541 adds three lines to `r6/routes.py`, #583 one; 3928 against a pin of 3927 | raise the pin in #583 with that reason, or move the line |
| landing-page test count | at #555 and after | the page said 2812; the guard's floor passes 2812 at that merge and ends at 2877 | #641, merged right after #544 |

The five collisions, each resolved by merging `main` into the branch after
the partner lands, running the suite, and pushing:

| Change | Collides with | File |
|---|---|---|
| #598 | #579 | `careagents/static/home.js` |
| #580 | #549 | `tests/test_build_marker_stamp.py` |
| #595 | #545, #562 | `tests/test_access_kernel.py` |
| #603 | #601, #609 | `docs/evidence/2026-08-16-set2-connectors.md` |
| #622 | #549 | `scripts/prod_watch.py` |

## The order

1. #544, the lockfile fix that turns `dependency-audit` green everywhere.
   Then close #551 and #552, which are byte-identical subsets of it.
2. #641, so the landing-page count does not go red mid-queue.
3. #607 (stops the auto-merge comment on every push) and #588 (CI on
   stacked changes).
4. The changes that touch no product code: #543 #560 #593 #605 #609 #610
   #613 #627 #628 #629.
5. Tests only: #631 #632 #636 #637 #639 #545 #546.
6. The chains, base first: #550 #566 #579; #553 #571 #598 #569 #621; #562
   #576 #578 #584 #594; #563 #619.
7. The rest: #540 #541 #547 #549 #555 #556 #561 #573 #580 #583 #595 #597
   #599 #601 #603 #611 #612 #614 #615 #618 #622 #623.
8. #582, the remaining dependabot change (its packages are not in #544;
   comment `@dependabot rebase` if it is dirty).
9. #642, then #643 last so the new reviewer does not fire on every
   update-branch of the queue.

## Stacked changes after their base squash-merges

When a base merges by squash and its branch is deleted, GitHub retargets
the change stacked on it to `main`, and that change is then dirty: its
branch still carries the base's original commits, which `main` holds only
as one squashed commit, so a merge conflicts on every region the base
touched. Do not resolve that merge by hand. Replay only the stacked
change's own commits:

```zsh
FORK=$(git merge-base <last commit of the base branch> origin/<stacked branch>)
git rebase --onto origin/main "$FORK"
git push --force-with-lease origin HEAD:<stacked branch>
```

Measured on #579 (2026-09-05): the merge showed eleven conflict hunks in
three files; the rebase replayed six commits with none.

Never chain the rebase and the push in one command. If the rebase stops
on a conflict, `HEAD` is the base you rebased onto, and a force-push at
that moment replaces the branch with `main`'s tip. GitHub then closes
the pull request as already merged, and it has to be reopened once the
real commits are pushed back. This happened on #598 (2026-09-05).
Rebase, look at `git status`, resolve, run the tests, and only then push. A stacked change
carries no auto-merge, so merge it yourself once its checks pass.

## If auto-merge is already armed

Arming auto-merge on every change does not move the queue by itself: GitHub
does not bring a branch up to date, and a branch that is behind cannot
merge. The steps above still apply; the script only notices that the merge
has already happened when the checks pass and moves on. On 2026-09-05 the
owner armed forty-four changes and merged six by hand; from there the queue
was walked exactly this way, one update-branch at a time.

## The script

Run from the repository root as a maintainer. It walks the order above,
performs the four steps for each change, and stops at the first one that
conflicts or goes red. Re-run to resume; merged changes are skipped.

```zsh
set -u
REQUIRE_REVIEW_LABEL=${REQUIRE_REVIEW_LABEL:-1}
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
ORDER=(
  544 641
  607 588
  543 560 593 605 609 610 613 627 628 629
  631 632 636 637 639 545 546
  550 566 579
  553 571 598 569 621
  562 576 578 584 594
  563 619
  540 541 547 549 555 556 561 573 580 583 595 597 599 601 603 611 612 614 615 618 622 623
  582
  642 643
)
if [ $# -gt 0 ]; then ORDER=("$@"); fi

# The eight contexts branch protection requires. A fresh head has none of
# them for a moment after update-branch, so "no pending checks" is not
# "all checks passed"; wait until every required context is present.
REQUIRED=(claude-standards-review python-tests node-tests postgres-tests lint secret-scan dependency-audit compliance-gates)

wait_checks() {
  # Wait until every required context exists and has a conclusion, then print
  # the names of any that did not succeed. Gives up after 40 minutes.
  local n=$1 i=0
  while true; do
    local json missing pending
    json=$(gh pr checks "$n" --repo "$REPO" --json name,state 2>/dev/null || echo '[]')
    missing=0
    for c in $REQUIRED; do
      printf '%s' "$json" | jq -e --arg c "$c" 'map(select(.name==$c)) | length > 0' >/dev/null || missing=$((missing+1))
    done
    pending=$(printf '%s' "$json" | jq '[.[] | select(.state=="PENDING" or .state=="QUEUED" or .state=="IN_PROGRESS")] | length')
    if [ "$missing" = "0" ] && [ "$pending" = "0" ]; then break; fi
    i=$((i+1)); if [ $i -gt 53 ]; then echo "timeout waiting for checks ($missing missing, $pending pending)"; return; fi
    sleep 45
  done
  printf '%s' "$json" | jq -r --argjson req "$(printf '%s\n' $REQUIRED | jq -R . | jq -s .)" '.[] | select((.name as $x | $req | index($x)) and .state != "SUCCESS") | .name'
}

for n in $ORDER; do
  state=$(gh pr view "$n" --repo "$REPO" --json state --jq .state)
  if [ "$state" != "OPEN" ]; then echo "#$n is $state, skipping"; continue; fi
  labels=$(gh pr view "$n" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")')
  case "$labels" in
    *claude:approve*) ;;
    *) if [ "$REQUIRE_REVIEW_LABEL" = "1" ]; then
         echo "STOP #$n: no claude:approve label (labels: '$labels'). The standards review has not run;"
         echo "  set ANTHROPIC_API_KEY (#608) so it runs on the update-branch push, or REQUIRE_REVIEW_LABEL=0."; exit 1
       else
         echo "   (merging without a standards-review verdict, REQUIRE_REVIEW_LABEL=0)"
       fi ;;
  esac
  if [ -n "$(gh pr view "$n" --repo "$REPO" --json autoMergeRequest --jq '.autoMergeRequest // empty')" ]; then
    echo "   (auto-merge is armed; it will fire when the checks pass, and this script will not merge twice)"
  fi
  # 551 and 552 are byte-identical subsets of 544; close them once 544 is in.
  if [ "$n" = "544" ]; then :; fi
  echo "== #$n: update branch"
  if ! gh pr update-branch "$n" --repo "$REPO" >/dev/null 2>&1; then
    m=$(gh pr view "$n" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus)
    if [ "$m" = "DIRTY" ]; then echo "STOP #$n: merge conflict with main; resolve locally, push, re-run from #$n"; exit 1; fi
    echo "   (already up to date or nothing to do: $m)"
  fi
  sleep 60
  echo "== #$n: waiting for checks"
  failed=$(wait_checks "$n")
  if [ -n "$failed" ]; then echo "STOP #$n: red checks: $failed"; exit 1; fi
  if [ "$REQUIRE_REVIEW_LABEL" = "1" ]; then
    labels=$(gh pr view "$n" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")')
    case "$labels" in *claude:approve*) ;; *) echo "STOP #$n: review verdict after update is '$labels'"; exit 1 ;; esac
  fi
  # An armed auto-merge fires the moment the checks above pass; treat that as done.
  if [ "$(gh pr view "$n" --repo "$REPO" --json state --jq .state)" = "MERGED" ]; then echo "== #$n: merged by auto-merge"; continue; fi
  m=$(gh pr view "$n" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus)
  if [ "$m" = "BEHIND" ]; then echo "STOP #$n: main moved under this PR (BEHIND); re-run from #$n"; exit 1; fi
  echo "== #$n: merge ($m)"
  if ! gh pr merge "$n" --repo "$REPO" --squash --delete-branch; then
    echo "STOP #$n: merge refused: $(gh pr view "$n" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus)"; exit 1
  fi
  if [ "$n" = "544" ]; then
    gh pr close 551 --repo "$REPO" --comment "Subsumed by #544, which carries the identical fast-uri bump." || true
    gh pr close 552 --repo "$REPO" --comment "Subsumed by #544, which carries the identical qs bump." || true
  fi
  if [ "$n" = "582" ]; then echo "(if #582 was DIRTY: comment '@dependabot rebase' and re-run 582)"; fi
done
echo "queue done: $(gh pr list --repo "$REPO" --state open --json number --jq 'length') still open"
```
