# Plan — session of 2026-09-02

Executes `docs/2026-09-02-council-ruling.md` §3 in order. Each row names the
seat, the property protected, the proof, and the lane. A row is done when its
candidate PR is open with a run log, not when it is merged.

## 0. Unblock

| Step | Owner | Proof |
|---|---|---|
| Merge #544 (`npm audit fix`, lockfile only) | owner arms merge | `dependency-audit` green on the next PR run |
| Merge #540, #541 | owner arms merge | prod watch green on the deploy |

## 1. Build queue — measured against the tracker on 2026-09-04

Another session opened the ruled work while this one was down: 40 PRs open, none merged since 2026-09-02, `main` unchanged at `89b42fb`. Duplicates were stood down the moment they were found. What remains unique here is listed second.

**Stood down (existing PR owns it; this side reports gaps only):**

| Ruling item | Existing PR(s) | This side's branch (kept for the record, not pushed) |
|---|---|---|
| 2 beta slice | #553 → #571 → #569 (hard order) | candidate/beta-slice-careagents |
| 5 D10 populate bound | #562 → #576/#578/#584/#594 | candidate/d10-populate-bound |
| 6 clinical honesty | #563 (#542, #436), #555 (#458) | candidate/clinical-honesty-542-436-458 |
| P MCP phase 1 | #556, #560 (spec amendments) | candidate/mcp-phase1-prm |
| P B2 agent_runs audit | #595 | — |
| D https discovery | #583 (`x_proto` only — the better shape) | dropped |
| 8 #219 probe | #573 (probe), #580 (build) | — (this side's probe was refused by the classifier) |

**Still unique here (committed locally, not pushed):**

| # | Branch | Property protected | Proof |
|---|---|---|---|
| D | candidate/medent-refresh-endpoint | a refresh reaches the endpoint that answers; no credential in error output | live run against the expired cache: clean 400, no secret printed |
| D | candidate/demo-e2e-gates-execute | the pre-deploy gates for PHI redaction and cross-tenant isolation execute | 10/13 with skips → 16/16 |
| D | candidate/legacy-db-adoption (building) | a pre-v1.8.0 database migrates instead of crashing | test builds the 11-table legacy DB; head reached |
| P | candidate/safe-harbor-identifiers (building) | identifiers are removed, not truncated; SECURITY.md stops overclaiming | `***` pins flipped; Grade A held |
| P | candidate/f5-live-resources, then a5-raw-tenant-reads (building) | deleted rows stay deleted; every read tenant-scoped | ratchet pins move in the same commit |
| — | candidate/intent-and-plan | this document | — |

**Three facts from the other session, relayed for the owner (each verified or verifiable):**
1. Nothing has merged; #541 (the Cohort 1 gate) is reviewed, green, unmerged.
2. The auto-merge workflow arms on approval and fires on the *next* push, reopen or ready-for-review — often someone else's. An approval is a deferred merge.
3. #608: `claude-standards-review` reports SUCCESS on every PR because `ANTHROPIC_API_KEY` is unset and the step skips. No PR carries `claude:approve`. Do not read that green as a review.

## 2. QA with the owner's data — run log (2026-09-02, Mac mini)

Every number carries the command that produced it (STOP #3).

**careagents.cloud, the stranger's first screens, unauthenticated.**
`for p in / /auth /home /chat /brief /api/connections/catalog /api/trust /healthz; do curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}
" https://careagents.cloud$p; done`
→ `/` 200 · `/auth` 200 · `/home` `/chat` `/brief` 302 → `/auth` · catalog 401 · trust 200 · healthz 200.
`curl -s https://careagents.cloud/auth | grep -oiE 'href="[^"]*(terms|privacy)[^"]*"'`
→ both point at `healthclawguardrails-production.up.railway.app` — **#534 reproduced live; PR #540 is not deployed.**
`curl -s https://careagents.cloud/ | grep -ci beta` → 0. No beta banner yet (queue item 2).
Sign-in by email code: **not run** — the maintainer's agent was refused the POST carrying the owner's email. Needs the owner to paste the code or allow the call.

**Telegram personas, the same dispatcher the OpenClaw bots exec, tenant `ev-personal`.**
`getMe` on all five bot tokens → default, sally, mary, dom, kristy all answer.
`~/.healthclaw/venv/bin/python3 ~/.healthclaw/commands.py <cmd>` for health, summary, conditions, labs, meds, allergies, immunizations, vitals, token
→ 9/9 answer (rc=0). health: flask 200, gateway 200, mcp up, redis up. summary: 63 conditions, 202 observations, 15 medications, 1 allergy.
Leak check on every command's output: `grep -ciE "<owner surname>|<owner given name>|<owner DOB>|<area code>-|@gmail|MRN|[0-9]{3}-[0-9]{2}-[0-9]{4}"` → **0 hits on 6/6 clinical commands.**

**HealthEx → apply_redaction** (earlier this session): the owner's real record shapes (name, DOB, phone, email, address, SSN, MRN, a clinical note) → zero leaks.

**#264 premise, measured.** `curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}" https://careagents-production.up.railway.app/` → 200 (also `/auth` 200, `/home` 302 to the same Railway host, `/healthz` 200). The Railway hostname does **not** 308 today. Passed to the CareAgents seat; the redirect is built, not pinned.

**#522, measured.** `curl -s "https://dns.google/resolve?name=mcp.healthclaw.io&type=A"` → 216.150.1.193, 216.150.16.193. `curl -sI https://mcp.healthclaw.io/mcp` → 404, `server: Vercel`, `x-vercel-error: DEPLOYMENT_NOT_FOUND`. Dangling, as the issue says. Owner-only: repoint to Railway before MCP phase 1 deploys.

**OpenClaw gateway (Mac mini).** `[default]` Telegram account restart loop; cause in `/tmp/openclaw/openclaw-2026-09-02.log`: "telegram account default routing has no explicit owner". Fix script written; the maintainer's agent was refused the config edit and restart. Owner runs it.

**Not measured:** sign-in → connect → ask → approve on a phone (needs the email code); the Telegram bots end to end (ruling D6: do not service the bot for the beta).
## 2a. Coordination (2026-09-04)

Other sessions on the owner's account that overlap this queue, and what each was told:

| Session | Overlap | Sent |
|---|---|---|
| OpenClaw setup on the Mac mini (cloud, one-way) | `[default]` Telegram binding fix | root cause, the guarded script path, "apply only if you hold the permission" |
| Email verification (remote) | careagents.cloud sign-in as the owner | replied: it is a different codebase and, correctly, that a peer may not carry out a step this session was refused. The sign-in stays with the owner. |
| Recruit testers (remote) | Cohort 1 invitations | do not send until #538 deploys; D17 shape; no "connect your records" copy |
| Dispatch (remote) | unknown | the list of branches and areas this session holds |

Nobody on this side pushes or merges. Specialists build in `.claude/worktrees/`; the maintainer's session reviews, then the owner arms merges.

**Refused by the maintainer-session permission classifier (not retried, not delegated):** the careagents.cloud sign-in POST carrying the owner's email; the OpenClaw config write + gateway restart; the #219 probe (10 concurrent curls, read as load generation). Each is one owner action or one allow-rule.

**HealthEx:** `update_records` requested 2026-09-03 ~02:15 UTC; `check_records_status` → `lastUpdated 2026-09-03T02:33:37Z`. The owner's records are current.

**Edge-case register (new):** the persona `token` command prints a 300-second step-up token into the chat transcript by design. A Telegram chat is not a secret store. Not in the ruling; recorded, not filed. Peer tip for #540 verification: read the `age` header before trusting a deploy — an edge cache can serve the old sign-in page after the build is READY.

## 3. Review against the vision, before each build starts

Ask the four architecture-review questions and write the answers in the PR:
serves the vision or adjacent; honest failure mode and who notices; what it
makes harder later; how it is proven, with what data, run by whom.

## 4. Not this session

#219 probe (needs 10 concurrent testers), #520 and #215 (after Cohort 1
week 1), MCP phase 2, Cohort 2 preconditions P1–P7 (owner-only).

## 5. Ratchet report (run `uv run pytest tests/test_ratchets.py -q`)

2026-09-02 start: 13/13 pins hold. Update at session close.
