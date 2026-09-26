# Quickstart: Grok (and Grok Bot)

Time: about 3 minutes. You need a Grok account that can add connectors.

## What you are doing

Grok is an AI helper made by xAI. A connector is a plug. It lets Grok use
HealthClaw's tools. HealthClaw is the guard between the AI and a health record.

This guide plugs Grok into our **practice record**. Every patient in it is
made up, so it is safe to try and safe to film.

## 1. Add the connector

1. Go to [grok.com/connectors](https://grok.com/connectors) and sign in.
2. Click **New Connector**, then **Custom**.
3. Paste this address:

   ```text
   https://mcp-demo-production-ee2c.up.railway.app/mcp
   ```

4. Name it `HealthClaw` and save. No password or key is needed.

On a work or school Grok account, an admin may have to allow the connector
first. If the button does nothing, ask them.

## 2. Try it

Start a new chat and type:

> What HealthClaw tools do you have? Then pick one of the practice patients
> and tell me about their health record in plain words.

Grok will use the tools and explain what it found. Want more? Try the
[10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent).

## 3. See the guard work

Ask Grok to add something to the record:

> Use fhir_commit_write to add a made-up blood pressure reading.

HealthClaw says no. Changing a record needs extra permission, and the demo
never gives it. That refusal is the guard doing its job.

## What Grok Bot means for this

Grok Bot uses the same connectors. Two things are different:

- All of your bots share one cloud computer. A connector you add is there for
  every bot.
- Grok Bot can review its own actions with "Auto Review". HealthClaw still
  checks every call on its side.

## Your own records

Not yet. This connector only reads the practice record. Real records need a
secure sign-in step that is still being switched on. Please do not paste
passwords, tokens or real health details into the chat.

## If something goes wrong

- **Grok asks for a Client ID or a sign-in:** check that the address ends in
  `mcp-demo-production-ee2c.up.railway.app/mcp`. The other server needs a
  key that Grok cannot send.
- **No tools show up:** start a new chat and ask again.
- **"Tool call failed":** the server may be waking up. Try once more.
