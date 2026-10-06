# Runbook: Sendblue as the CareAgents iMessage line

Sendblue is a hosted iMessage provider. It replaces the Mac-mini relay
(`deploy/careagents/imessage_relay.py`) as the transport for the text
surface. The surface logic is unchanged: `careagents/imessage.py` decides what
every text means. `careagents/sendblue_surface.py` is the adapter, and
`careagents/sendblue.py` is the HTTP client.

## Before real records go over this line

**Real health records must not go over Sendblue until two things are in
place:** Sendblue's dedicated HIPAA instance, and a signed Business Associate
Agreement with Sendblue. The shared sandbox and standard plans have neither.
Until both exist, use this line only with sample-record accounts and testers
who know it is not a HIPAA channel. An agent's answer can quote a lab value,
and that text goes through Sendblue.

The code enforces this. `SENDBLUE_REAL_RECORDS` is off unless set to `1`,
`true`, `yes` or `on`. While it is off, a text to an agent whose connection
is anything but the sample starts no run. It is answered with "Your
assistant is using your real records, so I can't send answers by text yet.
Open careagents.cloud to read it." The worker checks again before it sends,
so an answer queued before the agent moved to real records is withheld the
same way. Switch the flag on only after the HIPAA instance and the BAA are
in place.

## The sandbox

- The sandbox line is a **shared number**. Our own number comes with a paid
  plan.
- Up to **10 verified contacts**. Add each tester's number in the Sendblue
  dashboard before they text.
- **Contacts must text first.** Sendblue limits a contact that has not
  replied yet to 300 characters and 6 messages a day, and answers HTTP 400
  with error codes 4007-4010 past that limit. CareAgents only answers
  inbound texts, so in practice the contact has always written first. A
  refused send is logged with its code and a masked number. It is not
  retried.

## Settings

Set these on the **web** service and the **worker** service. The worker
sends the agent's answers, so a worker without these settings never sends
one.

| Variable | Value |
|---|---|
| `SENDBLUE_API_KEY_ID` | API key id (dashboard, API keys) |
| `SENDBLUE_API_SECRET` | API secret key |
| `SENDBLUE_WEBHOOK_SECRET` | The secret you set on the webhook in the dashboard |
| `SENDBLUE_FROM_NUMBER` | Our Sendblue line, E.164 (`+1...`) |
| `SENDBLUE_API_BASE` | Optional. Default `https://api.sendblue.co`. The docs also show `https://api.sendblue.com`. |
| `SENDBLUE_REAL_RECORDS` | Optional. Off by default. Leave it off until the HIPAA instance and BAA exist. |

The line is on only when the first four are all set. With any one missing,
the webhook answers 404 and the worker does not start the deliverer.

Settings shows the Sendblue number as the "text us" handle.
`CARE_IMESSAGE_HANDLE` still overrides what is shown, if set.

On Railway, the worker service reads the web service's variables by
reference (`docs/runbooks/careagents-durable-worker.md`, "Create the worker
service"). That list was taken once, when the worker was created. Add the new
names to the existing worker by reference:

```bash
railway variables --service careagents-worker \
  --set 'SENDBLUE_API_KEY_ID=${{web.SENDBLUE_API_KEY_ID}}' \
  --set 'SENDBLUE_API_SECRET=${{web.SENDBLUE_API_SECRET}}' \
  --set 'SENDBLUE_WEBHOOK_SECRET=${{web.SENDBLUE_WEBHOOK_SECRET}}' \
  --set 'SENDBLUE_FROM_NUMBER=${{web.SENDBLUE_FROM_NUMBER}}'
```

Use the web service's real name in place of `web`.

## The webhook

In the Sendblue dashboard, add a **receive** webhook:

- URL: `https://<careagents host>/api/surfaces/sendblue/webhook`
- Secret: the value of `SENDBLUE_WEBHOOK_SECRET`. Sendblue sends it in the
  `sb-signing-secret` header. A missing or wrong header gets 401.

What the webhook does:

1. It ignores outbound messages, group messages, and any webhook that is not
   a receive. Each of these gets a 200, so Sendblue does not retry them.
2. It records a hash of the `message_handle`. Sendblue retries up to three
   times on a 5xx. A retry of a message already seen gets a 200 and nothing
   else.
3. A message with only a picture gets "I can only read text for now."
4. Anything else goes to `imessage.handle_inbound`. A stranger gets a
   sign-in link; a paired number's text becomes a queued run. The webhook
   answers 200 once the turn is queued, never after the agent runs. Any
   immediate reply, and the typing indicator for a queued run, is sent from
   a background thread after the 200.
5. If the core raises, the webhook forgets the message and answers 500, so
   Sendblue's retry handles it again.

## Delivering the answer

The worker process owns delivery. Each queued run is a row in
`ca_sendblue_messages` (pointers only: the run id and the surface id). Every
two seconds the worker reads the rows not yet delivered:

- Finished run: the worker builds the text the relay would send (the agent's
  words, a review link for a form, the URL of a signed document), marks the
  row delivered, then sends. The mark comes first, so two workers never both
  send. A crash between the mark and the send loses that answer rather than
  sending it twice.
- Not finished after `max(180s, CARE_RUN_DEADLINE_SECONDS + 60s)`: the
  texter gets "That took too long..." once.
- The number sent STOP, or was disconnected on the web: nothing is sent.

A 429, a 5xx or a network error is retried once, after two seconds. After
that, the send is logged with a masked number and marked `failed`.

## Logs

Logs show a masked number (`***23`), HTTP statuses and Sendblue error codes.
They never show message content, a full number, or Sendblue's
`error_message`, which can repeat the number.
