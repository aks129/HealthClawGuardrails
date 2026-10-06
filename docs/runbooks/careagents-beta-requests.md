# Runbook: beta-tester requests (`/beta`)

What the brief (`docs/briefs/2026-10-06-beta-onboarding.md`) asks the owner
to run by hand. Code: `careagents/beta_signup.py`.

Run every command as `flask --app careagents.wsgi <command>` with
`CARE_DATABASE_URL` pointing at the database to read or change. Output goes
to your terminal only. Anything a visitor typed is printed escaped.

## How a request arrives

Double opt-in. A submit creates a `pending` request and emails the address
one "Confirm you asked to test CareAgents" link (single use, 24 hours). A
mailbox gets at most one confirmation email a day, however many times or
from wherever it is submitted; the page answers the same either way. The
mailbox is the address lowercased with any `+tag` dropped, and for Gmail
with the dots dropped and googlemail.com folded into gmail.com, so
`pat+1@` and `p.a.t@gmail.com` count as the inbox they reach.

The confirm page shows what is being confirmed: a new request or a change,
the first name, and the last four digits of the mobile, or no mobile.
Nothing else happens until the link is confirmed: no iMessage spot, no owner
email, no "You're in". A submit for an address that is already confirmed
never changes it; it asks that address to confirm the change. A link nobody
uses lapses after 24 hours and takes what it asked about with it: the
change on a confirmed request, the number on an unconfirmed one.

## Settings

| Variable | Effect |
| --- | --- |
| `CARE_BETA_NOTIFY_EMAIL` | Where a confirmed request is announced. Unset: no announcement. The email carries the first name, the masked email domain, whether a mobile was given, the `ref` and the status. Never the address or the number. |
| `SENDBLUE_*` | With Sendblue on (`cfg.sendblue_enabled`), `mark ... added` emails the "Text hi" line, and the hub shows "Text your assistant" to a sample account whose request is `added` or `active`. |
| `RESEND_API_KEY` | With no key, no email goes out and the form still succeeds. |
| `CARE_ENV` | Real mail leaves only when this is `production`. Anywhere else the mail module logs "mail suppressed (not production)" and sends nothing, even with a key: the `flask` CLI loads the repository's `.env`. Production sets `CARE_ENV=production` (`deploy/careagents/deploy.sh`; required by the durable-worker runbook's variable check). |
| `CARE_ALLOW_REAL_MAIL` | `1` lets a non-production run send real mail. Leave it unset. |

## Daily

```sh
flask --app careagents.wsgi beta-requests list            # confirmed
flask --app careagents.wsgi beta-requests list --pending  # not confirmed yet
```

Shows date, first name, email, mobile, ref and status. The mobile is in full
while the status is `new` or `waitlist`, so you can act on it.

Ten iMessage spots, and one per mailbox and one per mobile. A confirmed
request with a mobile waits when the ten are taken, or when another request
with the same mailbox or the same mobile holds a spot; `list` shows
`waitlist #1`, `#2`... in queue order (first confirmed, first served), with
`(same mailbox as another request)` or `(same mobile as another request)`
when that is why. The tester was told iMessage is full and the web app
works today.

When a spot frees up (`mark ... removed`, a tester's removal link, a deleted
account, the purge), the oldest waitlisted request that is not a twin of a
spot holder becomes `new`. The log records it with the domain masked, the
tester is emailed the "You're in" text with the texting line, and `mark`
and `purge` print it. `mark ... new` on a waitlisted request respects the
ten; `--force` goes past them.

## Adding someone to iMessage

1. Add the number from `beta-requests list` as a contact in the Sendblue
   dashboard.
2. Mark it:

   ```sh
   flask --app careagents.wsgi beta-requests mark someone@example.com added
   ```

   This deletes the stored number and emails the tester "Text hi to
   +1 555-010-9999. You'll get a link back to sign in, then your assistant
   answers there.", the number as an `sms:` link. With Sendblue off, the
   status changes and no email is sent; the command says so. From then on
   the tester's hub shows the same line on the "Text your assistant" tile.

Other statuses: `new`, `waitlist`, `active` (a tester the Monday check-in
goes to), `removed` (also deletes the number and frees the spot). A later
submit from a `removed` address has to be confirmed again.

## Retention (weekly)

The page promises: the mobile is deleted once marked `added`, or after 30
days; a request whose email never became an account is deleted after 60
days. Unconfirmed requests go after 7 days, and a number nobody confirmed
goes when its link lapses (24 hours; also cleared whenever the table is
read). `mark ... added` does the first. Run the rest at least weekly:

```sh
flask --app careagents.wsgi beta-requests purge
```

It prints how many requests and mobiles it deleted, clears day-old
confirmation-email records, and fills any freed spots. Two ways to run it:

- **Railway cron (preferred).** A cron service from the same image, with the
  web service's `CARE_DATABASE_URL` and `CARE_ENV=production`, schedule
  `0 9 * * 1` (Mondays 09:00 UTC), start command
  `flask --app careagents.wsgi beta-requests purge`. Deploy it from the same
  stage as web and worker (`docs/runbooks/careagents-durable-worker.md`).
- **By hand.** Every Monday, before the check-in email, run the command
  above against production.

## Links in the emails

Confirmation and removal links are `/beta/confirm?t=<token>` and
`/beta/remove?t=<token>`. Opening one only asks; the button acts. Only a
hash of each token is stored. The removal ask screen also has "Keep my
request", which lands on a page that says nothing changed. The tokens are in
the query string, which the access log does not record (`%(U)s`).

Deleting a CareAgents account also deletes its beta request.

## The form's rate limit: X-Real-IP

Five submits per client address per ten minutes, per process. The client
address is the `X-Real-IP` header, which Railway's edge sets to the
connecting client's address, overwriting any value the client sent. A
missing or malformed value falls back to the direct peer. `X-Forwarded-For`
is not read at all: Railway staff on the Railway Help Station (2026-06-12)
say the client is its first value and another hop may follow, so its
right-hand entry can be an internal hop shared by every client, and its
left-hand entries are whatever the client wrote. Sources: the two Railway
Help Station threads cited in the security tester's round-two report on PR
874. CareAgents does not use ProxyFix.

This trusts the edge: a request that reaches gunicorn without passing
Railway's edge could choose its own `X-Real-IP`. The per-mailbox email cap
does not depend on it; it is in the database.
