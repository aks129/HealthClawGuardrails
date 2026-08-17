# PRD 4 — Action rail

> Owner brief: `.claude/agents/owner-action-rail.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

The moment an agent can *do* something — refill a prescription, book an appointment, send a form — the guardrail question stops being about reading and starts being about consent. The user is **the patient whose name is on the action** and the clinician who receives it.

## 2. What "works" means

> Nothing executes without an out-of-band human step — demonstrated, not asserted.

Demonstrated means: a recording in which execution is blocked, a human acts somewhere the agent cannot reach, and only then does it run. If the human step happens inside the same request the agent controls, nothing has been demonstrated.

## 3. How it is proven

- **Run log** — propose → commit → approve → execute, with the refusal at each stage the human has not reached yet.
- **Recording** — the out-of-band step, visibly out of band.
- **Register** — every path that reaches execution, and which gate each one crosses.
- **Sign-offs** — QA adversarial (tried to execute without the human); end-user is a clinician receiving the output.

**Synthetic data and sandbox providers only.** An action rail proven against a real pharmacy is an incident, not evidence.

## 4. Current state, measured

- **Unmeasured end to end.** No pack exists.
- The separate approval endpoint is the real mechanism. The `X-Human-Confirmed` header on direct FHIR writes is a **known gap** (#214) — a header a caller sets about itself.
- Gate ordering is verified: `enforce_human_in_loop` runs in `before_request`, so a bare clinical write answers **428, not 401**. The full four-row matrix is pinned by `tests/test_aidbox_example_tells_the_truth.py`.
- **#215 says no Tier-2 approve surface exists**, so nothing could execute even with vendor keys. That is the honest state of this set: the gates are real and the thing behind them is not built.

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 215 | no Tier-2 approve surface exists — nothing could execute even with vendor keys | **the set's headline** |
| 214 | `X-Human-Confirmed` is the whole gate on direct clinical writes | known gap |
| 216 | allowlist + daily cap error codes are reserved but enforced nowhere | control that does nothing |
| 161 | EPIC: comms rail — phone + SMS executors behind the human gate | epic |
| 162 | route medication refill requests through the rail | product |
| 163 | appointment prep + booking through the rail | product |
| 160 | advisor escalation: hand off to the human gate instead of dead-ending | journey |
| 86 | fax-delivery executor for completed intake PDFs | help wanted |
| 248 | persist AgentRun and ToolCall events; move turns off request threads | architecture |
| 255 | resume human-waiting runs and expose PHI-safe operations | architecture |
| 217 | purge leaves ActionEvent and ActionConfirmation orphaned | data lifecycle |
| 413 | `$curatr-apply-fix` is unreachable in production | dead path |
| 95 | make `action_policy.yaml` authoritative before exposing a describe contract | policy drift |
| 485 | should propose-stage accept an Observation with no `effective[x]`? | decision |
| 61 | SMBP phase 2: reminder scheduler + cuff photo OCR | enhancement |

## 6. Specifications

- `action_policy.yaml` — the policy file, **not yet authoritative** (#95).
- `docs/2026-08-03-refactor-working-protocol.md` for per-PR rules.
- **Missing, and a SOW item:** a written specification of the human gate itself — what counts as out-of-band, what a Tier-2 approval surface must do, and what the closure path for #214 is. Prior research points at MCP URL-mode elicitation (SEP-1036) as the standardized replacement; that has never been written down as our design.
