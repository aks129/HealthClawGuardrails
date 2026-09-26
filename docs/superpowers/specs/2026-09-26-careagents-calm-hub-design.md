# CareAgents: a calm hub and a real connector menu

Status: draft for owner review, 2026-09-26.
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

1. **Waiting for you.** Shown only when something is pending: "2 requests
   waiting for your approval", linking to the queue. First on the page.
2. **Your assistant.** One card per agent. It shows the name, which records
   it reads, and two actions: "Chat" and "Visit brief". A menu holds Rename,
   Change records, and Delete.
3. **Your records.** One card per active connection. It shows the source, a
   record count and "Updated 3 days ago". Actions: Refresh or Upload, and
   Disconnect.
4. **Add records.** The connector menu, section 4.
5. **Past connections.** Revoked connections, collapsed by default, each
   with Delete.

Surfaces, sharing grants and account deletion move to the settings page.
Grants stay one tap away as a hub link when any exist.

## 4. The connector menu

Grouped by what a person is trying to do. Each group lists its sources with
one status chip: **Connected**, **Available** or **Coming soon**.

| Group | Sources |
|---|---|
| Find my records | Fasten: "Find my records at my doctor or hospital" |
| Bring a file | Upload a FHIR file; SMART Health Link |
| Devices and apps | Apple Health and wearables |
| Other record services | HealthEx; Health Bank One |
| Practice | Sample records |

Rules:

- **Fasten leads.** When real records are open for the account, it is a
  full-width button above the groups.
- **Closed is not dead.** When real records are closed for the account, the
  real-record sources show "Coming soon" with one shared line: "Real
  records open to invited testers first."
- **Coming soon collapses.** A group with nothing available shows its chips
  on one line, not full tiles.
- **No operator words.** "Not configured on this deployment" and "sidecar
  not wired" become "Coming soon". The reason goes to the server log.
- **The sample is one per account.** Tapping it again opens the existing
  sample. The server returns the existing active sample connection instead
  of minting a new tenant.

## 5. One assistant by default

- When an account's first connection becomes active, create one agent for
  it: default name "Juniper", calm voice, no specialty.
- "Add another assistant" stays, as a small link under the cards.
- "Change records" moves an agent to another of the account's active
  connections. The server checks ownership, as `create_agent` does today.
- Delete removes the agent only. Its conversation stays in HealthClaw until
  the connection is deleted, as today.
- The create form's record picker lists active connections only.

Nobody's existing agents are merged or deleted. The first-run default only
applies to accounts with no agents.

## 6. Settings

A new `/settings` page:

- Passkeys: list, add, remove (the last one needs another sign-in method).
- Where you can reach your assistant: the surfaces now on the hub.
- Apps you have shared records with: the grants now on the hub.
- Your AI: a placeholder until the bring-your-own-key spec ships.
- Sign out, and Delete my account.

## 7. Copy

| Today | After |
|---|---|
| Banner "synthetic records only" for every account | Matches the account: "Sample records" or "Your records are connected" |
| "Your provider (verified)" / "My health provider" | "Find my records" and, once connected, the provider's name |
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
- The bring-your-own-key feature (its own spec, #834).

## 9. Testing

- Tapping the sample tile twice leaves one active sample connection.
- A first active connection creates exactly one agent; a second does not.
- Change records refuses another account's connection.
- The hub shows no revoked connection outside past connections.
- Catalog output contains none of the operator phrases in section 4.
- Pending-approval count matches the approvals endpoint.
- Every status word in section 7 renders as its plain-language form.
- A browser run of the new-account journey, captured before and after, on
  synthetic records only.
- Mutation evidence for the sample dedupe and the ownership check.
