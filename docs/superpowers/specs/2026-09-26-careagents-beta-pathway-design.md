# CareAgents: a pathway from sample beta to real-life testing

Status: draft for owner review, 2026-09-26.
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

These are decisions or accounts only the owner holds. Engineering cannot
close them.

1. **Legal.** Decide with counsel whether the FTC Health Breach Notification
   Rule applies (#168). Update the privacy policy and its date (#565). Add
   short tester terms.
2. **A vetted model for real records.** Production chat runs on Gemini today.
   The #833 rule refuses to start with real records on until chat uses
   Anthropic or OpenAI. Fund one of those keys, or have testers bring their
   own under #834.
3. **Hosts.** Point `mcp.healthclaw.io` away from its dangling record (#522).
   Shut the abandoned VPS copy (#624).
4. **An incident contact.** Name who is told within an hour if tester records
   may have been exposed, and who notifies testers.

## 4. What engineering builds

### 4.1 A waitlist

A form on the landing page and on the "Coming for invited testers" line:
email, and what the person wants to connect. It is stored in CareAgents as
account-level data. It asks for no health information and says so.

### 4.2 Invites in the database

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
