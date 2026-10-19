# CareAgents automatic deploy and drift alarm

CareAgents used to ship only by a manual `railway up`, and production drifted
22 commits behind `main`. Two workflows close that gap:

| Workflow | Runs | Does |
|---|---|---|
| `.github/workflows/careagents-deploy.yml` | every push to `main` touching `careagents/**`, `deploy/careagents/**`, `pyproject.toml` or `uv.lock`; also by hand | stages, stamps, uploads web and worker from the same stage, then waits for `/healthz` to report the build |
| `.github/workflows/careagents-drift.yml` | hourly, and by hand | opens one issue when `careagents.cloud` has been behind the newest CareAgents commit on `main` for more than two hours |

The manual procedure in
[careagents-durable-worker.md](careagents-durable-worker.md#stage-the-build)
still works and stays the fallback. The workflow follows it step for step.

## What the deploy does

1. Skips with a notice if the `RAILWAY_TOKEN` secret is not set. Runs only
   from `main`, so a `workflow_dispatch` from a feature branch deploys nothing.
2. Stages the runbook's four files into a fresh directory under
   `$RUNNER_TEMP`: `pyproject.toml`, `uv.lock`, `careagents/`, and
   `deploy/careagents/Dockerfile` as `Dockerfile`. Runs
   `deploy/careagents/stamp_build.sh` last, and refuses a `-dirty` stamp.
3. `cd`s into the stage. Runs `railway status` and stops unless the output names
   `awake-serenity`, because a `railway up` that does not resolve to a project
   creates a new one.
4. Runs `railway up --detach --environment production` for `careagents` and
   then for `careagents-worker`, from the same stage. Uses the pinned Railway
   CLI (`RAILWAY_CLI_VERSION` in the workflow).
5. Polls `https://careagents.cloud/healthz` for up to 15 minutes. It waits until
   `build` equals the stamped sha and `run_workers` is `true`
   (`scripts/careagents_release.py wait`). If that does not happen, the job
   fails.

Runs queue and never cancel each other (`concurrency: careagents-deploy`).
Two merges in quick succession therefore deploy in order, and the second run
ships the newer `main`.

`/healthz` reports the web service's build. The worker's build is not exposed,
and `run_workers: true` only says that some worker is draining. Both uploads
come from one stage, so they carry the same marker. To check the worker
directly, read its deployment in the Railway dashboard.

## Owner setup (once)

1. In Railway, open project **awake-serenity**, then **Settings → Tokens**.
   Create a **project token** for the **production** environment. A project
   token can deploy to that one environment and cannot touch other projects.
   Do not use an account or team token.
2. In GitHub, open the repository, then **Settings → Secrets and variables →
   Actions → New repository secret**. Name it `RAILWAY_TOKEN` and paste the
   token as the value.
3. Open **Actions → careagents-deploy → Run workflow** on `main`. This first run
   checks the one thing that could not be tested before merge: whether
   `railway status` and `railway up` resolve the project from a project token
   alone. A green run means `/healthz` already reports the new build. A run
   that stops at the `awake-serenity` check means the token is for the wrong
   project, or this CLI version prints status differently. Nothing was
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
secret. The workflow then skips with a notice.
