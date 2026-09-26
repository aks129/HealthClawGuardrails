# QA sign-off standard

Every change to HealthClaw or CareAgents is signed off against this page
before it merges. It says what the product must achieve, what evidence a
change needs, and who signs.

## 1. The product vision, as checks

A change must not move any of these backwards. A feature is done when it
moves one of them forward and a tester has seen it happen.

| # | Vision | How a tester checks it |
|---|---|---|
| V1 | A person's health data is private by default. | Output carries no name, contact detail or upstream label. Canary names never appear. |
| V2 | Every look at a record is written down, or the look fails. | Each read and write leaves an audit row. A forced audit failure blocks the read. |
| V3 | The AI suggests; a person approves. | No form, call, text or write happens without a separate human approval step. |
| V4 | A non-technical person succeeds on a phone, unaided. | The journey finishes at 375 pixels wide, with no jargon, dead ends or duplicate rows. |
| V5 | The product only claims what is true. | Every claim on screen, in docs or on the site matches behaviour on `main`. |
| V6 | Each person sees only their own records. | Another account's ids, tenants and connections are refused everywhere. |

## 2. Gates every change passes

| Gate | Evidence |
|---|---|
| G1 Spec | The change does what its spec or issue says, and nothing extra. |
| G2 Suite | Full `uv run pytest -q` and `uv run ruff check .` pass, with counts quoted. |
| G3 Tests are honest | Each new test fails without the change. Mutation evidence for any guard or check. |
| G4 Real run | The behaviour is shown against a running app, not only fakes. |
| G5 Guardrails | Conformance stays Grade A. No non-negotiable in `CLAUDE.md` is weakened. |
| G6 Vision | The change is checked against every row in section 1 it touches. |
| G7 Copy | Patient-facing words are plain, and the prose check passes. |
| G8 Deploy | After release, `scripts/prod_watch.py` passes against production. |

A gate that cannot be run is reported as not run, with the reason. It is
never reported as passed.

## 3. Who signs

| Change touches | Signs off |
|---|---|
| Anything | QA verifier: gates G1 to G5 |
| A screen or words a person reads | Patient tester: V4 and G7 at phone width |
| Redaction, audit, auth, tenancy, writes, secrets | Security tester: V1, V2, V3 and V6 |
| Architecture, security design, new dependencies | CTO review before build |
| Scope, journeys, value | Product review of the spec |

A single FAIL from any signer blocks the merge. Findings are fixed and
checked again by the same signer.

## 4. The verdict

Each signer reports:

- **PASS** or **FAIL** first.
- For each finding: severity, `file:line`, the input that breaks it, and a
  command that reproduces it.
- For a PASS: what was exercised, and what was not covered.

## 5. Continuous checks

QA does not stop at merge. A scheduled sweep runs against production and
files an issue for anything that fails:

- `scripts/prod_watch.py`: every service answers, on the released commit.
- The public demo MCP server: connects, lists tools, and refuses a write.
- The CareAgents new-account journey on sample records, at phone width.
- Canary scan: no synthetic canary name appears in any public response.

## 6. Feature acceptance for the stable beta

| Feature | Accepted when |
|---|---|
| Sign in | Passkey or email code works on a phone. A wrong code gives a plain message. |
| Sample records | One tap gives one sample and opens chat. A second tap opens the same one. |
| Real records | An invited account connects through Fasten or a file. Records land in its own tenant. |
| Chat | Answers cite the record and its date. Missing data is named, never guessed. |
| Approvals | A prepared form waits for approval. Nothing is sent before Approve. The approval is audited. |
| Connector menu | Each source works or says "Coming soon" in one line. |
| Settings | Passkeys, surfaces, shared apps and account deletion work. |
| Delete my account | Every tenant and conversation is purged. The person can sign up again. |
