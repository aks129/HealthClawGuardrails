# Runbook: beta-tester requests (`/beta`)

What the brief (`docs/briefs/2026-10-06-beta-onboarding.md`) asks the owner
to run by hand. Code: `careagents/beta_signup.py`.

Run every command as `flask --app careagents.wsgi <command>` with
`CARE_DATABASE_URL` pointing at the database to read or change. Output goes
to your terminal only.

## Settings

| Variable | Effect |
|---|---|
| `CARE_BETA_NOTIFY_EMAIL` | Where a new request is announced. Unset: no announcement; the request is still saved. The email carries the masked email domain, whether a mobile was given, the `ref` and the status. Never the name, the address or the number. |
| `CARE_IMESSAGE_HANDLE` | The number in the "Text hi to ..." email, and the switch for the hub's "Text your assistant" tile. Replaced by the Sendblue setting when #868 lands. |
| `RESEND_API_KEY` | With no key, no email goes out and the form still succeeds. |

## Daily

```sh
flask --app careagents.wsgi beta-requests list
```

Shows date, first name, email, mobile, ref and status. The mobile is in full
only while the status is `new`.

A request that gave a mobile after ten others already hold an iMessage spot
arrives as `waitlist`. The tester was told that iMessage is full and that the
web app works today.

## Adding someone to iMessage

1. Add the number from `beta-requests list` as a contact in the Sendblue
   dashboard.
2. Mark it:

   ```sh
   flask --app careagents.wsgi beta-requests mark someone@example.com added
   ```

   This deletes the stored number and emails the tester "Text hi to <number>".
   Without `CARE_IMESSAGE_HANDLE` set, the status changes and no email is
   sent; the command says so.

Other statuses: `waitlist`, `active` (a tester the Monday check-in goes to),
`removed` (also deletes the number). A second submit from a `removed` email
starts again as `new`.

## Retention (weekly)

The page promises: the mobile is deleted once marked `added`, or after 30
days; a request whose email never became an account is deleted after 60
days. `mark ... added` does the first. Run the rest at least weekly:

```sh
flask --app careagents.wsgi beta-requests purge
```

It prints how many requests and mobiles it deleted. Two ways to run it:

- **Railway cron (preferred).** A cron service from the same image, with the
  web service's `CARE_DATABASE_URL`, schedule `0 9 * * 1` (Mondays 09:00
  UTC), start command
  `flask --app careagents.wsgi beta-requests purge`. Deploy it from the same
  stage as web and worker (`docs/runbooks/careagents-durable-worker.md`).
- **By hand.** Every Monday, before the check-in email, run the command
  above against production.

## Removal links

Every tester email ends in a "Remove my request" link,
`/beta/remove?t=<token>`. Opening it asks; the button deletes the request.
Only a hash of the token is stored, and each new email replaces the previous
link. The token is in the query string, which the access log does not record
(`%(U)s`, from #866).

Deleting a CareAgents account also deletes its beta request.
