# Quickstart: Meta Muse

Time: about 5 minutes. You need a Meta Muse account.

## What you are doing

Muse is an AI helper made by Meta. Muse has no "add connector" button yet.
Instead, you ask Muse to build the connection for you, and it does.

HealthClaw is the guard between the AI and a health record. This guide
connects Muse to our **practice record**. Every patient in it is made up, so
it is safe to try and safe to film.

## 1. Ask Muse to connect

Start a chat in Muse and send this message:

> Build a custom integration to HealthClaw. Its MCP server URL is
> https://mcp-demo-production-ee2c.up.railway.app/mcp. It needs no key or
> login. When it works, list the tools it has.

Muse will set it up and try each tool. That can take a minute.

If Muse asks for a key, say it needs none. Never type a real password or key
into the chat. If a service ever needs one, Muse opens a separate secure box
for it.

## 2. Try it

Once Muse lists the tools, send:

> Using HealthClaw, pick one of the practice patients. Tell me about their
> health record in plain words.

Want more? Try the
[10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent).

## 3. See the guard work

Ask Muse to change the record:

> Using HealthClaw, add a made-up blood pressure reading with
> fhir_commit_write.

HealthClaw says no. Changing a record needs extra permission, and the demo
never gives it. That refusal is the guard doing its job.

## Good to know

- Meta does not check custom connections that users build. That is why we
  only point Muse at the practice record today.
- Muse runs in Meta's cloud. It can reach our server because the server is
  on the public internet.
- Meta also has a connector directory. HealthClaw is not listed there yet.

## Your own records

Not yet. This connection only reads the practice record. Real records need a
secure sign-in step that is still being switched on. Please do not paste
passwords, tokens or real health details into the chat.

## If something goes wrong

- **Muse says it cannot reach the server:** check the address. It must end in
  `mcp-demo-production-ee2c.up.railway.app/mcp`.
- **Muse asks for a sign-in or Client ID:** you may have the other server's
  address. That one needs a key that Muse cannot send.
- **"Tool call failed":** the server may be waking up. Ask Muse to try again.
