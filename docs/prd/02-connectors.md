# PRD 2 — Upstream connectors

> Owner brief (local only — `.claude/` is gitignored): `.claude/agents/owner-connectors.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

A guardrail layer that only works in front of one vendor's FHIR server is a product nobody can adopt. The user is **an integrator who already has a FHIR server** — Aidbox, Medplum, HAPI, or something else — and wants the guardrails without moving their data.

## 2. What "works" means

> Each connector kind runs its own live walkthrough end to end, against a real server of that kind.

Not a mock, and not one kind standing in for another. A connector proven only against a mock is a connector nobody has used.

## 3. How it is proven

- **Run log** — the six-step walkthrough per kind.
- **Recording** — `examples/aidbox-healthclaw-guardrails/qa/` drives the live stack and renders each result as it lands.
- **Register** — a four-row table saying which kinds are proven live and which are not. That table is the artifact.
- **Sign-offs** — QA adversarial; end-user is the vendor's own developer relations (an Aidbox example PR is already upstream).

## 4. Current state, measured

| Kind | Proven live | Against |
|---|---|---|
| `hapi` | **yes** | HAPI FHIR 8.11.16 |
| `generic` | **yes** | Firely Server 6.9.1 |
| `aidbox` | **no** | server down 2026-08-16 |
| `medplum` | **no** | server down; no credentials present |

- Pack: `docs/evidence/2026-08-16-set2-connectors.md` — **EVIDENCE PARTIAL**, 2 of 4.
- Four defects fixed today off this run:
  - `hapi` dropping its credentials (#512)
  - health lying about upstream mode (#513)
  - `$conformance` colliding on a shared server (#514)
  - an unknown kind booting then 500-ing (#518)
- The example's Aidbox image is **unpinned** (`:edge`, `pull_policy: always`) with a dated exemption recorded in `tests/test_aidbox_example_tells_the_truth.py`.

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 140 | wearables connector: Apple Health via Open Wearables | milestone: aug18 |
| 229 | Open Wearables OAuth blocked upstream — six providers plus Apple Health are dark | external blocker |
| 141 | wire sleep-session / nap ingestion | follow-on to #140 |
| 326 | CLEAR identity verification fails in TEFCA mode | external, Fasten |
| 461 | Fasten `widget.config_error` is console-only, so support gets no request id | diagnosability |
| 377 | `MedicationStatement` is not a supported ingest type | silent drop |
| 352 | self-host the terminology server | ruling Q2, deferred |
| 94 | local search ignores FHIR strict handling and the self link is untruthful | contract |

## 6. Specifications

- `docs/upstream-connectors.md` — the registry and how to add a row.
- `docs/recipes/healthclaw-in-front-of-aidbox.md` and `…-medplum.md`
- `docs/runbooks/medplum-self-host-qa.md`
- **Missing, and a SOW item:** a written connector conformance checklist a *vendor* could run themselves. Today the only runnable definition is our walkthrough script.
