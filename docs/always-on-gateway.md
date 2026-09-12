# Keeping the gateway running on macOS

The getting-started skill points here for the always-on setup: a gateway that
comes back after a reboot and does not stop when the machine sleeps or the
terminal that started it closes. Nothing below is specific to one machine, and
none of it is required to try the project — a foreground `openclaw gateway
start` is enough for that.

Two separate problems, and they need different answers.

## 1. Start it at login, and restart it if it exits

macOS runs per-user background jobs through **launchd**. A LaunchAgent is a
plist in `~/Library/LaunchAgents/` that launchd reads at login.

Write `~/Library/LaunchAgents/com.example.openclaw-gateway.plist`, replacing
the label, the binary path (`which openclaw`) and the log paths with your own:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.example.openclaw-gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/openclaw</string>
    <string>gateway</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/tmp/openclaw-gateway.log</string>
  <key>StandardErrorPath</key><string>/tmp/openclaw-gateway.err</string>
</dict>
</plist>
```

Load it, and check it took:

```
launchctl load -w ~/Library/LaunchAgents/com.example.openclaw-gateway.plist
launchctl list | grep openclaw     # a PID in the first column means running
curl -sS http://localhost:4319/    # the gateway's own port
```

`KeepAlive` restarts the process when it exits, which is what you want for a
gateway and not what you want for a one-shot job.

Two things that catch people out. **launchd does not read your shell profile**,
so a binary found by your interactive `PATH` may not be found here — give an
absolute path, and pass any variable the process needs through an
`EnvironmentVariables` dict rather than assuming it is inherited. And **a
LaunchAgent runs only while that user is logged in**; a Mac that reboots to the
login window runs nothing until somebody signs in. Enable automatic login, or
use a LaunchDaemon instead and accept that it runs as root.

## 2. Stop the machine sleeping out from under it

A logged-in Mac still sleeps, and a sleeping Mac answers nothing.

```
caffeinate -dimsu &                 # keep display, disk and system awake
```

Better, tie it to the process rather than leaving it running forever:

```
caffeinate -is /opt/homebrew/bin/openclaw gateway start
```

`caffeinate` then exits when the gateway does. In a LaunchAgent, make
`caffeinate` the program and the gateway its arguments, so the two are one
supervised unit.

For a machine that is meant to stay up, also turn off the system's own idle
sleep: **System Settings → Energy** (or `sudo pmset -a sleep 0` on a desktop).
Laptops on battery will still sleep on lid close whatever you set here.

## Checking it actually survives

Reboot the machine and, without opening a terminal window, run the health
check from another device on the same network. A setup that only works while
you are watching it is the failure this page exists to prevent.

## Related

- [`skills/getting-started/SKILL.md`](../skills/getting-started/SKILL.md) — the
  full first-run walkthrough this page is a footnote to
- [`docs/development.md`](development.md) — running the app itself
