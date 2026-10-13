# One assistant per person

Status: draft for owner review, 2026-10-13. Design only; no code in this PR.
Scope: CareAgents (`careagents/`), the consumer app at careagents.cloud.
Not CareAgents Access, which is a separate project.

## 1. Problem

An account can hold several assistants. Each `Agent` row
(`careagents/models.py:123`) is a name, one `connection_id`, a persona
(`careagents/personas.py`) and an optional advisor
(`careagents/advisors.py`). The hub shows only the name and which records it
reads (`careagents/hub.py:68`), so testers see several assistants that look
identical. The difference between them, voice and specialty, is invisible.

The fix is a change of grain. The thing a person has one of is an
assistant **for a person**: themselves by default, and later someone they
care for. Specialties become routines that one assistant runs, and tone
becomes a setting on it.

## 2. Model

Four nouns, kept apart:

| Noun | What it is | Where it lives |
|---|---|---|
| Account | Who signs in (email, passkeys) | CareAgents `ca_accounts` |
| Person | Whose records an assistant reads: the account holder, or someone they care for | CareAgents, new `ca_people` |
| Connection | A pointer to one HealthClaw tenant holding one source's records | CareAgents `ca_connections` (unchanged) |
| Assistant | The `Agent` row: name, tone, routines, the connection it reads | CareAgents `ca_agents` |

### 2.1 New table `ca_people`

```
id                   person_<hex>
account_id           FK ca_accounts
kind                 "self" | "other"
relationship         enum, NULL for self: parent | child | partner | other
nickname             String(24), NULL for self ("Mom", "Leo")
attested_at          Float, NULL until the account holder attests authority
attestation_version  String(16), the attestation text that was agreed to
created_at           Float
```

Every account has exactly one `kind="self"` person, created with the account
(and by the migration for existing accounts).

The table holds no PHI. It must never gain a legal name, a date of birth or
any clinical field. Age-dependent behaviour reads `Patient.birthDate` from
the tenant through the engine, behind redaction and audit. The nickname is
the one free-text field, so it is capped at 24 characters and the form
labels it "A nickname, like Mom". See owner decision D4.

### 2.2 Changes to existing tables

- `ca_connections.person_id`: FK `ca_people`, NOT NULL after the migration.
  A connection belongs to one person. Today a connection is created before
  any assistant exists (`accounts.py:864` makes the first assistant only
  when records land), so the person has to be on the connection, not
  derived from the assistant.
- `ca_agents.person_id`: FK `ca_people`. Enforce one assistant per person
  with the compare-and-set pattern `ensure_first_agent` already uses
  (`accounts.py:833`), not a DB unique constraint, because legacy accounts
  violate it (section 4).
- `ca_agents.routines`: String, a comma-separated set of routine ids.
  Replaces `ca_agents.advisor`, which stays readable for one release and is
  then dropped.
- `ca_agents.persona` stays as the tone column. It is already per
  assistant, so making tone a setting changes the UI, not the schema.

### 2.3 Tenants and the engine: nothing changes

A Person is a CareAgents grouping. The engine never learns about it.

- Tenant stays one per connection. Every engine call still carries exactly
  one `X-Tenant-Id` and one step-up token minted for that tenant
  (`careagents/healthclaw.py:268-270`).
- A chat turn still reads exactly one tenant: `run_turn(cfg, hc, tenant, …)`
  (`careagents/agent.py:840`), and the run ownership check stays
  tenant plus agent (`careagents/app.py:1842`).
- Transcripts stay in the tenant, keyed `careagents:{agent_id}`
  (`careagents/healthclaw.py:826`).

No call path spans two tenants, so tenant isolation is not touched by this
spec in the beta.

### 2.4 One assistant reading several sources (deferred)

Two ways to do it. Neither ships in the beta.

1. **Ingest every source into one tenant per person.** Simple reads, but the
   ingest upsert keys on (tenant, resource type, id) (`r6/context_builder.py`,
   the id handling near line 95). Two providers that both export
   `Patient/123` overwrite each other. Revoking one source can then only
   purge the whole tenant. Rejected unless ingest namespaces ids per source.
2. **One tenant per source, fan-out in the tool layer.** Each tool call in
   `_execute_tool` (`careagents/agent.py:606`) loops over the person's
   active connections, one engine call per tenant, each with its own
   step-up token and its own audit row. Results merge only in the model's
   context. Open questions: which tenant owns the transcript (it dies with
   that source's purge), the approvals queue (already per tenant,
   `app.py:2182-2200`), and truncation honesty across a merged list.

Recommendation: option 2, after the beta. Until then a person can hold
several connections, and the assistant reads one at a time; the existing
`move_agent` (`accounts.py:891`) is the "switch source" control.

## 3. Sign-up creates one assistant

The assistant is still created when the first records become active
(`activate_connection` then `ensure_first_agent`, `accounts.py:864`, `:833`).
Creating it at sign-up would mean an agent with no connection, and
`get_agent_context` returns None in that case (`accounts.py:946-950`), so
chat would 404. Fewer states wins: the hub shows "Your assistant, waiting
for records" in the empty state, which is a template, not a row.

The change is to the add path. "Add an assistant" goes away. The hub offers
"Add someone you care for" (section 6), and a person who already has an
assistant gets no second one.

## 4. Migration of existing accounts

One idempotent startup migration in the style of `models._ensure_columns` (`models.py:375`):

1. For each account, insert one `kind="self"` person.
2. Point every existing connection and every existing agent at that person.
3. `routines` = `{advisor}` for `healthy-habits`, `care-completion`,
   `medication-refills`; `{habits}` for `diet-exercise`; empty for
   `general`, NULL or an unavailable advisor.

Every legacy assistant, including a second assistant on a different
connection, is attached to the **self** person. Its records might belong to
someone else, but the migration must not mark them so: that would infer
authority from a pointer. If the holder says otherwise, they add that person
and move the connection, and the attestation in section 6 applies.

Legacy assistants are kept, not merged. Merging would orphan their
transcripts, which live in the tenant under each agent's own id.

How they render:

- **Primary assistant**: the oldest agent on an active connection. The hub
  shows it as "Your assistant".
- **Other assistants**: listed under it, each showing its tone and its
  routines. Showing them is the fix for "identical assistants". Each has
  Remove (the existing `delete_agent`, `accounts.py:912`). No Add.
- Surfaces bound to a legacy assistant keep working unchanged.

Before shipping, Dev counts the affected accounts with the operator CLI
(counts only) so the owner knows how many people see the legacy rendering.

## 5. Surfaces

A surface binds to an assistant (`ca_surfaces.agent_id`), so it reaches
exactly one person. Nothing about binding changes:

- **iMessage / Sendblue**: a handle is bound to at most one account
  (`bind_imessage_handle`, `accounts.py:1032`, backed by the partial unique
  index), and to the assistant whose pairing code it used. One number,
  one person. A caregiver who texts about someone else pairs that person's
  assistant from its settings page, which moves the handle. Switching
  person by a text command ("switch to Mom") is deferred. A misheard
  switch answers about the wrong person, so it needs its own design.
- **Telegram**: the same rule.
- **Web**: a person switcher at the top of the hub and chat. The chat
  header always names the person ("Mom's records") when it is not self.
- **Approvals**: the queue is already one per tenant (`app.py:2182-2200`).
  Each queue is labelled with its person.

## 6. Someone you care for (proxy access)

### 6.1 What we can honestly support

We cannot verify that a caregiver has authority over another person's
records. Instead we depend on a check the source has already made, and we
record what the caregiver tells us. We never infer anything.

- **Authority comes from the source.** The caregiver connects using their own
  proxy access to the other person's patient portal. The portal has
  already decided they may see those records. CareAgents does not widen
  that access.
- **Attestation is explicit.** Before the first connection for a
  `kind="other"` person, the caregiver ticks an unticked box: "I am allowed
  to see these records, for example through proxy access the portal gave
  me." `attested_at` and `attestation_version` are stamped. The connect
  route refuses a `kind="other"` person without a current attestation.
  Nothing is pre-checked, nothing carries over from the self person, and
  a newer attestation version asks again (the `consent_version` pattern,
  `models.py:94-100`).
- **Consent and authority are different records.** `Connection.consented_at`
  stays the account holder accepting the terms. It is not authority, and
  is never read as such.

### 6.2 Audit records who acted

Today every CareAgents call sends the constant `X-Agent-Id: careagents`
(`healthclaw.py:270`). The engine writes that header into
`AuditEventRecord.agent_id` (`r6/routes.py:400` and others;
`r6/models.py:216`, `String(128)`). A dependent's audit trail would show
that CareAgents read their records, not which account did.

Change: send `careagents:<account_id>`, an opaque pointer of about 30
characters with no PHI, on user-driven calls, and keep `careagents-worker`
for the worker plus the account pointer carried on the run. This is a
CareAgents change only; the engine already stores the header as given. It
is worth doing for the self case as well. The audit trail lives in the
person's tenant, so "their own audit trail" is true without extra work.

### 6.3 Out of the beta

- Third-party agent grants (MCP consent, `Grant`, `models.py:215`,
  `careagents/consent.py`): the connection chooser excludes `kind="other"`
  connections. Whether a caregiver can grant Claude access to a parent's
  records is a separate decision.
- Shared access between two accounts (co-caregivers), and a dependent
  taking over their own records. Ids do not transfer across the
  CareAgents/HealthClaw boundary, so both need their own design.
- Adolescents (12 to 17). Portals restrict proxy access for this age group
  on purpose, and the rules vary by state. See D1.

## 7. Routines

Today an advisor is a system-prompt block and nothing else
(`advisors.py:8-13`). The tool set is the same guarded six for all of them.
That stays true: a routine is a guidance block, and an assistant with
several routines on gets their blocks concatenated after the persona and
safety core (`personas.py:104`). Turning one on or off changes no access.

| Advisor today | Routine | Beta |
|---|---|---|
| `general` | none; it is the base prompt | always on, not a toggle |
| `healthy-habits` | Habits and vitals | toggle |
| `diet-exercise` | merged into Habits and vitals (the two overlap) | merged |
| `care-completion` | Care gaps | toggle |
| `medication-refills` | Refills | toggle |
| `research-monitor` | Research | hidden; still unavailable |
| `kids-health` | none; it becomes the Person kind, and its guidance is unwritten | deferred |

**Proactive behaviour does not ship in the beta.** "Watching" suggests the
assistant runs without being asked. That would mean scheduled, unattended
reads of PHI and outbound messages with health content. Texting health
content through a vendor whose BAA we do not have is blocked on vendor
terms whatever the code does. Until a routine runs unprompted, the
copy says what is true: "Focuses on: refills, care gaps". See D3.

## 8. Beta slice vs later

Measured against `docs/beta-program.md` §4: Cohort 1 is invited and
synthetic, and Cohort 2 has real records after the hard gates.

**Beta (ship):**

1. `ca_people` plus the migration in section 4, with person ids on
   connections and agents.
2. One assistant per person: no "Add an assistant"; the hub's empty state
   says "Your assistant, waiting for records".
3. Tone and routines visible on the hub card and editable in settings,
   with routines replacing advisors.
4. Legacy rendering for accounts with several assistants.
5. Audit actor pointer (section 6.2).
6. "Add someone you care for" visible, with the **sample connection only**,
   so Cohort 1 can test the shape on made-up records.

**Beta, behind a flag, per invite** (reuse the `RealRecordInvite` pattern,
`models.py:320`): real records for someone you care for. Gated on items 1
and 5, on the attestation in 6.1, and on owner decision D1.

**Later:** several sources per assistant (2.4, option 2); proactive
routines; switching person by text; co-caregivers and dependent takeover;
MCP grants for other people's records; kids and research routines.

## 9. Guardrail risks

| Risk | Where | Control |
|---|---|---|
| Wrong-person answer | surfaces, chat | one surface reaches one assistant, which reaches one tenant; the person is named in the chat header and the approvals queue; no text-command switching |
| Cross-tenant read | tool loop | unchanged: one tenant per turn and per call, run check is tenant plus agent (`app.py:1842`); multi-source deferred |
| PHI in CareAgents | `ca_people` | no name, DOB or clinical field; nickname capped and labelled; relationship is an enum; transcripts stay in the tenant |
| Inferred authority | migration, connect | legacy rows default to self; an explicit, versioned attestation is required for `kind="other"`; nothing pre-checked |
| Audit cannot say who acted | `healthclaw.py:270` | account pointer in `X-Agent-Id` (6.2) |
| Redaction | engine | unchanged; Person never reaches the engine, and labels still come from `r6/terminology.py` after `apply_redaction` |
| Step-up | engine calls | unchanged; one token per tenant per call, never reused across tenants |
| Writes | action rail | unchanged; approvals stay out-of-band on the rail, nothing new on `X-Human-Confirmed` (#214); the approval page names the person |
| Third-party exposure of a dependent's records | MCP consent | `kind="other"` connections excluded from the grant chooser in the beta |

## 10. Owner decisions

- **D1. Who can be added with real records in the beta.** Recommendation:
  adults who gave the caregiver portal proxy access, and children under 12
  through a parent's proxy. Exclude 12 to 17. This needs counsel, not
  engineering. Until it is decided, "someone you care for" is sample-only.
- **D2. Several sources per assistant.** Recommendation: defer past the
  beta, then option 2 in section 2.4.
- **D3. Routine copy.** Recommendation: "Focuses on" until something runs
  unprompted. "Watching" waits for proactive routines and a BAA-covered
  outbound channel.
- **D4. Person label.** Recommendation: relationship enum plus an optional
  nickname of up to 24 characters. The alternative is relationship only
  ("Your parent"), which carries less risk and is clunkier when a person
  cares for two parents.
- **D5. Legacy assistants.** Recommendation: keep them, show them with tone
  and routines, offer Remove, and never auto-merge.
- **D6. Merge `diet-exercise` into Habits and vitals.** Recommendation: yes.
