# The screenshot pipe

A skill running inside a workspace guest can capture one of your screens and
read the result, the way a host-side agent skill does. The pipe is the path:
a loopback TCP port inside the guest that, through an ssh reverse forward,
reaches a capture command running on the client that started the workspace.

The pipe is client-held. The holder process that serves a workspace's
forward proxies also keeps one `ssh -N -R` child. That child asks the
guest's own sshd to bind a loopback listener, and each connection to it
becomes one connection to a unix socket the holder owns on your machine.
The daemon relays the ssh bytes and holds no part of the capture: it serves
no endpoint for this, and the workspace's network chain stays exactly as it
was.

## Enabling the pipe

One setting decides: `screenshot_command`, a global-only client config key.
Its value is the capture command the pipe runs. Set it in `msks.yaml`:

```yaml
screenshot_command: import -window root -crop 3840x2160+0+0
```

The variable `MSKSC_SCREENSHOT_COMMAND` carries the string form for one
shell and overrides the file. A leading `~` in the command's program
expands to your home directory, the way the shell would take it. The value
can also be a list, with the tokens kept whole:

```yaml
screenshot_command:
  - import
  - -window
  - root
  - -crop
  - 3840x2160+0+0
```

The setting belongs to the operator, not to a daemon connection: a
per-alias `screenshot_command` is refused, the same rule `identity_file`
carries. The screen the pipe captures is this client's desktop, wherever
this client runs.

The command's presence is the whole enablement. A client with the setting
unconfigured spawns no pipe, and a workspace started from it leaves the
guest's port refused. Every start from a configured client brings the pipe
up, beside the bridge holder the start already leaves for forward proxies.

The guest-side port is a fixed default, `127.0.0.1:8662`, and
`screenshot_port` overrides it (global-only, same rule). The holder
writes the configured port to `/tmp/msks-screenshot-port` in the
guest, and [the guest's pi](#the-guests-pi) reads it from there. If
something in the guest already holds the port, the holder reports the
refusal on its stderr as a start-time failure and keeps retrying at a
slow pace; the capture path answers only when the bind lands. The
report reaches the
terminal of a holder you run by hand (`msks screenshot hold`, `msks proxy
hold`). A holder the start path left in the background writes it to no
terminal. The guest's refused connection is the symptom you see.

## Holding the pipe on demand

`msks start` leaves the pipe up when it leaves a holder: a configured
client, a start, and the pipe serves after the command exits. The pipe
also serves while any holder stands, and `msks proxy hold <workspace>`
brings one on demand.

The pipe's own command:

```bash
msks screenshot hold <workspace>
```

It runs the same holder (the bridge and the pipe serve together) and
refuses first, with the remedy spelled out, when this client carries no
`screenshot_command`.

The pipe's lifetime is the holder's, and the holder's is the ssh session
it holds. A stopped workspace takes the pipe down with the bridge; the
guest's next connection to the port is refused until a configured client
starts the workspace again or someone runs the hold command. A holder
that dies on its own comes back only through the next start or a manual
hold: the automatic re-hold an open client runs covers forward proxy rows
alone. A stop signal to a running holder (SIGTERM, or the terminal a
manual holder runs in closing) unwinds it cleanly: the guest's listener
and the client's socket file go with it. A holder killed outright
(`kill -9`) leaves the socket file behind. The runtime directory's own
lifetime sweeps it.

## The guest's contract

Inside the guest, the pipe is one loopback HTTP endpoint. The request is a
plain GET, and the answer is the capture command's stdout:

```bash
curl --fail-with-body --max-time 30 \
  'http://127.0.0.1:8662/screenshot?monitor=0' -o /tmp/screenshot.png
```

`monitor` is the only parameter. It selects the screen to capture, with
`0` the default. The whole number passes to the capture command as the
`MSKS_SCREENSHOT_MONITOR` variable, so the command's argv is exactly what
the config spelled.

`--fail-with-body` matters: plain `curl -f` discards error bodies, and the
body is where every remedy below travels. `--max-time` bounds a hung
capture command; the helper also kills one itself after 60 seconds.

The answers a request can fail with:

| the skill sees     | means                                                                  | the body carries                                                                                                                                |
| ------------------ | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| connection refused | no holder serves the pipe, or no command is configured                 | nothing: the skill's own remedy line names both: start the workspace from a client with `screenshot_command` set, or run `msks screenshot hold` |
| `400`              | a malformed request                                                    | the rule and the valid spellings                                                                                                                |
| `500`              | the capture command failed                                             | its stderr tail, verbatim, or the timeout                                                                                                       |
| `503`              | the client is already running the most captures it runs at once (four) | one line: retry                                                                                                                                 |

A refused connection covers two causes because the guest cannot tell them
apart: nothing is listening in both. The skill prints its remedy line and
the operator picks the fix. The guest's pi reaches this endpoint through
its `screenshot` tool; see [The guest's pi](#the-guests-pi).

## The guest's pi

The guest images ship a pi extension beside the model-discovery one:
`~/.pi/agent/extensions/msks-screenshot/`, copied into every account's
home from the skeleton. It gives the agent in the workspace two ways to
capture your screen through the pipe.

### The `screenshot` tool

The agent calls `screenshot` and receives the capture as an image in one
tool call. The optional `monitor` parameter selects the screen, `0` by
default. The full-resolution capture is saved first, in a directory this
account alone reads, and the tool's text gives its path. pi downsamples
an image before the model sees it. Small text on a 4K capture needs a
`magick` crop and a second `read` of that file.

### Who sees the capture

The proxy's model list carries no modality data. The `llm-models.ts`
extension therefore registers every proxy model with image input, and the
active model receives the capture directly. A text-only upstream answers
the image with an error. That error is the signal to pick a vision model.

`/screenshot model` opens a picker over the model list. The choice is
stored under the `msksScreenshotVisionModel` key in
`~/.pi/agent/settings.json` and survives a restart. With a vision model
picked and a different model active, the tool sends the capture to the
vision model in one nested call and returns its description as text.
The optional `prompt` parameter says what to look at. The active model
sees the capture directly when no vision model is picked, or when the
picked model is the active one.

### The `/screenshot` command

`/screenshot` captures monitor 0, and `/screenshot <monitor>` captures
that screen. The capture, or the vision model's description, goes in
front of the agent as a message and a turn starts on it. A failed
capture shows the pipe's answer as a notice instead.

### The port in the guest

The holder writes the configured `screenshot_port` to
`/tmp/msks-screenshot-port` when the pipe comes up, and the extension
reads the file. The default `8662` answers when the file is absent. The
holder writes a fresh file and moves it over the path, so a reader sees
the whole port or the previous file. A stale file is harmless while the
ports agree: with no holder the port is refused either way, and the next
hold rewrites the file. Two holders on one workspace with different
`screenshot_port` values are a misconfiguration: the file carries the
port of the hold that landed last, and the other holder's pipe is not
the one the tool reaches.

## The capture command's contract

The command runs on the client, once per request, in a fresh process. Its
stdout is the answer's body, whatever bytes it writes (an image, in
practice). Its exit code decides the response: zero answers `200`, anything
else answers `500` with the command's stderr tail.

The helper that runs the command passes it the whole holder environment,
so a desktop capture finds `DISPLAY`, `WAYLAND_DISPLAY`, and `XAUTHORITY`
already set: the holder inherits the shell that started the workspace.
Two variables join them:

- `MSKS_SCREENSHOT_MONITOR` — the request's monitor number.
- `MSKS_SCREENSHOT_WORKSPACE` — the workspace's name, set by the holder
  from its own records, useful for a notification that says who captured.

The command's argv comes from the config alone. Nothing from the request
reaches the command line: the monitor number is validated to a whole
number first, and it travels as a variable. A command that wants to be
told which screen to grab reads `MSKS_SCREENSHOT_MONITOR` and picks its
own geometry.

One exchange runs one process, and the process tree ends with it: the
helper exits after the response, and a failing or leaking capture command
costs exactly one request. A capture that writes more than 64 MiB, or
runs past 60 seconds, is refused with a `500` naming the limit.

The client runs at most four captures at once. A fifth concurrent request
answers `503`, written by the holder itself without spawning anything:
a guest that floods the port gets cheap refusals, not a pile of capture
commands on the desktop. A connection that sends no request head answers
nothing and holds no slot past 30 seconds. A SIGTERM'd helper takes its
running capture command with it, so stopping the holder leaves no capture
(a blocking prompt among them) running on.

## Recipes

Nothing ships as a default command. An unconfigured client is the shipped
state, and the first refused connection from a guest is the onboarding
path: it names the setting. These recipes are the copy-paste starts.

X11, one fixed monitor (a 4K left screen):

```yaml
screenshot_command: import -window root -crop 3840x2160+0+0
```

Wayland (KDE), the same idea through spectacle:

```yaml
screenshot_command: spectacle -b -n -o /dev/stdout
```

macOS, where the client is the Mac and `screencapture` serves:

```yaml
screenshot_command: screencapture -x -
```

Per-capture visibility, any platform with `notify-send`: a wrapper script
in the config's value. The pipe tells the wrapper which workspace asked
through `MSKS_SCREENSHOT_WORKSPACE`:

```bash
#!/bin/sh
notify-send "msks screenshot" "$MSKS_SCREENSHOT_WORKSPACE captured the screen"
exec import -window root -crop 3840x2160+0+0
```

A blocking prompt, for a pipe you want to answer by hand per capture:
the same wrapper with `zenity` in place of the notification, exiting
nonzero when the operator declines (the guest then reads the `500`).

## The audit trail

The daemon holds no part of this feature, so its events feed and the TUI
show nothing of captures. The record is one JSON line per command run,
appended to `screenshots.jsonl` under the client state root
(`MSKSC_STATE_DIR`, else `~/.local/state/msks`):

```json
{
  "ts": "2026-12-01T18:22:04.512345+00:00",
  "workspace": "dev",
  "monitor": 0,
  "status": "ok",
  "bytes": 821349
}
```

The `status` is `ok` for a capture that answered `200` and `error` for one
that answered `500`. A malformed request writes nothing. The lines are the
whole history: who captured, when, and how big.

## The trust shape

The capability is the holder's session and nothing wider. The guest's sshd
binds the listener, so the guest holds a port it could bind anyway; the
client's ssh session reaches in, not out; and the only thing the grant
gives the guest is one fixed-argv exec whose stdout flows back. Stopping
the holder removes the guest's listener with it.

Anything in the guest can reach the port, and every guest caller looks
alike to the client. The audit line is the record; the notify wrapper
above is the live signal; and a command that prompts is the gate, for a
pipe an operator wants to answer per capture. The images pin
`AllowTcpForwarding` in their sshd config so the reverse forward the pipe
asks for is a contract, not an OpenSSH default.
