# CareAgents: a calm hub and a real connector menu

Status: draft for owner review, 2026-09-26. Reviewed by the product agent;
its required changes are applied.
Scope: phase 1 of the stable-beta plan. Phase 2 opens real records.

## 1. Goal

A person opens CareAgents and sees one assistant, the records it reads, and
anything waiting for their approval. Connecting a new source is one clear
menu, with finding their records at the top.

Success means three things:

- A new account never sees a duplicate row.
- Every tile either works or says plainly why not, in one short line.
- The approval queue is visible from the hub without opening a chat.

## 2. Why now

A survey of the shipped hub found the clutter is self-inflicted:

- Every tap on the sample tile creates another "Sample records" row.
- Accounts can create unlimited agents, and cards look identical.
- Revoked connections stay on the page with a Delete button.
- With default settings, 6 of 7 connector tiles are dead.
- There is no settings page, and the visit brief is unreachable.

Competing apps (Apple Health, Google Health, Samsung Health, Verily Me)
share one pattern. A single front door finds your records, and connected
sources show when they last synced. Our difference is the approval card: a
person approves every outbound form, call or text. The hub should show it.

## 3. The hub, top to bottom

1. **Waiting for you.** Always shown. With nothing pending it is one line:
   "Nothing yet. Anything your assistant prepares, like a form or a
   reminder, waits here for your OK." With requests pending it becomes a
   band: "2 requests waiting for your approval", linking to the queue. If
   the count cannot be fetched it says "Couldn't check for requests", never
   zero.
2. **Your assistant.** One card per agent. It shows the name, which records
   it reads, and two actions: "Chat" and "Visit brief". A menu holds Rename,
   Change records, and Delete.
3. **Your records.** One card per active connection. It shows the source, a
   record count and "Updated 3 days ago". The sample card carries a
   "Made-up records" badge.
4. **Add records.** The connector menu, section 4.
5. **Past connections.** Revoked connections, collapsed by default, each
   with Delete.

Surfaces, sharing grants and account deletion move to the settings page.
Grants stay one tap away as a hub link when any exist.

## 4. The connector menu

The menu has two states, decided per account by the real-records setting.

**Real records closed** (most beta accounts):

- One full-width primary action: "Explore with made-up records".
- One line underneath: "Coming for invited testers: your doctor's records,
  Apple Health and wearables, uploading a file from your patient portal."
- No groups and no coming-soon tiles.

**Real records open:**

| Group | Source | How it connects today |
|---|---|---|
| Find my records | Fasten: "Find my records at my doctor or hospital" (full width) | Fasten Connect, in the app |
| Find my records | Epic MyChart and other portals, via Health Skillz | Sign in there, download the file, upload it here |
| Record services | Health Bank One | OAuth flow exists (`/shc/hbo/callback`), run by the bot today |
| Record services | HealthEx | Arrives through the HealthEx connector in Claude today |
| Bring a file | "Upload a file from your patient portal" | In the app |
| Bring a file | SMART Health Link | Sharing works; importing waits on the decoder (#225) |
| Devices and apps | Apple Health and wearables | In the app, when enabled |

Each source shows one status chip: **Connected**, **Available** or **Coming
soon**. A source is **Available** only when a person can finish it inside
CareAgents and the import lands in their own tenant. That is proven by a
test that runs the full import. Until then it shows **Coming soon**.

Phase 1 makes three sources available: Fasten, file upload, and Health
Skillz as a guided two-step flow through file upload. Health Bank One,
HealthEx and SMART Health Link import each get their own wiring task, in
that order. Each flips to Available as its test passes.

The sample stays available as a small link.

Rules for both states:

- **No operator words.** "Not configured on this deployment" and "sidecar
  not wired" never reach a person. The reason goes to the server log.
- **The sample is one per account.** Tapping it again opens the existing
  sample. The server returns the existing active sample connection instead
  of minting a new tenant.

## 5. One assistant, straight into chat

- The first time a connection becomes active, create one agent for it:
  default name "Juniper", calm voice, no specialty. That happens in two
  places: the sample connect handler, and the ingest-complete path for
  real records.
- After the sample connects, go straight to chat. The first starter prompt
  is "Fill out my intake form for a new doctor". That path ends in an
  approval, so the first five minutes show what the product is for.
- When a real connection first becomes active and the agent reads the
  sample, the hub asks once: "Switch Juniper to your records?"
- "Change records" moves an agent to another of the account's active
  connections. The server checks ownership, as `create_agent` does today.
- Delete removes the agent only. Its conversation stays in HealthClaw until
  the connection is deleted, as today.
- The hub has no "add another assistant" control in phase 1. The API keeps
  supporting more than one.

Nobody's existing agents are merged or deleted. The first-run default only
applies to accounts with no agents.

## 6. Settings

A new `/settings` page:

- Passkeys: list and add.
- Where you can reach your assistant: the surfaces now on the hub.
- Apps you have shared records with: the grants now on the hub.
- Sign out, and Delete my account.

## 7. Copy

| Today | After |
|---|---|
| Banner "synthetic records only" for every account | Matches the account: "Sample records" or "Your records are connected" |
| "Your provider (verified)" / "My health provider" | "Find my records" and, once connected, the provider's name |
| "Upload records" (FHIR bundle) | "Upload a file from your patient portal" |
| "no signup" on the sample tile | "Made-up records to explore safely" |
| Status words `active`, `pending`, `empty`, `revoked` | "Connected", "Connecting…", "No records yet", in past connections only |
| Sample card "Disconnect: stop new records arriving" | Sample cards offer Delete only |
| First-run "connect your health records" | "Start with sample records, or find your own" when open |

Also: remove the duplicated starter prompts in `chat.html`, and the
Telegram handler bound to a removed element in `home.js`.

## 8. Out of scope

- Identity-first record discovery. That is phase 2, after the Fasten fix.
- Search inside the connector menu. There are too few sources to need it.
- A waitlist for coming-soon sources.
- A "Your AI" settings section, added when #834 ships.
- The bring-your-own-key feature (its own spec, #834).

## 9. Testing

- Tapping the sample tile twice leaves one active sample connection.
- A first active connection creates exactly one agent; a second does not.
- Change records refuses another account's connection.
- The hub shows no revoked connection outside past connections.
- Catalog output contains none of the operator phrases in section 4.
- Pending-approval count matches the approvals endpoint; a failed count
  never renders as zero.
- With real records closed, the menu shows no coming-soon tiles.
- The sample connect lands in chat with the intake-form starter first.
- Every status word in section 7 renders as its plain-language form.
- A browser run of the new-account journey at phone width, captured before
  and after, on synthetic records only.
- Mutation evidence for the sample dedupe and the ownership check.
