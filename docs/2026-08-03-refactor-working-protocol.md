# Refactor working protocol — 2026-08-03

**Founder decision (Eugene, 2026-08-03):** the architecture refactor proceeds
now. The CTO and Product recommendation to defer all structural work past
Aug 18 is **overruled on priority**. Their *design* objections stand and are
being resolved before any code is written (see
`docs/2026-08-03-access-kernel-spec.md`).

The constraint, in the founder's words:

> Do it carefully and intentionally, not rushed and break-fix — that defeats
> the whole purpose. The goal: stop the bleeding and the constant issues,
> align on quality, simple implementation, and scope for vision impact.

This document is the protocol that constraint implies. It governs every PR in
the refactor program.

## Why this refactor exists (the bleeding)

Six defects in one week shared one shape (`docs/2026-08-02-retro.md`): a
control that looks like one thing and quietly does two. The audit found why
that keeps happening — the four guarantees are **per-route conventions, not
enforced invariants**. In `r6/routes.py` alone: 32 tenant reads with four
defaulting strategies, 41 audit calls with two transaction semantics, 11
redaction calls across four profiles with no selector, 7 step-up gates
answering 401 at four sites and 403 at three.

The write-guard matrix (`tests/test_write_guard_matrix.py`) then measured the
cost across all 29 write paths and found two live cross-tenant
vulnerabilities the audit itself had missed (#311, #312). That is the
bleeding: every new write path re-decides every guard, and some of them
decide wrong.

The kernel's job is to make those decisions unavailable to re-make.

## The protocol

These rules exist because a refactor that breaks the product is worse than
the debt it removes.

1. **Pins before moves.** No code moves until the behavior it owns is pinned
   at the HTTP boundary. `tests/test_write_guard_matrix.py` (PR #313) is the
   characterization baseline and **must merge first**. It is the instrument
   the whole migration is measured against.
2. **The kernel lands as pure addition.** The first implementation PR adds
   `r6/access.py` and its tests and is adopted by *nothing*. Zero call sites
   change. It cannot break the product because nothing calls it yet.
3. **One guard, one blueprint, one PR.** After that, each PR migrates a
   single guard across a single blueprint. Small enough to review in one
   sitting and revert in one command.
4. **A behavior change is a failure, not a merge conflict.** If the guard
   matrix, the conformance grade, or the suite changes at any step, the step
   is wrong — revert and re-scope. Do not "fix forward" by adjusting the test
   to match the new behavior. That is the exact move this protocol exists to
   forbid.
5. **Never mix a move with a fix.** A PR either relocates behavior or changes
   it, never both. Security fixes ship on their own branches (as #308, #309,
   #314, #315 did).
6. **Grade A at every step.** `tests/test_guardrail_conformance.py` is
   refactor-invariant by construction — it asserts only wire-observable
   facts. If it moves, something real broke.
7. **Every step is independently revertable.** No PR may depend on a later
   one to be correct. If the program stops after any given merge, `main` is
   in a coherent state.
8. **Deleted code is deleted, not orphaned.** When a call site migrates to
   the kernel, the old path goes with it in the same PR. Two ways to do one
   thing is how the four tenant-defaulting strategies happened.

## What "scope for vision impact" rules out

The audit proposed five workstreams. Adopting all of them at once is the
rushed version. In scope now:

- **The access kernel** — the one piece that structurally prevents the defect
  class that is actually costing us (cross-tenant scoping, missing audit,
  status-dialect drift, unguarded clinical writes).

Deliberately still deferred, with reasons:

- **The `r6/routes.py` package split** — pure motion. It buys locality, not
  correctness, and the test suite is coupled to the module path at 17 patch
  sites plus 34 private-symbol imports. It should follow the kernel, not
  precede it.
- **`tools.ts`** — carve out the two data-only fixes (#S-6 manifest schema,
  S-8 the unconditional `X-Human-Confirmed` injection); the rewrite waits.
- **The ingest engine and the CareAgents boundary** — both are real, both are
  post-kernel. The S-4a log fix (#309) already took the urgent half.

## Definition of done for the program

Not "the kernel exists." The program is done when the recurring defect
classes are structurally unavailable:

- One tenant reader, and a write handler cannot see a tenant the grant did
  not authorize (kills the #304/#311 class).
- One step-up gate with one status policy (kills the four dialects).
- Audit that cannot be silently skipped, including on the read-miss paths
  (kills the five unaudited 404s).
- A response path where applying no redaction requires naming the endpoint on
  a security allowlist (kills the silent-unredacted-exit class).
- Clinical-write human confirmation reachable from every blueprint, not just
  `r6_blueprint` (kills the `/r6/smbp/reading` gap).

Each of those maps to a matrix row that must flip from `xfail` to `pass`
without the test being edited.
