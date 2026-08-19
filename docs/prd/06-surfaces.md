# PRD 6 — Surfaces

> Owner brief (local only — `.claude/` is gitignored): `.claude/agents/owner-surfaces.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

The guardrails are only worth anything where someone can reach them. The user
is **whoever is holding the agent**. That is a developer wiring an MCP client,
a patient in a chat window, or a partner evaluating whether this is real.

## 2. What "works" means

> Every advertised tool answers, and refuses when it should — both halves, on every surface.

The second half is the one that goes unchecked. An unauthenticated call must be refused, and someone must have *made* that call. **Production MCP stays token-locked**; that is not open to a convenience argument.

## 3. How it is proven

- **Run log** — every tool in the manifest, called. Name the number advertised and the number exercised; if they differ, that difference is the register's first entry.
- **Recording** — 401 unauthenticated, full tool list authenticated, in one run.
- **Register** — every surface, and whether it has been exercised at all.
- **Sign-offs** — QA adversarial; end-user is a partner installing it without our help.

## 4. Current state, measured

- **Partially measured.** The Aidbox example asserts MCP 401 unauthenticated and 27 tools authenticated.
- **#290: the locked MCP endpoint is unreachable from hosted connectors** — no OAuth metadata, and the 401 gives no way forward. That is the set's headline: the production surface is correctly locked and effectively unusable by the clients it is for.
- MCP server deploys are manual with **no drift signal** (#155).
- prod-watch monitors the Railway host, not the origin users reach (#289).

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 290 | locked MCP endpoint unreachable from hosted connectors | **the set's headline** |
| 164 | EPIC: distribution — MCP App directory + non-developer install path | epic, milestone: aug18 |
| 155 | MCP-server deploy drift is unobservable | ops |
| 289 | prod_watch monitors the Railway host, not the origin | monitoring blind spot |
| 243 | prod-watch does not cover the signed-in journey | monitoring gap |
| 427 | prod-watch: deployed build is stale | **owner action** |
| 184 | community test drive — run it, tell us what breaks | the QA gate, crowdsourced |
| 57 | split MCP `tools.ts` (1.8k lines) | maintainability |
| 266 | RELEASING.md claims 11 gates; the count is computed and is not 11 | doc drift |

## 6. Specifications

- `services/agent-orchestrator` tool manifest — the advertised contract.
- MCP spec 2025-11-25 stable; the 2026-07-28 RC makes protocol core stateless and deprecates Sampling/Roots/Logging.
- **Missing, and a SOW item:** RFC 9728 `/.well-known/oauth-protected-resource` plus audience-validated tokens is the smallest fix for #290 that keeps the existing issuer. Prior research identified it; it has never been written up as our design.
