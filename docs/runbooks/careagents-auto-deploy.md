# CareAgents automatic deploy and drift alarm

CareAgents used to ship only by a manual `railway up`, and production drifted
22 commits behind `main`. Two workflows close that gap:

| Workflow | Runs | Does |
|---|---|---|
| `.github/workflows/careagents-deploy.yml` | every push to `main` touching `careagents/**`, `deploy/careagents/**`, `pyproject.toml` or `uv.lock`; also by hand | waits for CI to pass on the commit, stages, stamps, uploads web and worker from the same stage, then waits for `/healthz` to report the build |
| `.github/workflows/careagents-drift.yml` | hourly, and by hand | opens one issue when `careagents.cloud` has been behind the newest CareAgents commit on `main` for more than two hours |

The manual procedure in
[careagents-durable-worker.md](careagents-durable-worker.md#stage-the-build)
still works and stays the fallback. The workflow follows it step for step.

## What the deploy does

1. Runs in the GitHub Environment `careagents-production`. Its deployment
   branch policy admits `main` only, and it holds `RAILWAY_TOKEN` as an
   environment secret. That policy is what stops a feature branch from
   deploying. The workflow file's own `if: github.ref == 'refs/heads/main'`
   is a convenience: a branch can edit its copy of the file, but it cannot
   edit the environment. If the token is missing, the job skips with a notice.
   Only a true/false check of the secret happens before the upload step, and
   only the upload step receives the token itself.
2. Waits for CI on the same commit (`scripts/careagents_release.py ci-gate`).
   `main` does not require a branch to be up to date before it merges, so a
   `main` commit can be a combination of PRs that CI never tested together.
   The gate reads the `CI - FHIR MCP Guardrails` push run for `github.sha`
   with the job's own `GITHUB_TOKEN` (`actions: read`). It continues only if
   `python-tests`, `postgres-tests`, `node-tests`, `lint`, `secret-scan`,
   `dependency-audit` and `compliance-gates` all concluded `success`.
   `playwright-tests` is not required. A failed, skipped or missing required
   job fails the deploy at once. If CI has no verdict after 60 minutes, the
   deploy fails too. Only `main`'s push run counts. If the log repeats
   "permission denied (check actions: read)", the token scope is wrong, not
   CI slow. Either way nothing is uploaded. A manual **Run
   workflow** waits the same way for CI on the `main` tip it runs. After CI
   is fixed or re-run green, choose **Re-run all jobs** on the failed deploy.
3. Downloads the pinned Railway CLI release binary and checks it against a
   pinned sha256 (`RAILWAY_CLI_VERSION`, `RAILWAY_CLI_SHA256`). It does not use
   `npm install`, so no package install scripts run in the job.
4. Stages the runbook's four files into a fresh directory under
   `$RUNNER_TEMP`: `pyproject.toml`, `uv.lock`, `careagents/`, and
   `deploy/careagents/Dockerfile` as `Dockerfile`. Runs
   `deploy/careagents/stamp_build.sh` last, and refuses a `-dirty` stamp.
5. `cd`s into the stage. Runs `railway status --json` and stops unless the
   project `id` is exactly `835e92df-ebaa-4aa1-bfb5-cb1b346d385e`
   (awake-serenity). A `railway up` that does not resolve to a project creates
   a new one. The Actions log is public, so the job prints only the project
   name and never the status output.
6. Runs `railway up --detach --environment production` for `careagents` and
   then for `careagents-worker`, from the same stage.
7. Polls `https://careagents.cloud/healthz` for up to 15 minutes. It waits until
   `build` equals the stamped sha and `run_workers` is `true`
   (`scripts/careagents_release.py wait`). If that does not happen, the job
   fails.

A running deploy is never cancelled (`concurrency: careagents-deploy`). GitHub
keeps at most one run pending behind it, so the newest pending run wins and
any older pending run is cancelled before it starts. After a burst of merges,
production goes from the running deploy straight to the newest `main`. Not
every merge in between gets its own deploy.

`/healthz` reports the web service's build. The worker's build is not exposed,
and `run_workers: true` only says that some worker is draining. Both uploads
come from one stage, so they carry the same marker. To check the worker
directly, read its deployment in the Railway dashboard.

## Owner setup (once)

1. In Railway, open project **awake-serenity**, then **Settings → Tokens**.
   Create a **project token** for the **production** environment. A project
   token can deploy to that one environment and cannot touch other projects.
   Do not use an account or team token.
2. Make sure the repository's default branch is `main` (**Settings → General →
   Default branch**). Scheduled workflows, including the drift alarm, run only
   from the default branch, and **Run workflow** offers that branch first.
3. Create the environment **before** merging. If the workflow names an
   environment that does not exist, GitHub creates it with no protection.
   In GitHub, open **Settings → Environments → New environment** and name it
   `careagents-production`. Under **Deployment branches and tags**, choose
   **Selected branches and tags** and add the rule `main` (type: branch).
   Do not allow any other branch or tag.
4. On the same environment page, under **Environment secrets → Add environment
   secret**, name the secret `RAILWAY_TOKEN` and paste the token. Do not store
   it as a repository secret: any workflow on any branch can read those.
5. Open **Actions → careagents-deploy → Run workflow** on `main`. This first run
   checks the one thing that could not be tested before merge: whether
   `railway status --json` and `railway up` resolve the project from a project
   token alone. A green run means `/healthz` already reports the new build. A
   run that stops at the project-id check means the token is for the wrong
   project, or this CLI version reports status differently. Nothing was
   uploaded in either case.

## Drift alarm

`scripts/careagents_release.py drift` compares `/healthz` `build` with the
newest `main` commit that touches the paths above:

- **Current:** the deployed build is that commit, or a later `main` commit that
  contains it. A deploy from a later tip counts as current.
- **Behind:** the build predates it, is `unknown`, is `-dirty`, or is not a
  commit on `main`. The alarm fires once the CareAgents commit is more than two
  hours old. Inside that window a deploy may still be rolling out.
- **Could not check:** `/healthz` is unreachable. The issue is neither opened
  nor closed. `prod-watch` owns outages.

The issue is titled `careagents-drift: production is behind main`. Each run
edits it in place, and it gets a comment only when the deployed build changes.
It closes when a run sees production current. The `prod-watch` stale-build
alarm (24-hour window, every six hours) can fire for the same cause. The two
alarms overlap but do not conflict.

## Roll back

- **Preferred:** revert the bad commit on `main`. The revert touches CareAgents
  paths, so it deploys through the same workflow and passes the same checks.
  That includes waiting for CI on the revert, usually 10 to 25 minutes.
- **Re-run an earlier deploy:** in **Actions → careagents-deploy**, open the
  last good run and choose **Re-run all jobs**. A re-run checks out that run's
  commit, so it stages, stamps and uploads the older build through the same
  checks, and waits for `/healthz` to report it. It deploys both services from
  one stage. Production then disagrees with `main`, so the drift alarm opens
  after two hours, and the next CareAgents merge deploys `main` again. Revert
  on `main`, or pause the workflow, if the rollback must hold.
- **Faster, for an emergency:** in the Railway dashboard, open each service
  (`careagents` and `careagents-worker`), find the previous deployment, and
  choose **Redeploy**. Do both services. Production then runs an older build,
  so the drift alarm opens after two hours. That is correct, because `main`
  and production disagree. Revert on `main` to settle it. The next CareAgents
  merge deploys `main` again, so pause the workflow first if the dashboard
  rollback must hold.

## Pause

Go to **Actions → careagents-deploy → ⋯ → Disable workflow**. Re-enable it the
same way. While it is disabled, the drift alarm fires after two hours, which
is a reminder that it is off. Disable `careagents-drift` too if the pause is
deliberate and long. To stop deploys completely, delete the `RAILWAY_TOKEN`
secret from the `careagents-production` environment. The workflow then skips
with a notice.
