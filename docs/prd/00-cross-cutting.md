# Cross-cutting issues — no feature set owns these

They are real and they are not features. Listed so the six PRDs can each
claim to cover their set completely.

| # | Issue |
|---|---|
| 56 | Refactor: carve `r6/routes.py` into the `register_*` module pattern |
| 231 | CI: move the strict dependency audit off the PR critical path |
| 232 | CI: the Postgres lane is a hand-curated allowlist that already leaked |
| 235 | Process: adopt the standing sub-agent roster + research calendar |
| 276 | docker-compose overrides CMD, so the only local runner bypasses role dispatch |
| 414 | Process: main went red because one PR pinned a defect another was fixing |
| 432 | Process: a PR that moves a pin another open PR touches must say so |
| 433 | Unexplained one-off failure in `test_prod_watch_build` — flake, or real |
| 449 | Auto-merge runs on `GITHUB_TOKEN`: main gets no post-merge CI |

**#433 stays open deliberately.** An unexplained flake is a stop condition,
not something to close for tidiness.
