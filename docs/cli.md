# The CLI client

The `msks` command is a thin client for the daemon's `/api/v1` REST
surface. It runs from any host that can reach the daemon — a dev box,
a CI runner, the appliance itself — and every command authenticates
with the same bearer token the REST API uses.

The command set covers the operator loop:

```bash
msks ls                      # what exists, and what state is it in
msks create ws                # make a workspace
msks console ws               # boot it if needed, then work inside it
msks forward ws 22            # bridge a guest TCP port to stdio
msks key ws                   # fetch the workspace's minted ssh identity
msks home export ws           # download its /home volume (backup, seed)
msks start ws                 # boot it without attaching
msks stop ws                  # power it off
msks rm ws                    # delete it (and its data)
```

## Client environment

The client reads three environment variables. They are prefixed
`MSKSC_` (client) to stay apart from the daemon's `MSKSD_*` (server)
namespace — a box that runs both can export each side independently.

| Variable       | Meaning                                           | Default                  |
| -------------- | ------------------------------------------------- | ------------------------ |
| `MSKSC_URL`    | The daemon's base URL                             | `https://127.0.0.1:8660` |
| `MSKSC_TOKEN`  | A daemon bearer token (see tokens below)          | — (required)             |
| `MSKSC_CAFILE` | A PEM file to verify the daemon's TLS certificate | unverified with warning  |

A missing `MSKSC_TOKEN` is an error before any network activity: the
client names the variable and exits. Tokens come from the daemon:
`POST /api/v1/tokens` mints one, and the appliance writes its
bootstrap token to `.appliance/bootstrap-token` on first boot —

```bash
export MSKSC_URL=https://192.168.77.2:8660
export MSKSC_TOKEN=$(cat .appliance/bootstrap-token)
```

The daemon serves TLS with a self-signed certificate. Point
`MSKSC_CAFILE` at the daemon's CA (`msks-ca.pem` under its state
directory) and the client verifies the certificate chain. Without
`MSKSC_CAFILE` the client proceeds unverified and prints a warning to
stderr on every invocation — the same trust-on-first-use posture as
`msks console` (#21), fine for a lab network and worth closing before
anything real.

## `msks ls`

Prints one line per workspace the daemon knows, aligned in four
columns: id, status, image hash (first 12 hex chars), and owning host.

```text
$ msks ls
my-workspace             running   9f2c41ab77de   hv-1
scratch                  created   -              hv-1
```

The status column speaks the daemon's lifecycle vocabulary —
`created` (row exists, never booted), `starting`, `running`,
`paused`, `stopped`, `unknown`, `absent`. A `-` in the image column
means the workspace boots explicit kernel/rootfs paths instead of
a catalog image.

`--json` replaces the table with one JSON document — the API's
workspace rows verbatim (id, kernel, initrd, rootfs, cmdline, cpus,
mem_mib, image_hash, host, root_mib, home_mib, status, created_at):

```bash
msks ls --json | jq -r '.[] | select(.status == "running") | .id'
```

A daemon with zero workspaces prints nothing (an empty table) and an
empty JSON array under `--json`.

## `msks create`

POSTs the API's create body. The positional id follows the daemon's
workspace charset — lowercase letters, digits, and dashes, starting
with a letter or digit, up to 64 chars (it becomes a directory name
under the state dir and a pod name on k8s).

Flags map one-to-one onto the create request's fields:

| Flag          | API field   | Meaning                                                     |
| ------------- | ----------- | ----------------------------------------------------------- |
| `--image`     | `image`     | Catalog ref: `name:version`, bare name, or hash             |
| `--kernel`    | `kernel`    | Explicit kernel path (skips the catalog)                    |
| `--initrd`    | `initrd`    | Explicit initrd path                                        |
| `--rootfs`    | `rootfs`    | Explicit rootfs path (skips the catalog)                    |
| `--cmdline`   | `cmdline`   | Explicit kernel cmdline                                     |
| `--cpus`      | `cpus`      | vcpus, 1–64 (daemon default: 2)                             |
| `--mem-mib`   | `mem_mib`   | Guest memory MiB, 64–32768 (daemon default: 1024)           |
| `--root-mib`  | `root_mib`  | Persistent root overlay size (daemon default)               |
| `--home-mib`  | `home_mib`  | Persistent /home volume size (daemon default)               |
| `--user-data` | `user_data` | First-boot provisioning payload file; `-` reads stdin (#41) |

Only the flags you pass are sent — unset flags let the daemon apply
its own defaults. An `--image` reference resolves against the
daemon's image catalog (`docs/images.md`); explicit `--kernel` and
`--rootfs` bypass it. Size ranges are enforced server-side; a value
outside them comes back as a validation error (below).

`--start` boots the workspace right after creating it:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace
attach with: msks console my-workspace
```

The id prints as soon as the create succeeds and before the boot is
attempted. A failed boot still leaves the workspace created — the
error message says so and names the recovery command:

```text
created my-workspace
msks: 503: vmm launch failed
msks: my-workspace is created; boot it later with: msks start my-workspace
```

Creating without `--start` prints the id and exits; boot it whenever
with `msks start` — or just `msks console` it: the console command boots
a not-running workspace on its own (below).

`--user-data` is the first-boot provisioning hook (#41): the file's
contents travel to the daemon and run once on the
workspace's first boot (see `docs/images.md` for the seed-disk
mechanism, the payload forms each image provisioner accepts, and
the create-time immutability). It composes with `--start`:

```bash
$ printf '#!/bin/sh\napt-get update\n' | msks create ws --user-data - --start
created ws
attach with: msks console ws
```

## `msks start`

Boots one created workspace (`POST /api/v1/workspaces/{id}/start`)
and prints the result:

```bash
$ msks start my-workspace
my-workspace running
```

The start request answers after the VMM finishes booting — a few
seconds on an idle host, longer under load. The client waits up to
two minutes before reporting a timeout, and a timeout message notes
that the daemon may still finish the boot.

## `msks stop`

Powers one running workspace off (`POST /api/v1/workspaces/{id}/stop`)
and prints the result, mirroring `msks start`:

```bash
$ msks stop my-workspace
my-workspace stopped
```

The stop asks the guest for a graceful, deadline-bounded power-off —
the daemon presses the ACPI power button and the guest's systemd runs
a full shutdown — so the write-out can take a moment. The deadline is
the daemon's (`MSKSD_SHUTDOWN_TIMEOUT_S`); there is no client-side
timeout flag, and like `msks start` the client waits at most two
minutes on the answer. A stop that misses the daemon's deadline
answers 503 with the endpoint's detail on one line, exit non-zero.
Stopping a workspace that is already stopped is a no-op success: the
daemon reports it stopped either way, and the client asks without
pre-filtering on local state — the same holds for a workspace that
was never booted (the row records `stopped` without a launch ever
happening). The data survives the stop — the root overlay and the
`/home` volume come back on the next `msks start`.

## `msks rm`

Deletes workspaces (`DELETE /api/v1/workspaces/{id}`) — the row, the
VMM (stopped first, killed if wedged), and the persistent artifacts:
the root overlay and the `/home` volume. The data does not come back;
recreating a workspace with the same id starts from the image's
pristine root. One id or several:

```bash
$ msks rm my-workspace
my-workspace deleted
$ msks rm scratch-1 scratch-2
scratch-1 deleted
scratch-2 deleted
```

Ids are removed one at a time, in the order given; a failure stops
the run there with the API's one-line error, and the ids already
removed stay removed (each success printed its confirmation line).
There is no confirmation prompt — deleting is what `rm` means, and a
workspace is recoverable by recreating it. A workspace recorded on
another host answers 409 with the host mismatch named in the error,
like every lifecycle command.

## `msks image`

Manages the daemon's image catalog — the surface `docs/images.md`
documents over HTTP, as CLI subcommands. Every subcommand uses the
same client environment as the workspace commands.

### `msks image ls`

One line per registered image — reference, hash (first 12 hex
chars), the default designation, and the kernel facts:

```text
$ msks image ls
debian:13                9f2c41ab77de  default  6.12.107+deb13 (raw)
alpine:3.20              33aa9db1c4ef  -        6.12.7 (raw)
```

The image the daemon designates as default carries the `default`
flag; a bare `msks create` resolves to it. `--json` prints the
listing as the API returns it (`GET /api/v1/images`), stable for
scripting.

### `msks image import`

Registers an archive in the catalog (`POST /api/v1/images`). The
path is a **daemon-side** path: the daemon reads the file from its
own filesystem (an appliance reaches host files through its
virtiofs share) — the command does not upload anything:

```text
$ msks image import /srv/images/debian-13.tar
imported debian:13 (9f2c41ab77de)
```

The first image imported into an empty catalog also becomes the
daemon's default. An archive the daemon cannot read or parse
answers 400 with the reason on one line.

### `msks image rm`

Removes an image from the catalog. The reference accepts every form
the daemon resolves for workspace create — `name:version`, a bare
name (its newest version), `name@hash` (the full 64-hex hash), a
full hash — and a unique hash prefix (the 12 chars `image ls`
prints):

```text
$ msks image rm debian:12
debian:12 deleted
```

The removal is keyed by the image's hash after resolving the
reference against the listing. An image a workspace still boots is
refused — the API's 409 names the workspace — and a reference that
matches nothing exits with the catalog spelled out so the next try
can be copy-pasted. An ambiguous hash prefix names the images it
matches; use the full hash or `name@hash`. (Two imports of the same
`name:version` — a rebuilt archive — are the usual ambiguity, and
only the hash forms still identify one of them.)

### `msks image info`

Prints one image's full record — reference, hash, kernel facts,
cmdline, the console's vsock port, and the default designation —
from the same listing data:

```text
$ msks image info debian:13
ref      debian:13
hash     9f2c41ab77de0000000000000000000000000000000000000000000000000000
kernel   6.12.107+deb13 (raw)
cmdline  console=hvc0 root=/dev/vda rw
console  vsock port 1073741826
default  yes
```

The reference forms are the same as `image rm`'s.

## `msks home`

Moves a workspace's `/home` volume through the daemon (#80) —
backup, migration to another daemon, seeding a fresh workspace with
data — as the two byte-stream endpoints `docs/storage.md` documents:

```bash
msks home export my-workspace              # writes my-workspace.ext4
msks home export my-workspace other.ext4   # names the output file
msks home export my-workspace - | gzip > backup.ext4.gz
msks home import fresh-ws my-workspace.ext4
msks home import fresh-ws - < backup.ext4
```

The workspace must be stopped (a volume under a running VM answers
`409` — stop it first; `msks stop` is enough). A boot that arrives
during a move waits for it and boots the volume the move left
(moves and boots serialize per workspace). Export streams the
volume file's bytes verbatim and prints one confirmation line
(`exported my-workspace (2097152 bytes) to my-workspace.ext4`);
`-` writes the bytes to stdout and moves the note to stderr, so a
pipe stays clean for gzip or ssh — and a reader that goes away
mid-stream (`| head`, a compressor on a full disk) prints one line
on stderr and exits non-zero. Import uploads the named ext4
image (or stdin, for `-`), the daemon replaces the volume with it,
and the reply is the byte count: `imported 2097152 bytes into
fresh-ws`.

The daemon refuses — one line, exit 1 — a file that is not an ext4
image (`msks: 400: the request body is not an ext4 image ...`), a
workspace in the wrong state, and everything the API's refusals
name (a foreign host, the k8s backend). An import keeps the
workspace's existing volume until the upload completes and passes
the ext4 check; a cut-off upload changes nothing.

The typical pairings: backup (`export`, later `import` back into
the same workspace after a `rm` + `create`), migration (`export` on
one daemon, `create` + `import` on another), and seeding (build an
ext4 image with the data a fleet of workspaces starts from, import
it into each fresh one). Inside a workspace with egress, git and
rsync over the forward (`docs/networking.md`) carry day-to-day
code; the volume moves are for the whole `/home` at once.

## `msks console`

An interactive shell inside a workspace, over the daemon's console
websocket. The command boots the workspace first when the daemon
reports it as not running — the notices print on stderr while the
boot runs, then the session attaches:

```text
$ msks console my-workspace
msks: my-workspace is stopped; starting it
msks: my-workspace running
(workspace prompt)
```

The session runs as **root** by default. `--user` requests another
identity — the image's workspace user — and the guest helper
negotiates it in-band (#63): a user the image does not serve is
refused by name before any shell starts, and the session's terminal
geometry rides the same request (the guest pty matches the client's
size at attach):

```text
$ msks console my-workspace --user msks
(workspace prompt, as the workspace user)
```

The guest pty keeps the size it was given at attach for the
session's life: the console stream is a raw byte pipe, so a window
resized mid-session does not reach it — reconnect for the new size.
Full-screen work (editors, tmux) belongs to an ssh session through
the forward (#108–#112): ssh's window-change channel resizes its
pty live.

A workspace that is already running attaches with no preamble. Two
states get special handling:

- **`starting`** — another client's boot is in flight. The console
  waits for it (polling up to two minutes) and attaches when it
  lands, instead of racing a second boot into the daemon's
  double-launch guard.
- **`paused`** — refused with the honest reason: the daemon has no
  resume, so the message names the recovery (`msks stop` it, then
  `msks start` again).

A start that loses a race — the daemon reports `stopped`, another
client boots it in the gap — re-checks and attaches to the winner.

Ctrl-] detaches and leaves the workspace running; Ctrl-C and Ctrl-D
reach the guest. To type a literal Ctrl-] into the guest, press it
twice quickly (the second press within 50 ms): the pair delivers one
Ctrl-] byte. A single Ctrl-] — or one followed by any other byte —
detaches, and the byte that followed the escape is consumed with it,
so a paste that happens to contain a lone Ctrl-] detaches the session.
Large pastes travel as a few websocket frames (4,096-byte chunks), not
one frame per byte. The session needs a tty on both stdin and stdout.
See the README's workspace-console section (#21) for the transport
story.

A session whose guest stream stops carrying bytes while input keeps
flowing is closed by the daemon after `console_stall_timeout_s`
(`MSKSD_CONSOLE_STALL_TIMEOUT_S`, 60 s default): the guest pty echoes
every input byte, so that silence names a wedged stream, and the
client exits with `console stalled (guest stream wedged; reconnect
for a fresh session)` — reconnecting opens a fresh shell in the same
workspace. An idle session stays open indefinitely: the clock runs
only while client input is waiting for its echo (#103). Two caveats:
a program that reads with echo off (`read -s` password prompts, `su`)
also draws no echo, so a prompt left waiting past the window closes
the session — set the timeout higher or to `0` (off) for such
workflows. The deadline is anchored at the first unanswered input, so
continuous sending into a dead console still gets the named close one
window later. A stream that wedges while the helper still holds
undelivered output is closed by the helper's own teardown after
300 s (without the 4502 name); a stream that wedges fully idle stays
open — nothing is in flight to time — until the next input arms the
daemon's clock.

## `msks forward`

A guest TCP port on this command's stdio — the pipe ssh's
ProxyCommand expects — over the daemon's forward websocket. The
command boots the workspace first when the daemon reports it as not
running (the same notices as `msks console`), then bridges bytes
unexamined in both directions: a tty is not required, and binary
protocols (ssh, rsync) ride it cleanly:

```bash
msks forward my-workspace 22                 # stdio: ProxyCommand shape
msks forward my-workspace 8080 --local 8080   # loopback listener
```

`--local PORT` binds `127.0.0.1:PORT` instead of stdio; every
accepted connection opens its own forward websocket, so parallel
clients (a browser and a curl, two ssh sessions) are independent
sessions. Refusals print one line — the daemon's close codes name the
cause (no NIC, not running, service not listening, still booting) —
while a clean end of stream (the guest service closed, stdin EOF)
leaves exit code 0.

The forward authenticates with the `Authorization` header (the same
Bearer form as the REST surface), not the query string: URLs land in
proxy and process logs, headers do not. Only egress workspaces have
a NIC to forward to — a workspace created `--no-egress` is refused
with the reason naming it. `forward.opened` and `forward.closed`
events appear on the daemon's events channel for every session.

## `msks key`

The workspace's minted ssh identity (#111): every workspace a
local-backend daemon creates carries
a keypair msksd created at create-time, whose public half the
guest's first boot planted into `authorized_keys` for root and the
`msks` workspace user. The fetch takes the same
`MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` environment as every other
command:

```bash
msks key my-workspace                    # the public authorized_keys line
msks key my-workspace --private          # the private half, on stdout
msks key my-workspace --out ~/.cache/msks/my-workspace.key
```

`--out FILE` writes the private half with mode 0600 and prints
nothing but the path; the mode is forced on an existing file too.
The path is always the operator's choice: the command writes the
key to the file the operator named and to stdout, and nowhere else.
A shell redirect (`msks key my-workspace --private > f`) keeps the
shell's own umask — that is what `--out` is for. A workspace that
predates #111 answers 404 with "no minted identity"; the key type is
the daemon's `MSKSD_SSH_KEY_TYPE` setting (ECDSA P-256 by default).
Both halves persist across daemon restarts and workspace stop/start.

## `msks ssh`

Stock ssh into a workspace over the forward, with the minted
identity staged in memory (#112) — one command, no key steps:

```bash
msks ssh my-workspace              # as the msks workspace user
msks ssh my-workspace -- -l root   # the recovery login
msks ssh my-workspace -- -A        # forward the session agent (the workspace identity)
msks ssh my-workspace -- -L 8080:localhost:80
```

The command boots the workspace first when the daemon reports it as
not running (the same notices as `msks console`), fetches the
identity over the authenticated API, and runs `ssh` with the
forward websocket as its ProxyCommand (`msks forward <ws> 22`). The
private half never becomes a file: a transient in-process ssh-agent
holds it in memory for the session, ssh names the identity by its
public half (`-i`, public material only) and signs through the
agent socket — the key material goes away with the process, and a
crash leaves no private material behind. Host keys land in a per-workspace
`known_hosts` under the msks cache root (XDG_CACHE_HOME, else
`~/.cache/msks`, then `<ws>/known_hosts`) under `accept-new`; they
persist across stop/start on the workspace's
overlay, so the first-connection entry keeps matching. (The alias block keeps
its own known_hosts under `~/.cache/msks/msks-<ws>/` — the two
paths record the same host key independently.)

Everything after the workspace id (the `--` is optional — any
argument ssh would take works verbatim) is passed to ssh. A
passthrough that starts with a plain word is a remote command
(`msks ssh my-workspace -- uname -a`); when it starts with an
option, ssh's own separator carries a command after the options
(`msks ssh my-workspace -- -A -- uname -a`). `-A` forwards the
session agent — the guest can sign as the workspace identity
(useful for nested logins to the same workspace); forwarding your
own agent — git credentials for `git push` from inside — is the
alias path's job, where your real `SSH_AUTH_SOCK` rides untouched.
The exit code is ssh's own (255 for ssh failures), not the msks
command set. The session agent lives exactly as long as the
`msks ssh` process — `ControlPersist`/mux sessions that outlive it
belong to the alias path, whose identity is a key file. Explicit
passthrough options override the injected defaults (your own
`UserKnownHostsFile`, a different `ProxyCommand`) exactly as with
stock ssh: ssh takes the first value a repeated option receives,
and the passthrough comes first. The login user is the image's
`msks` workspace user by default; ssh arguments that name a user
(`-l root`, `-o User=root`) override it. The host argument ssh
sees is the workspace id itself — the transport is the proxy, so
the name never resolves. The ProxyCommand runs this very
client (the absolute interpreter and module form — `msks` need not
be on the ssh child's PATH), and it inherits your environment:
`MSKSC_URL`, `MSKSC_TOKEN`, and
`MSKSC_CAFILE` must be set where ssh runs (see the
networking chapter's alias workflow for the `Host msks-*`
configuration that hides all of this).

## Errors, exit codes, and timeouts

Every command fails with one readable line on stderr and exit code 1
— never a traceback:

```text
msks: set MSKSC_TOKEN to a daemon token (MSKSC_URL for a non-default daemon)
msks: cannot reach https://192.168.77.2:8660: [Errno 113] ...
msks: 401: invalid or revoked token
msks: 409: workspace exists
msks: 422: body.id: String should match pattern '^[a-z0-9][a-z0-9-]*$'
```

The status lines carry the daemon's `detail` field verbatim. A
validation failure (422) reports each problem as `field: message`,
joined on one line — the same facts the API returns, minus the JSON
scaffolding. A timeout says so explicitly, because the daemon may
still complete a request the client stopped waiting for. Argument
errors exit with code 2 (argparse convention); success is 0; a
Ctrl-C during a long boot prints `msks: interrupted` and exits 130.
