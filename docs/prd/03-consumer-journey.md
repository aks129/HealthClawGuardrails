# PRD 3 — CareAgents consumer journey

> Owner brief (local only — `.claude/` is gitignored): `.claude/agents/owner-consumer-journey.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

A patient cannot get a straight answer about their own records. The data is scattered across portals, and the tools that promise to help either want everything or deliver nothing. The user is **a patient on a phone**, often inside an in-app browser, usually while something else is going wrong.

## 2. What "works" means

> A person completes sign-up, connects a real record, asks a question, and approves an action — start to finish, without help and without being told what to click.

Not *the endpoints return 200*. The measure is a person finishing, and the only way to know is to watch one try.

## 3. How it is proven

- **Run log** — the journey, step by step, on a phone.
- **Recording** — the journey itself, captured with redaction on. This set's recording is the product demo.
- **Register** — every point a person hesitated or backed out.
- **Sign-offs** — QA adversarial; **end-user is someone who is not us**, on their own phone, without a script. That sign-off is the hardest artifact in the whole programme and the only one that answers the question the set is named after.

## 4. Current state, measured

- **Unmeasured end to end.** No pack exists.
- Deployed build is **stale** (#427) — the only thing keeping that issue open is a manual `railway up`.
- CareAgents stores **no PHI** — accounts and pointers only. PHI-adjacent data, chat transcripts included, belongs in HealthClaw per tenant.
- **CareAgents tests fake the HealthClaw client**, so they prove a call is *made*, not *accepted*. Ids do not transfer between the two systems and the rejection is silent.

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 264 | two live origins, one account store — passkeys break across the DNS cutover | decide before Aug 18 |
| 157 | EPIC: one patient identity across HealthClaw, CareAgents and SHC | epic |
| 250 | unify connect → ask → approve → outcome in one durable timeline | journey coherence |
| 249 | scoped care circles for patient-human-agent collaboration | product |
| 136 | iMessage surface: hub binding + agent routing | milestone: aug18 |
| 137 | shared inbound → agent → reply pipeline | surfaces |
| 158 | advisor router: route a question to the right advisor | quality of answer |
| 159 | shared advisor memory (preferences only, no PHI) | quality of answer |
| 396 | the relayed review page loads HealthClaw nav and CSS on careagents.cloud | broken journey |
| 219 | thread saturation causes a restart loop that wipes all chats | reliability |
| 263 | browser sign-in is coupled to an unguarded log-line format | test fragility |

## 6. Specifications

- `docs/runbooks/careagents-durable-worker.md`
- **Missing, and the biggest SOW item in the programme:** there is no written specification of the consumer journey. Six issues in the table above are journey design decisions being made one at a time. This set needs its SOW and PRD *before* more of them are answered individually.
