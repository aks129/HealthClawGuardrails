# CareAgents: a pathway from sample beta to real-life testing

Status: approved by the owner, 2026-09-26.
Depends on: the calm hub (phase 1), approved providers for real records
(#833), and bring your own key (#834).

## 1. Goal

Get many people using CareAgents, and move them onto their own records in
stages. Each stage has a gate. Nobody reaches real records before its gate
is met.

Success means we can answer one question each week with numbers: of the
people who connected real records, how many asked a question and approved
an action?

## 2. Stages

| Stage | Who | Records | Size | Gate to enter |
|---|---|---|---|---|
| 0. Open beta | Anyone who signs up | Sample only | Unlimited | Calm hub shipped |
| 1. Invited testers | People we invite | Their own, by invitation | Up to 25 | Owner gates in section 3 |
| 2. Wider testing | Waitlist, in batches | Their own | 100, then 500 | Two clean weeks of stage 1, and counsel sign-off |

Stage 0 runs now. It recruits through the beta issue (#718), the quickstart
guides and the site.

## 3. Owner gates before stage 1

Decided or done on 2026-09-26:

1. **Legal.** The owner decided the FTC Health Breach Notification Rule does
   not currently apply (#168, closed). The privacy policy date (#565) and
   short tester terms remain to write.
2. **Approved model providers for real records:** Anthropic, OpenAI, Google
   Gemini and Groq (#839). Real-record chat should use a paid key, since
   free API tiers may use prompts to improve the provider's models.
3. **Hosts.** `mcp.healthclaw.io` now points at the MCP server (#522,
   closed). The stale CareAgents copy on the old VPS is stopped (#624).
4. **Incident contact:** contactus@healthclaw.io. It is told within an hour
   if tester records may have been exposed, and it notifies testers.

Still open for the owner: the privacy policy date and tester terms, and
whether any secret the old VPS copy held is shared with production (#624).

## 4. What engineering builds

### 4.1 A waitlist

A form on the landing page and on the "Coming for invited testers" line:
email, and what the person wants to connect. It is stored in CareAgents as
account-level data. It asks for no health information and says so.

### 4.2 Invites in the database

Built 2026-10-01: `flask --app careagents.wsgi invites add|list|revoke`.
See `docs/runbooks/careagents-durable-worker.md`.

Today real-record access is a comma-separated environment variable. That
does not scale past a handful of people.

- New table `ca_real_record_invites`: email, invited_at, invited_by,
  revoked_at.
- An operator script adds, lists and revokes invites. No admin web page yet.
- `real_records_open_for(email)` checks the table in `allowlist` mode. The
  environment variable keeps working, so nothing breaks on deploy.
- Revoking an invite blocks new real connections. Existing ones keep working
  until the person disconnects, or the operator uses the kill switch.

### 4.3 Tester terms at first real connection

The existing consent card gains the tester terms from gate 1. That bumps the
consent version, so every tester accepts the new wording once.

### 4.4 Feedback in the app

A "Send feedback" link on the hub and in chat. It opens a short form: what
you tried, what happened, and a 1 to 5 rating. The form says not to include
health details. Entries are capped in length and stored as account data.

### 4.5 The weekly number

Counts per week, per stage, with no health data:

- signed up;
- connected real records;
- asked at least one question;
- approved at least one action.

These come from existing tables and run events, as counts only. No page
tracking is added behind sign-in.

### 4.6 Kill switches

- `CARE_REAL_RECORDS=off` already closes new real connections.
- New: an operator script pauses one account's real connections. The
  assistant then answers "Your records are paused" until resumed.

## 5. Out of scope

- Paid plans and credits (spec B).
- Automatic batch invites. The operator script is enough for stage 1.
- A public status page.

## 6. Testing

- An invited email can start a real connection; an uninvited one cannot.
- The environment allowlist still works alongside the table.
- A revoked invite blocks new connections and keeps existing ones.
- A paused account's turns answer the paused message and call no provider.
- Waitlist and feedback forms store no field beyond the ones listed.
- Weekly counts contain numbers only.
- A consent version bump makes a tester accept again.
