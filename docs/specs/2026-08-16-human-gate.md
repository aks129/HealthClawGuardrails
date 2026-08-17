# Spec — the human gate

> Step 3 (architecture review) of `docs/2026-08-16-delivery-process.md`, for
> feature set 4 (action rail). Owner: `.claude/agents/owner-action-rail.md`.
> PRD: `docs/prd/04-action-rail.md` — this document is the §6 item recorded
> there as *"Missing, and a SOW item: a written specification of the human
> gate itself."*
>
> **Design only. No production code, no deploy, no merge.** Everything below
> is a contract to be built against, plus the test that decides whether it
> was built.

## 0. What this document settles

Three things had no written definition, so there was no test for any of them:

1. **What makes a human step count.** `X-Human-Confirmed` and the approval
   endpoint are both called "human confirmation" in our docs. Nobody had
   written down what separates them in principle, so "is this gate real"
   was a matter of taste. §2 makes it one sentence and one test.
2. **What the Tier-2 approve surface is** (#215). The gates are real and
   pinned; nothing behind them can be approved by a human, because no
   surface exists for a human to approve on. §4.
3. **Whether the reserved allowlist and daily-cap codes get enforcement or
   get deleted** (#216). §6 gives a split verdict, not a hedge.

It also corrects a piece of prior research that has gone stale (§5), and
names the closure path for #214 (§7).

---

## 1. The four architecture-review questions

### Q1. Does this serve the vision, or is it adjacent work that feels productive?

It is the vision's hardest case, and `docs/2026-08-16-hard-truths.md` already
says so:

> Reading under constraint is the easy half; acting under constraint is the
> claim nobody else is making.

The product thesis is an enforcement layer that lets an agent be useful on
real health records **without being trusted**. Every read guardrail answers
"what may this agent see". The human gate is the only control that answers
"what may this agent *do*", and it is the one a partner will ask about first,
because the failure mode is a phone call placed to a real pharmacy rather
than a record shown to the wrong person.

The specific work here is not adjacent because of §6 of the hard truths: the
gates are demonstrated and the room behind them is not built. Every demo of
the gate implies a thing that does not exist. Building the approve surface is
what makes the existing demo honest, and it is, per #215, *the piece entirely
in our control* — it needs no vendor, no onboarding, and no credential we do
not already hold.

### Q2. What is the honest failure mode, and who notices it first?

The failure mode is not the gate being removed. It is **the gate quietly
becoming agent-reachable while still looking like a gate**. Four concrete
shapes, in the order they are likely:

| Shape | How it happens | Symptom |
|---|---|---|
| Capability URL | the approve link is built to carry a token so it can be pasted into a chat message | anyone holding the link approves; the agent generated the link |
| Mint-secret reach | `INTERNAL_TOKEN_MINT_SECRET` becomes readable by a process that also runs model output, or is returned in a response body | the agent mints its own approval credential |
| Session sharing | the approve surface is added to a service that already holds an agent-facing session or API credential | one credential set does both halves |
| Summary drift | the approve page renders a model-written summary rather than the payload that executes | the human approves text A; bytes B are sent |

**Who notices first, today: nobody.** There is no assertion anywhere that the
gate is unreachable from agent credentials. `tests/test_write_guard_matrix.py`
pins that `/confirm` refuses a *missing* step-up token (401) and that
`/approval-token` refuses a missing internal secret (403). Neither asks the
question this document is about, which is whether the *full* set of
credentials an agent legitimately holds can reach execution by any route.

That gap is the entire argument for §8's closure test. The first three shapes
above are all caught by it; the fourth is caught by the byte-equality
assertion in §8.3 step 9.

### Q3. What does it make harder later?

Stated plainly, because these are real costs and two of them will be
proposed as "improvements" later:

- **Headless and server-to-server deployments need their own surface.**
  Binding approval to an authenticated human session means an integration
  with no browser cannot approve. That is the intended consequence, not an
  oversight. Such a deployment needs a separately designed surface with its
  own attestation story; it does not get to reuse the agent's credential.
- **Batch approval becomes deliberately awkward.** Single-use, action-bound,
  TTL-limited credentials mean "approve all 12 refills" is twelve
  credentials and twelve consent records. Any future batch feature must
  approve N actions with N recorded consents, not one consent scoped to a
  set. A batch token is a capability token by another name.
- **`declined` is a schema change.** §4.4 adds a terminal state; the state
  column is already sized (`String(32)`) and `schema_sync` widens at boot,
  but the transition map, the ledger readers, and the write-guard matrix all
  move together.
- **Retiring `X-Human-Confirmed` moves a pinned matrix.** See §7 — this is
  the one place standing order 4 ("never edit a pin to go green") has a
  legitimate exception, and it needs to be written into the implementing PR
  rather than discovered by whoever trips it.

### Q4. How will we prove it works, with what data, run by whom?

§8, in full. In one line: two independent contexts, the agent's refusals
asserted **before** the human acts, the human acting in a browser session the
agent's context provably does not hold, and the bytes that execute asserted
equal to the bytes the human saw — all of it synthetic, executed against the
`webhook-poster` rail so that a real execution happens and no real-world side
effect does.

---

## 2. What "out-of-band" means

### 2.1 The criterion

> **An approval is out-of-band if and only if no combination of credentials
> the agent legitimately holds can produce it.**

Everything else in this section is that sentence made checkable. The test in
§8.2 is that sentence made executable.

`X-Human-Confirmed` fails in one clause: the header is a string the caller
writes about itself, so the agent's own credential set produces it. It is not
a weak human step; it is not a human step. #214 is correctly filed as a
security issue rather than an enhancement.

### 2.2 The six properties

A human step counts when all six hold. They are numbered so a review can cite
one.

**H1 — Separate request.** The human's act lands on a different request than
the agent's. Nothing in the request the agent controls advances the action
past `awaiting_confirmation`.

**H2 — Separate principal.** The approving identity is authenticated on its
own credential, not asserted by the agent. A header, a body field, or a JWT
claim the agent can set is the agent's assertion about a human, which is the
`X-Human-Confirmed` shape regardless of what the field is called.

**H3 — Unmintable by the caller.** The artifact that unlocks execution cannot
be produced by anyone holding only the agent's credentials. In our rail this
is `INTERNAL_TOKEN_MINT_SECRET` on
`POST /r6/actions/<id>/approval-token` (`r6/actions/routes.py:630`), checked
with `hmac.compare_digest` and refusing 403 — and refusing it for public
tenants too, which is the clause that matters.

**H4 — Bound and spent.** The approval authorizes exactly one action, once,
inside a window. Our rail: `audience=action-approval`, `operation=<action_id>`,
`consume_nonce=True` on the `require_grant` call at
`r6/actions/routes.py:509`, plus the guarded claim transition
`awaiting_confirmation → executing` which is the single-winner mutex.
Audience and operation binding closed #211; the nonce is what stops replay.

**H5 — Shown, verbatim.** The human saw the bytes that will execute — not a
summary, not a paraphrase, not a model-authored restatement. If the payload
can change between what was displayed and what is sent, H5 does not hold.

**H6 — Recorded independently.** A durable consent record exists that does
not depend on the agent's word: who, when, via which channel. Our rail:
`ActionConfirmation` (`r6/actions/confirmations.py`) plus an AuditEvent whose
`detail` stays PHI-free.

### 2.3 The in-band tells

A step is in-band, and therefore worthless as evidence, if any of these is
true. Each is a real pattern, not a hypothetical:

- The confirmation is a **request field** — header, body key, query param,
  claim. (`X-Human-Confirmed`.)
- The credential is **reachable from the agent's credential**: any token the
  agent can obtain, or a mint endpoint the agent's token opens.
- The link **is** the credential — a URL containing a token, so possession of
  the URL is approval. The agent generated the URL and can therefore approve
  by fetching it.
- The approval happens in the **same request** as the proposal, however many
  internal steps separate them.
- The human sees **prose the agent wrote** rather than the payload.
- The only record of the approval is a **field the agent set**.

### 2.4 External validation

The current MCP specification (2026-07-28) independently arrives at three of
these as MUST-level requirements for out-of-band interactions. They are worth
quoting because they turn §2.2 from our taste into a standard's requirements:

- *"MUST NOT provide a URL which is pre-authenticated to access a protected
  resource, as the URL could be used to impersonate the user by a malicious
  client."* — this is H2 and the third in-band tell. The approve link carries
  no credential; the session authenticates.
- *"the server MUST ensure that the user who started the elicitation request
  … is the same user who completes the authorization flow"* — this is H2's
  binding half, and the spec's phishing scenario (Alice generates the link,
  tricks Bob into completing it, tokens bind to Alice) is precisely why an
  ownership check at the approve surface is load-bearing rather than tidy.
- *"Servers for which a given `requestState` must be consumed at most once …
  MUST enforce that invariant server-side."* — this is H4, and our guarded
  claim transition is the server-side enforcement it demands.

Sources: `https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation`
and `.../basic/patterns/mrtr`, both read 2026-08-16.

---

## 3. What is already true, measured

Read from the code on `main` at `4cb3771`, not from the docs describing it.

| Stage | Route | Credential | Result |
|---|---|---|---|
| propose | `POST /r6/actions/propose` (`routes.py:318`) | tenant header only | 201, `proposed` |
| commit | `POST /r6/actions/<id>/commit` (`routes.py:369`) | write step-up | **202, `awaiting_confirmation`** — nothing executes |
| mint | `POST /r6/actions/<id>/approval-token` (`routes.py:630`) | `X-Internal-Secret` | 403 without it, for public tenants too |
| approve | `POST /r6/actions/<id>/confirm` (`routes.py:462`) | audience + operation + nonce | the only place an executor runs |

Against §2.2 the engine side already satisfies H1, H3, H4 and H6. The four
gaps are:

- **H2 has no surface.** The mint secret is held by CareAgents, which does
  have human sessions — but only the form-fill review relay
  (`careagents/app.py`, `/review/<agent_id>/<action_id>/submit`) ever calls
  it. Phone-call, SMS and webhook-poster actions have no page at all. This is
  #215, and it is why the commit response's *"approve in your HealthClaw
  dashboard"* names a dashboard that does not exist.
- **H5 is unspecified.** No surface renders the verbatim payload for a
  non-form-fill action, and nothing states that the payload is immutable
  after commit. See §9, R2.
- **Decline is not representable.** `_TRANSITIONS` in
  `r6/actions/models.py` gives `awaiting_confirmation → {executing, expired}`.
  A human who reads the script and says no produces the same record as a
  human who never opened the page.
- **Nothing tests the criterion itself.** §2.1 has no assertion anywhere in
  the 3,151-test suite. §8.2.

---

## 4. The Tier-2 approve surface (#215)

### 4.1 Where it lives, and why that is the design

**On CareAgents, not on the engine.** The engine authenticates *tenants* and
*agents*; it has no concept of a logged-in person. CareAgents authenticates
*people*. Putting the approve page on the engine would mean building human
sessions into the service that also answers agent credentials, which is the
"session sharing" failure shape from Q2.

The invariant is not process isolation — it is narrower and testable:

> The mint secret and the minted token are reachable only from a code path
> behind `@login_required` plus an ownership check, and neither ever appears
> in a response body, a log line, or model context.

`careagents/healthclaw.py:343` `confirm_action` already implements exactly
this: mint with `X-Internal-Secret` server-side, use the token immediately,
never return it. The approve surface reuses that method unchanged.

### 4.2 Routes

Mirroring the existing review relay, which is the shape that already works:

```
GET  /approve/<agent_id>/<action_id>      @login_required
POST /approve/<agent_id>/<action_id>      @login_required   (Approve)
POST /approve/<agent_id>/<action_id>/decline   @login_required
```

`GET` renders the approval card. Both `POST`s change state and are CSRF-
protected; neither is reachable by a `GET`, so a link cannot approve.

The URL carries **no credential**. `agent_id` and `action_id` are
identifiers, not secrets: possession of the URL grants nothing, and the
session is what authenticates. This is the MCP MUST from §2.4 and the third
in-band tell from §2.3.

Every route resolves ownership through `_agent_owns_action(agent_id,
action_id)`, which binds session → account → agent → tenant → action. A
logged-in person who does not own the agent gets 404 — not 403, which would
confirm the action exists.

### 4.3 What the card shows

The card is the H5 artifact, so its contents are a contract, not a design
preference:

| Element | Rule |
|---|---|
| Action kind | plain words: "Phone call", "Text message", "Form submission" |
| Destination | the **full** destination, not the label — the actual number for a call, the actual endpoint for a webhook. `summary()` shows `to` (a label); the card shows what `to_dict()` holds, because a label is what a mis-dial looks like. |
| The payload, verbatim | the exact `payload.body` that will be sent, in a monospace block, not truncated and not summarized. If it does not fit, it scrolls. |
| Provenance | for each fact drawn from records, where it came from — the pattern `r6/actions/review.py` already uses ("from your records") |
| Expiry | the approval window as a wall-clock time, and the fact that it lapses |
| Two outcomes | **Approve** and **Decline**, both explicit, neither a default |

No element of the card is authored by a model. Every string is either a
template constant or a field read from the action row.

The card is PHI-bearing by necessity — the verbatim script for a pharmacy
call contains medication names. That is fine on the page, which sits behind
the session, and is forbidden everywhere else: audit `detail`, Telegram
pushes, and any recording all use `summary()`. §8.4.

### 4.4 What it records, and the `declined` state

**Approve** — unchanged from today's confirm path: guarded claim, then
`ActionConfirmation(approved_via=...)` issued and consumed in one
transaction, then the AuditEvent, then the executor. Add `'approve-page'` to
`APPROVED_VIA_VALUES` so the consent record names the surface that produced
it rather than borrowing `'dashboard'`.

**Decline** — new, and needed. Proposed changes:

```
_TRANSITIONS['awaiting_confirmation'] = {'executing', 'declined', 'expired'}
_TRANSITIONS['declined'] = set()            # terminal
```

A decline writes an `ActionEvent` (actor `approve-page`), an
`ActionConfirmation`-shaped refusal record with who/when/via, and a PHI-free
AuditEvent. It is terminal: a declined action is re-proposed, never revived.

Three outcomes, and they map exactly onto MCP's three-action elicitation
model (§5), which is a useful coincidence rather than a driver:

| Human does | Rail state | MCP action |
|---|---|---|
| taps Approve | `executing` → resolved | `accept` |
| taps Decline | `declined` | `decline` |
| closes the page, window lapses | `expired` | `cancel` |

### 4.5 How the proposing agent cannot drive it

Five independent barriers. The design intent is that no single mistake opens
the gate:

1. **Session, not token.** The approve routes accept a session cookie. The
   agent holds tenant and step-up credentials, which these routes do not
   accept.
2. **Ownership binding.** `_agent_owns_action` ties the session's account to
   the agent that owns the action. A different logged-in account gets 404.
3. **Mint secret is server-side only.** `INTERNAL_TOKEN_MINT_SECRET` lives in
   CareAgents' environment. It is never placed in a response, a template, a
   log line, or a prompt.
4. **The minted token is used and discarded in the same server call.**
   `confirm_action` mints, POSTs, and returns the engine's answer. The token
   does not survive the request, so there is nothing to capture.
5. **The engine's own binding.** Even a leaked token is audience-bound,
   operation-bound and single-use, so it approves one action once.

Barriers 3 through 5 exist today. Barriers 1 and 2 exist for form-fill only.
Building #215 is extending 1 and 2 to the other kinds.

---

## 5. MCP URL-mode elicitation — the prior research has moved

**This is the section to read if you read one.** The note recorded 2026-08-05
identified SEP-1036 URL-mode elicitation with error `-32042` as the
standardized replacement for the `X-Human-Confirmed` shape. I checked the
current specification rather than trusting the note, as instructed. The
conclusion splits:

**What still holds.** URL-mode elicitation is in the current specification
(2026-07-28) and is still the standardized shape for exactly our problem: an
interaction that must happen out of band, in the user's browser, on a domain
the user trusts, without the sensitive part passing through the MCP client.
Servers **MUST** use URL mode rather than form mode for anything sensitive.

**What has changed, and it is not cosmetic.** Three specifics from the
2026-08-05 note were removed in 2026-07-28:

| Item | Status now |
|---|---|
| `-32042` `URLElicitationRequiredError` | **Forbidden.** The spec's error-code table says: *"Implementations of this protocol version MUST NOT emit these codes: … `-32042` — URL elicitation required (2025-11-25 only)."* |
| `elicitationId` | Removed from URL-mode requests |
| `notifications/elicitation/complete` | Removed |

The replacement is **Multi Round-Trip Requests (MRTR)**, introduced in
2026-07-28 and mandatory: *"Servers MUST send server-to-client requests (such
as `roots/list`, `sampling/createMessage`, or `elicitation/create`) using the
MRTR pattern. The previous pattern of server-initiated requests is no longer
supported. This is a breaking change."*

Under MRTR the server returns a **result**, not an error:

```json
{
  "resultType": "input_required",
  "inputRequests": {
    "approve_call": {
      "method": "elicitation/create",
      "params": {
        "mode": "url",
        "url": "https://<careagents-host>/approve/<agent_id>/<action_id>",
        "message": "Approve the pharmacy call before it is placed."
      }
    }
  },
  "requestState": "<integrity-protected opaque blob>"
}
```

The client opens the URL with the user's consent, replies `{"action":
"accept"}`, and **retries the original tool call** carrying `requestState`.
The server learns the outcome from its own state on the retry. There is no
completion notification and no correlation id on the wire.

### 5.1 How it maps onto our rail

The useful finding: **the rail is already MRTR-shaped, and needs no engine
change to be wrapped.**

| MRTR | Our rail today |
|---|---|
| `InputRequiredResult` / `resultType: "input_required"` | commit's **202 `awaiting_confirmation`** with `next_step` naming the out-of-band step |
| url-mode `elicitation/create` | the approve URL from §4.2 |
| `requestState` (opaque, integrity-protected, principal-bound, TTL, single-use enforced server-side) | the action id plus the engine's own state: tenant-scoped row, `expires_at`, and the guarded claim as the single-use enforcement |
| client retries the original request | `GET /r6/actions/<id>` polling, which commit's response already instructs |
| `accept` / `decline` / `cancel` | §4.4's `executing` / `declined` / `expired` |

Two requirements the MCP wrapper must meet, recorded here so set 6 does not
have to rediscover them:

- `requestState` **MUST** be integrity-protected (HMAC or AEAD) and treated
  as attacker-controlled, and **SHOULD** carry the authenticated principal, a
  short TTL, and an identifier for the originating request. If the wrapper
  encodes nothing but the action id, integrity protection is still required
  the moment that id influences authorization — which it does.
- The `url` **MUST NOT** be pre-authenticated. §4.2 already forbids this; the
  MCP wrapper must not "helpfully" append a token to make the link work
  without a login.

**Scope:** the MCP wrapper is feature set 6 (surfaces), not this set. This
document defines the contract it wraps; it does not build it. If set 6 finds
the mapping wrong, that is a finding against this document.

---

## 6. #216 — allowlist and daily cap. A split verdict

Both codes exist in `r6/actions/errors.py` and, per #216, are referenced
nowhere else. A reserved control that does nothing is the retro's defect
shape: a control that looks like one thing and quietly does another — here,
looks like enforcement and is a constant. It cannot stay as it is. The two
codes have different answers.

### 6.1 `DAILY_CAP_REACHED` — designable now, build it

**What is counted: claims, not proposals.** The cap counts transitions into
`executing` per tenant, per kind, per calendar day (UTC). Counting proposals
would let an agent exhaust a patient's cap without a single human tap, which
converts a safety control into a denial-of-service lever.

**Where it is checked: twice, with one authority.**

- At **propose**, advisory: refuse 429 with `DAILY_CAP_REACHED` if the cap is
  already spent. This exists so the agent is told early and no human is asked
  to tap Approve on something that cannot run.
- At **confirm**, authoritative: re-check immediately before the claim, in
  the same guarded UPDATE if it can be expressed as a predicate, otherwise
  immediately before it. Time passes between propose and approve; the
  propose-time check is stale by construction.

Writing down which check is authoritative is the part that keeps this from
becoming the retro's defect shape a second time. The propose-time check is a
courtesy and must be documented as one; a reviewer who deletes it should
break no guarantee.

`ActionEvent` already records every transition with `to_status`, so the count
is a query against the existing ledger and needs no new table.

**Default:** configurable per tenant, defaulting to a small number for
phone-call and sms. The number is a product decision, not an architecture
one; the mechanism is what this document fixes.

### 6.2 `CONTACT_NOT_ALLOWLISTED` — the prerequisite does not exist

The acceptance bar in spec v3 is "allowlisted referenced contact". Enforcing
that requires a store of contacts **the account holder saved**, tenant-keyed,
attested by a human on an authenticated surface. I checked: no such store
exists in `r6/` or `careagents/`. There is no table, no model, and no page.

An allowlist derived from anything else fails on inspection:

- Numbers found in FHIR records are not human-attested. A pharmacy number
  that arrived in a feed has had no human confirm it is the right pharmacy,
  and the whole point of the control is human attestation.
- Numbers seen in previous actions are circular: the first call to a wrong
  number allowlists it.

**Recommendation, for the owner's decision:** remove `CONTACT_NOT_ALLOWLISTED`
from `errors.py` and `errors.ALL` until the contact store is funded, and file
the store as a SOW item. The argument is standing: a reserved code that
nothing can emit reads to a reviewer, an auditor and a partner as a control
that exists. `DAILY_CAP_REACHED` stays because §6.1 builds it in the same
change.

The store, when it is funded, is roughly: tenant-keyed saved contacts with a
label, a destination, and an attestation timestamp; created only on the
authenticated surface; matched at propose and re-checked at confirm on the
same authoritative/advisory split as §6.1.

**This is not a unilateral deletion.** Removing a code from `errors.ALL`
changes an API contract surface. It goes in the implementing PR with this
section cited, and the owner may rule the other way — in which case the
honest alternative is a comment in `errors.py` saying the code is reserved
and unenforced, with the issue number, so nobody reads it as a control.

---

## 7. #214 — the closure path for `X-Human-Confirmed`

`enforce_human_in_loop` (`r6/health_compliance.py`) runs in `before_request`
and gates POST/PUT of clinical resource types on the caller-supplied
`X-Human-Confirmed` header. By §2.1 this is not a human step.

**The path, in order:**

1. **Documents first.** Four documents currently claim the header is gone:
   `CLAUDE.md`, `.github/REVIEW_STANDARDS.md` item 4, `docs/development.md`,
   `SECURITY.md`. The stale REVIEW_STANDARDS text is what the AI PR reviewer
   enforces on every PR, so it is actively teaching the wrong invariant. This
   is a documentation change with no behaviour change and can land alone.
2. **Route clinical writes through the rail.** A clinical create or update
   becomes a proposed action of a new kind (`fhir-write`), committed for
   approval and executed by the confirm path like any other. The write
   handler's direct path stays only for non-clinical resources.
3. **Retire the header.** `enforce_human_in_loop` stops reading
   `X-Human-Confirmed` and refuses clinical writes outright, directing the
   caller to the rail.

**The trap, written down so the implementer does not trip standing order 4.**
Step 3 changes the pinned matrix. Today a bare clinical write answers **428**
because `enforce_human_in_loop` runs ahead of every handler's auth gate; the
four-row matrix (neither gate / confirmed-without-credential /
credential-without-human / both) is pinned by
`tests/test_aidbox_example_tells_the_truth.py` as 428 / 401 / 428 / 201, and
the walkthrough that asserted 401 for row one shipped wrong once already.

Retiring the header makes rows two and four unreachable — there is no
"confirmed" state to be in. **That pin must be updated as part of the
designed change, in the same PR, with a comment saying the change is
deliberate and citing #214.** It is not a pin edited to go green; it is a pin
whose subject was removed. The distinction is the comment, and the agent
guide's strict-xfail trap (§6) is the precedent: a pin that goes red when you
*fix* what it pins is the expected shape, not an obstacle.

**Scope:** #214's step 2 is a follow-on PR against this design, not this
document's build. Step 1 is a documentation fix anyone may take.

---

## 8. How we prove it works

The PRD's bar: *"a recording in which execution is blocked, a human acts
somewhere the agent cannot reach, and only then does it run."* This section
makes each clause an assertion.

### 8.1 Data and providers

**Synthetic and sandbox only, by construction rather than by discipline.**

- Tenant: a per-run synthetic id, seeded with synthetic records.
- Recipient: a synthetic label and a destination owned by the test.
- Kind under test: **`webhook-poster`**. It is a real registered executor
  that makes a real outbound HTTP POST, configured by `WEBHOOK_POSTER_URL`
  and `WEBHOOK_POSTER_TOKEN`, and it fails loud with
  `PROVIDER_NOT_CONFIGURED` rather than simulating success. Pointing it at a
  sink inside the test process means **the execution is genuine and the
  side effect is not real-world**. No pharmacy, no carrier, no dial.

This is deliberate. Proving the human gate does not require proving Bland or
Twilio, and an action rail proven against a real pharmacy is an incident. The
phone-call and SMS legs are separately unproven; §9 says so rather than
letting the webhook proof imply them.

### 8.2 The agent-credential closure test — the assertion that decides §2.1

This is the test that did not exist, and it is the one worth the most.

**Definition.** Let `C_agent` be the complete credential set an agent holds
in the normal flow: `X-Tenant-Id`, `X-Agent-Id`, a write-scoped step-up token
as minted for commit, plus full knowledge of the action id and payload.

**Claim.** For every route the application registers, no request
constructible from `C_agent` moves an action out of `awaiting_confirmation`.

**Mechanization**, and step 1 is the part that keeps this from becoming a
guard written narrower than its property:

1. **Enumerate from the app, not from a list.** Read `app.url_map` for every
   rule whose endpoint begins with `actions.`, plus the FHIR write routes.
   **Assert the enumerated set equals the set the test exercises.** A route
   added later fails this test rather than being silently excluded. Hard
   truths §5 is the reason: `test_no_image_is_floating` matched one word on
   one registry and certified the gap.
2. **Assert the positive first.** `GET /r6/actions/<id>` returns 200 and
   `status == "awaiting_confirmation"`. The action exists and is in the
   pre-execution state. Without this, every refusal below is also true of an
   action that was never created.
3. For each enumerated route and method, issue the request with `C_agent`
   only, and assert the refusal. Named expectations, not "not 200":
   `POST /<id>/confirm` → 401 (audience/operation/nonce unmet);
   `POST /<id>/approval-token` → 403 (internal secret unmet);
   `POST /<id>/commit` → 409 (already `awaiting_confirmation`).
4. **Assert state, not just status codes.** After the whole sweep, re-assert
   `status == "awaiting_confirmation"` and that the `ActionEvent` ledger
   contains no transition to `executing`. A 500 is a refusal too, and not one
   we want to pass on.

This test is cheap, has no browser, and belongs in the suite. It is the thing
that would notice Q2's first three failure shapes.

### 8.3 The two-context recording

Playwright, following `examples/aidbox-healthclaw-guardrails/qa/demo.spec.ts`:
the test makes the real calls and renders each result into the page as it
lands, so **a video showing a pass cannot exist unless the pass happened.**

Two contexts, created independently:

- **Context A — the agent.** `request.newContext()`, API only, no browser
  storage state. Holds `C_agent`.
- **Context B — the human.** `browser.newContext()` with its own CareAgents
  login. Shares nothing with A.

Steps, each asserted and rendered:

1. A proposes. Assert 201, and that the body carries an id and
   `status == "proposed"`.
2. A commits with the step-up token. Assert **202** and
   `status == "awaiting_confirmation"`. Nothing has executed.
3. **A tries to execute, before any human acts.** The §8.2 closure sweep runs
   here. Every refusal is asserted now — asserting refusals after the human
   has approved proves nothing.
4. **Assert A cannot reach the human's channel.** Dump
   `contextA.storageState()` and assert it holds no cookie for the CareAgents
   origin. This is "somewhere the agent cannot reach", made mechanical rather
   than narrated.
5. B navigates to `/approve/<agent_id>/<action_id>`. Assert the page renders
   the destination and the verbatim body — assert the text is **present**,
   positively, before asserting anything about what is absent.
6. Capture the payload bytes as rendered on B's page.
7. B clicks Approve.
8. A polls `GET /r6/actions/<id>`. Assert it reaches `completed`.
9. **Assert the sink received exactly one POST, and that its body is
   byte-equal to what B saw in step 6.** This is H5 made testable: the bytes
   the human approved are the bytes that went out. Exactly one, because a
   double-send is the failure the single-winner claim exists to prevent.
10. Assert the ledger: `proposed → awaiting_confirmation` (actor
    `commit-route`), `awaiting_confirmation → executing` (actor `confirm`),
    and one `ActionConfirmation` row with the approving channel and
    timestamp.

**A second recording for Decline**, once §4.4 lands: same setup, B declines,
assert `declined`, assert the sink received **zero** POSTs, and assert the
refusal record exists.

### 8.4 PHI in the artifacts

The approve card carries PHI by necessity (§4.3). Therefore:

- The run is **synthetic end to end**, so the recording contains no real
  record. This is the one set where that is fully honest, because the PRD
  scopes it to synthetic data and sandbox providers.
- The recording is still captured with the guardrailed view, and the run log
  prints `summary()` shapes only.
- Assert it rather than assume it: every AuditEvent written during the run is
  checked to contain only `summary()` keys — `id`, `kind`, `to`, `status`,
  `expires_at` — and no `payload`.

### 8.5 Run by whom

| Artifact | Who | What makes it real |
|---|---|---|
| Run log | me, as set owner | `scripts/human_gate_walkthrough.sh`, modelled on `examples/aidbox-healthclaw-guardrails/scripts/walkthrough.sh`: it asserts, fails loudly, and names which guarantee broke |
| Recording | the Playwright spec above | produced **by** the run that asserts |
| Edge-case register | me, extended by QA | §9, with issue numbers |
| QA sign-off | QA, adversarially | §8.6 |
| End-user sign-off | per PRD, a clinician on the output; for the approve card, a person who is not us reads it and says what they thought they were approving | the card either communicated or it did not |

The end-user sign-off on the card is worth its own sentence: the question is
not "did it work" but "did you know what you were agreeing to". A card that
passes every assertion and leaves the person unsure what they approved has
failed at the thing it exists for.

### 8.6 The adversarial pass — what QA should try

Named so the pass is not improvised:

- Replay a consumed approval token on the same action, and on a different one.
- Approve the same action twice from two browser tabs, concurrently. Assert
  exactly one execution.
- Approve from a second logged-in account that does not own the agent. Expect
  404.
- Let the window lapse, then approve. Expect 410 and no execution.
- Attempt to mint an approval token with every credential in `C_agent`.
- Request the approve URL with no session. Expect a login redirect, and
  assert that following it does not approve.
- Tamper with the payload between commit and approve by every route
  available, then check step 9's byte equality still holds — or, if no route
  can, record that the immutability is structural (§9, R2).

---

## 9. Risks and known defects

Recorded with the evidence I have. Per the task, defects noticed are written
here rather than fixed.

**R1 — Decline is not representable.** `_TRANSITIONS` in
`r6/actions/models.py` gives `awaiting_confirmation → {executing, expired}`.
A human refusal and a human who never looked produce the same record today.
Evidence: read directly from the transition map. §4.4 designs the fix. No
issue number yet; this document is the first place it is written down.

**R2 — Payload immutability after commit is a requirement, and I did not
verify it is pinned.** H5 depends on the payload not changing between the
approve card rendering and the executor reading it. `transition_action`
refuses `status` in `**fields` but does not restrict `payload_json`, and I
did not find a test asserting the payload is immutable after
`awaiting_confirmation`. I did **not** audit every write path to the column —
that is the hunt this task excludes. Treated as an unverified requirement:
the implementing PR states it and pins it, and §8.6's last item probes it.

**R3 — `CONTACT_NOT_ALLOWLISTED` and `DAILY_CAP_REACHED` are reserved and
enforced nowhere** (#216). §6. Until §6.1 lands, an agent can propose a call
to any number, any number of times, and the human tap is the only defence.

**R4 — `X-Human-Confirmed` still gates direct clinical FHIR writes** (#214),
and four documents state the opposite. §7.

**R5 — The phone and SMS legs are unproven.** The vendor sandbox onboarding
(Bland, Twilio) is an external blocker outside this set. The §8 proof
demonstrates the *gate*, not those executors. Anyone citing the recording as
evidence that a pharmacy call works is over-reading it, which is why §9 says
so in the same document as the recording.

**R6 — Two soft-delete defects remain open under #509.** The form-fill half
that rendered a deleted record into a submitted form was fixed in `b265cfa`
(#517) and the `is_deleted=False` filter with its explanatory comment is
present at `r6/actions/rails/form_fill.py`. The issue is broader than that
one site and remains open; I did not survey the rest of it.

**R7 — This document has not been executed.** It is a design. Nothing in §8
has been run, and no claim in §3 comes from running the system — §3 is read
from code at `4cb3771`. The four artifacts do not exist yet; this specifies
what would make them.

---

## 10. What this does not do

The scope boundary, named so a later reader does not mistake silence for
coverage:

- **It does not build anything.** No production code, no schema migration,
  no deploy. Every "add", "route" and "remove" above is a proposal for a
  follow-on PR.
- **It does not build the MCP wrapper.** URL-mode elicitation over MRTR is
  feature set 6 (surfaces). §5 defines the contract; set 6 implements it, and
  a mismatch is a finding against §5.
- **It does not close #214.** §7 names the path in three steps. Step 2 is a
  separate PR with its own review.
- **It does not design the contact store.** §6.2 states the prerequisite and
  recommends a deletion in the meantime. The store is a SOW item.
- **It does not cover the Telegram surface.** `r6/telegram_push.py` is
  push-only. An approve action from Telegram is a second surface with its own
  identity question — a Telegram user id is not an authenticated account —
  and it is out of scope here.
- **It does not address vendor onboarding.** Bland and Twilio sandbox access
  is an external blocker (R5).
- **It does not touch #248, #255, or #217** (durable runs, resumption, purge
  orphans). They live in this set and are unaffected by this design.
- **It does not speak to non-clinical FHIR writes.** §7 changes the clinical
  path only.
