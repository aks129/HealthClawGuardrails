# Prioritized backlog, 2026-08-05

Ranked from the architecture audit, tonight's live shakeout, and the defect
pattern that produced six of this week's bugs. Sequenced into three sprints
to Aug 18. Every row names the property it protects and how a fix is proven.

Companion to `docs/2026-08-04-wrapup-review-and-plan.md` (§7 is the E-series
task queue, still valid) and `docs/2026-08-02-retro.md`.

## 1. The pattern that ranks these

Six defects this week share one shape, and it is not "a bug in redaction" or
"a bug in a lookup". It is:

> A guardrail produced **nothing**, and the caller read that as **an answer**.

- #365, #369 — the leak direction. Something got out that should not have.
- #376, #379 — the hole direction. Nothing came back and the hole was
  narrated to a patient as a fact about their clinic.
- #381 — the same hole, on a clinical recommendation rather than a name.

Both directions live at one boundary: `apply_redaction` strips, then
`label_codings` refills, and the caller can see the outcome of neither. The
codebase already solves this once, correctly, in two places:

- `r6/access.py` — `require_grant` returns a `Grant` or raises. Its docstring
  names the hazard: a clean 401 from an unexpected raise "reads to a client
  exactly like a working guard. That is the retro's defect shape with an HTTP
  status on it."
- `r6/labs/interpret.py` — `_indeterminate()` carries five distinct reasons
  rather than letting "could not judge" look like "normal".

Everything ranked P0 below is that pattern with a patient on the other end.

## 2. What makes a fix count

Three rules, learned expensively this week:

1. **Measure, do not read.** #282's list of eight unredacted sites came from
   reading code. Reading tells you a call is absent; only a probe tells you
   whether anything reaches a caller. Two of the eight were clean; one was
   leaking into a patient's chat.
2. **Assert the positive too.** Every guardrail check in the repo asserted a
   bad string was *absent*. A broken labeller passes those **harder** — it
   shrinks the response. The RxNorm lookup returned `None` for its entire
   life while conformance held Grade A.
3. **Mutation or it did not happen.** Every fix states the mutation that
   reddens its test. A test that cannot fail for the reason it claims is
   decoration.

## 3. P0 — patient is told something false, or unprotected data moves

| # | Issue | Property at risk | Proof required |
|---|---|---|---|
| 1 | #381 brief renders a crashed care-gaps engine as "no screenings due" | Never infer absence. A missed screening recommendation reads as good news | A forced engine failure renders "unavailable", not an empty section |
| 2 | #382 `r6/brief/` reads unredacted, on neither probe list | PHI at a boundary delivered outside the FHIR read path | Marker probe first, then redact-then-relabel, both halves asserted |
| 3 | #374 a 502 after `claim_next` commits strands a run | The chat hangs 60s with nothing on the stream; every deploy opens the window | `run.lease_expired` count and `attempt > 1` count, before and after |
| 4 | #214 `X-Human-Confirmed` is the entire human gate for direct clinical writes | The clinical write gate is a header a caller sets | A write without the action rail's approval cannot execute |
| 5 | #305 `/fasten/demo` writes with no authentication | Unauthenticated write path in production | 401/404 in production, write-guard matrix row updated same PR |

## 4. P1 — the product lies quietly, or falls over

| # | Issue | Why here |
|---|---|---|
| 6 | #310 dashboard fabricates an import animation on a real connection | A guardrails showcase asserting a completed audit trail before the import. Owner-gated on "is it in the demo" |
| 7 | #219 thread saturation causes a restart loop that wipes all chats | Availability, and it destroys patient conversations |
| 8 | #221 careagents Postgres engine lacks `pool_pre_ping` | Intermittent 500s after idle — one line, high return |
| 9 | #220 two paths report success when they failed | Same family as the P0 block, on the write side |
| 10 | #262 `/api/auth/email` reports `sent: true` when the cooldown sent nothing | The front door, reporting a send that did not happen |
| 11 | #213 Grade A is earnable by a deployment with the guardrails off | The badge is the product claim; it must not be earnable dishonestly |
| 12 | #380 CREATE echoes the caller's upstream display unredacted | Decide and write it down, or fix. An unwritten exception is the problem |

## 5. P2 — structural, and cheap now / expensive later

| # | Issue | Why here |
|---|---|---|
| 13 | #212 single-use nonce falls back to a per-process dict without Redis | Replay protection that silently degrades under scale-out |
| 14 | #281 `R6Resource.id` is String(255) with no length check | SQLite and Postgres diverge silently — this repo has shipped three such bugs |
| 15 | #375 the engine's access log eats retention | Evidence for every other defect. An hour-old report has none |
| 16 | #341 worker claim poll (design merged, PR #373) | Idle backoff, 11x fewer requests, zero `r6/` files |
| 17 | #378 scorecard cannot discover tenants | The tool the plan depends on measured the wrong tenant three times |
| 18 | #294 untested HealthClaw transport-failure wrap | The seam every CareAgents call crosses |
| 19 | #377 `MedicationStatement` silently dropped at ingest | Confident answer over a hole, at the source |
| 20 | #232 Postgres CI lane is a hand-curated allowlist that already leaked | The lane that catches the divergence class in #281 |

## 6. Sprint plan

Each sprint: planning, execution, QA verification, retro. Nothing merges
without a mutation check and an owner merge.

**Sprint 1 — "nothing is not an answer" (P0 rows 1-3, plus 8 and 10).**
The two brief defects and the stranded run, because all three are live and
patient-visible. #221 and #262 ride along: both are small, both are the same
honesty family, and neither touches the same files.

**Sprint 2 — the gates (P0 rows 4-5, P1 rows 6, 9, 11).**
`X-Human-Confirmed`, `/fasten/demo`, the dashboard animation, the
report-success-on-failure paths, and the conformance grade. This sprint needs
owner rulings first: #310 in-demo, #334 token strip.

**Sprint 3 — structure and evidence (P2).**
Backoff, retention, the CI lane, the discovery mode, the transport-failure
tests. Lower patient risk, and it is what makes the next month's defects
findable.

## 7. Out of scope before Aug 18, deliberately

Named so nobody re-opens the question mid-sprint: the `ingest_entries()`
extraction (#293's follow-on), self-hosting the terminology server (#352),
the comms rail epic (#161), care circles (#249), and the MCP App directory
submission (#164). Each is real; none changes what a stranger sees on the
18th.
