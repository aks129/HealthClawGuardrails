# Runbook: beta-tester requests (`/beta`)

What the brief (`docs/briefs/2026-10-06-beta-onboarding.md`) asks the owner
to run by hand. Code: `careagents/beta_signup.py`.

Run every command as `flask --app careagents.wsgi <command>` with
`CARE_DATABASE_URL` pointing at the database to read or change. Output goes
to your terminal only. Anything a visitor typed is printed escaped.

## How a request arrives

Double opt-in. A submit creates a `pending` request and emails the address
one "Confirm you asked to test CareAgents" link (single use, 24 hours). An
address gets at most one confirmation email a day, however many times or
from wherever it is submitted; the page answers the same either way.
Nothing else happens until the link is confirmed: no iMessage spot, no owner
email, no "You're in". A submit for an address that is already confirmed
never changes it; it asks that address to confirm the change.

## Settings

| Variable | Effect |
| --- | --- |
| `CARE_BETA_NOTIFY_EMAIL` | Where a confirmed request is announced. Unset: no announcement. The email carries the first name, the masked email domain, whether a mobile was given, the `ref` and the status. Never the address or the number. |
| `SENDBLUE_*` | With Sendblue on (`cfg.sendblue_enabled`), the hub shows "Text your assistant" to sample accounts, and `mark ... added` emails "Text hi to" the Sendblue line. |
| `RESEND_API_KEY` | With no key, no email goes out and the form still succeeds. Keep it empty in local runs. |

## Daily

```sh
flask --app careagents.wsgi beta-requests list            # confirmed
flask --app careagents.wsgi beta-requests list --pending  # not confirmed yet
```

Shows date, first name, email, mobile, ref and status. The mobile is in full
while the status is `new` or `waitlist`, so you can act on it.

When ten confirmed requests with a mobile already hold an iMessage spot, the
next confirmed one is `waitlist`, shown as `waitlist #1`, `#2`... in queue
order (first confirmed, first served). The tester was told iMessage is full
and the web app works today. When a spot frees up (`mark ... removed`, a
tester's removal link, a deleted account, the purge), the oldest waitlisted
request becomes `new` automatically; `mark` prints who was promoted.

## Adding someone to iMessage

1. Add the number from `beta-requests list` as a contact in the Sendblue
   dashboard.
2. Mark it:

   ```sh
   flask --app careagents.wsgi beta-requests mark someone@example.com added
   ```

   This deletes the stored number and emails the tester "Text hi to
   (number). You'll get a link back to sign in, then your assistant answers
   there." With Sendblue off, the status changes and no email is sent; the
   command says so.

Other statuses: `new`, `waitlist`, `active` (a tester the Monday check-in
goes to), `removed` (also deletes the number and frees the spot). A later
submit from a `removed` address has to be confirmed again.

## Retention (weekly)

The page promises: the mobile is deleted once marked `added`, or after 30
days; a request whose email never became an account is deleted after 60
days. Unconfirmed requests go after 7 days. `mark ... added` does the first.
Run the rest at least weekly:

```sh
flask --app careagents.wsgi beta-requests purge
```

It prints how many requests and mobiles it deleted, and clears day-old
confirmation-email records. Two ways to run it:

- **Railway cron (preferred).** A cron service from the same image, with the
  web service's `CARE_DATABASE_URL`, schedule `0 9 * * 1` (Mondays 09:00
  UTC), start command `flask --app careagents.wsgi beta-requests purge`.
  Deploy it from the same stage as web and worker
  (`docs/runbooks/careagents-durable-worker.md`).
- **By hand.** Every Monday, before the check-in email, run the command
  above against production.

## Links in the emails

Confirmation and removal links are `/beta/confirm?t=<token>` and
`/beta/remove?t=<token>`. Opening one only asks; the button acts. Only a
hash of each token is stored. The removal ask screen also has "Keep my
request". The tokens are in the query string, which the access log does not
record (`%(U)s`).

Deleting a CareAgents account also deletes its beta request.

## The form's rate limit and X-Forwarded-For

Five submits per client address per ten minutes, per process. The client
address is the right-hand `X-Forwarded-For` entry, which is the one
Railway's edge writes; anything to its left came from the client. A
private or internal address in that place (10/8, 172.16/12, 192.168/16,
loopback, link-local, 100.64/10) is treated as not a client, and shares the
direct peer's bucket. This assumes Railway's edge is the only way in; it is
not verified against Railway's documentation, and CareAgents does not use
ProxyFix. The per-address email cap does not depend on it: it is in the
database.
