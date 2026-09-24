# The CLI client

The `msks` command is a thin client for the daemon's `/api/v1` REST
surface. It runs from any host that can reach the daemon — a dev box,
a CI runner — and every command authenticates
with the same bearer token the REST API uses.

The command set covers the operator loop:

```bash
msks ls                      # what exists, and what state is it in
msks create ws                # make a workspace
msks console ws               # boot it if needed, then work inside it
msks forward ws 22            # bridge a guest TCP port to stdio
msks key ws                   # fetch the workspace's minted ssh identity
msks rsync ws -- -av ./src/ root@:/src/  # copy files over the forward
msks home export ws           # download its /home volume (backup, seed)
msks start ws                 # boot it without attaching
msks stop ws                  # power it off
msks rm ws                    # delete it (and its data)
```

## Client environment

The client reads six environment variables. They are prefixed
`MSKSC_` (client) to stay apart from the daemon's `MSKSD_*` (server)
namespace — a box that runs both can export each side independently.

| Variable               | Meaning                                                                                                                           | Default                  |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `MSKSC_URL`            | The daemon's base URL                                                                                                             | `https://127.0.0.1:8660` |
| `MSKSC_TOKEN`          | A daemon bearer token (see tokens below)                                                                                          | — (required)             |
| `MSKSC_CAFILE`         | A PEM file to verify the daemon's TLS certificate                                                                                 | unverified with warning  |
| `MSKSC_EXPECTED_IMAGE` | An image reference the operator sets; `msks ls` compares it with the image the daemon reports in `/health` and names drift (#160) | unset (no check)         |
| `MSKSC_CACHE_DIR`      | The directory per-workspace host-key caches live under (#251); the per-workspace directories are created below it                 | `~/.cache/msks`          |
| `MSKSC_DATA_DIR`       | The directory client-minted workspace identities live under (#251); same naming rule                                              | `~/.local/share/msks`    |

The two directory variables are separate because their contents
differ in durability: the host-key cache is disposable (a swept
cache costs one trust-on-first-use re-pin), while the minted
private halves have no other copy — losing one loses ssh to that
workspace. Pointing the cache at a per-project, disposable
location and the identities at somewhere durable is the intended
use; one variable for both would tie their lifetimes together.

Each names its directory directly — the per-workspace directories
are created below it — and takes an absolute path (a relative
value is refused with a line naming the fix; a leading `~`
expands; an empty value counts as unset).

A missing `MSKSC_TOKEN` is an error before any network activity: the
client names the variable and exits. Tokens come from the daemon:
`POST /api/v1/tokens` mints one, and the devenv environment presets
all three from the worktree's dev-daemon state dir,
`.devenv/state/msksd/` (token + CA) — once the dev daemon has
served once, a fresh devenv shell needs no exports. The same shell
presets the two directory variables at the worktree's own
`.devenv/state/msksc/` (cache under `cache/`, identities under
`data/`), so each checkout's client state stays its own — one
tree's cache entry is never read by another (#251); a non-empty
value exported before entering the shell survives the preset, and
an empty one counts as unset. Workspaces created before the preset
keep their minted halves under the previous root
(`~/.local/share/msks/<id>/`): inside a devenv shell, either move
that workspace's directory under the worktree's
`.devenv/state/msksc/data/` or unset `MSKSC_DATA_DIR` — the ssh
error names the path it looked in.

```bash
msks ls        # presets: https://127.0.0.1:8660, the worktree's
               # token, and .devenv/state/msksd/msks-ca.pem
               # (verified TLS)
```

Targeting a hand-run bare-host daemon instead (#146 — no managed
process; `msks-dev-ready` then `msksd` from a shell) takes the
explicit pair from ITS state, with its own CA:

```bash
export MSKSC_URL=https://127.0.0.1:8660
export MSKSC_TOKEN=$(cat .devenv/state/msksd/bootstrap-token)
export MSKSC_CAFILE=.devenv/state/msksd/msks-ca.pem
```

The daemon serves TLS with a self-signed certificate. Point
`MSKSC_CAFILE` at the daemon's CA (`msks-ca.pem` under its state
directory) and the client verifies the certificate chain. Without
`MSKSC_CAFILE` the client proceeds unverified and prints a warning to
stderr on every invocation — the same trust-on-first-use posture as
`msks console` (#21), fine for a lab network and worth closing before
anything real.

## Workspace identity: a name and an id (#246)

A workspace carries two identity fields. The **name** is the label
you choose at create — the positional argument of every command,
unique among the daemon's workspaces. The **id** is minted by the
daemon at create (10 hex digits — 5 random bytes, re-rolled until
it answers to nothing on this daemon), immutable, and never reused
while its workspace lives: every host-side surface — the artifact
directories under the state
dir, the catalog row, and the client-side caches — keys on the id.
Deleting a workspace and creating another under the same name
yields a different id with nothing of the first instance left to
collide: the second workspace's `msks ssh` works on the first try,
with a fresh host-key cache of its own.

Every command that takes a workspace accepts either reference —
`msks console ws` and `msks console 1a2b3c4d5e` reach the same
workspace. The name is the everyday reference; the id is the
reference no other workspace shares.

## `msks ls`

Prints one line per workspace the daemon knows, aligned in five
columns: name, id, status, image hash (first 12 hex chars), and
owning host. The columns are measured against the values present
(#271): a long name or image reference widens its column for every
row, so each row's columns start at the same offsets — the header
row included.

```text
$ msks ls
name          id          status   image         host
my-workspace  1a2b3c4d5e  running  9f2c41ab77de  hv-1
scratch       77eedd0199  created  -             hv-1
```

The status column speaks the daemon's lifecycle vocabulary —
`created` (row exists, never booted), `starting`, `running`,
`paused`, `stopped`, `unknown`, `absent`. A `-` in the name column
is a workspace without a label — created nameless through the API,
or before the id/name split — addressed by its id alone. A `-` in
the image column means the workspace boots explicit kernel/rootfs
paths instead of a catalog image.

`--json` replaces the table with one JSON document — the API's
workspace rows verbatim (id, name, kernel, initrd, rootfs, cmdline,
cpus, mem_mib, image_hash, host, root_mib, home_mib, status,
created_at):

```bash
msks ls --json | jq -r '.[] | select(.status == "running") | .name'
```

A daemon with zero workspaces prints nothing (an empty table) and an
empty JSON array under `--json`.

## `msks storage`

Reports the state-disk budget and its consumers (#184) — the budget
line, each workspace's cost beside its ceiling, and each catalog
image's cost:

```text
$ msks storage
state disk    used 23.4G of 40G    free 16.6G    pressure ok

workspace  root cost/ceiling  home cost/ceiling  cost
ws4        3.1G / 10G         812M / 2G          3.9G
scratch    61M / 10G          12M / 2G           73M

image      imported          cost
debian:13  2026-09-21 12:03  3G
debian:13  2026-08-02 05:11  3G
```

- **cost** is the disk blocks the artifact occupies on the state
  disk — an idle artifact costs its content, not its ceiling (the
  files are sparse), and an overlay's cost rides a little above
  what the guest's own `df` shows for its root (qcow2 bookkeeping).
- **imported** is when the image archive came in, local time to
  the minute (#186) — entries that share a reference (a rebuilt
  image imported again) read as distinct through it; the listing
  orders them oldest-first.
- **ceiling** is the size fixed at create; the guest sees it as its
  quota and its user can watch it fill with `df` inside the
  workspace.
- **pressure** names the state-disk condition: `warn` at or past
  `MSKSD_STORAGE_WARN_PCT` (default 90) percent used, `critical`
  below `MSKSD_STORAGE_FLOOR_MIB` (default 512) free. Below the
  floor, workspace creates, image imports, and home-volume
  imports answer a named `507` until space comes back (`msks
storage` names the consumers; `msks rm` and `msks image rm`
  remove them; the message's other escape is _lowering_ the floor —
  raising it refuses more, not fewer). Imports are sized when the
  size is knowable: the image archive is counted twice (retained
  copy plus unpacked cache), and a home upload with a
  `Content-Length` must fit above the floor.

`msks storage <workspace>` narrows the workspace table to one
workspace, by name or id.
`--json` prints the API's `GET /api/v1/storage` document verbatim
(state block, per-workspace rows, catalog rows). The events channel
carries a `storage.pressure` event whenever the pressure changes.

## `msks create`

POSTs the API's create body. The positional argument names the
workspace (#246) and follows the daemon's workspace charset —
lowercase letters, digits, and dashes, starting with a letter or
digit, up to 64 chars. The daemon mints the workspace's immutable
id; the name is the label the other commands address it by. The
create request's field is `name` (an `id` field is accepted as the
same thing — the pre-#246 spelling).

Flags map onto the create request's fields (the identity flags
below generate theirs):

| Flag            | API field               | Meaning                                                                      |
| --------------- | ----------------------- | ---------------------------------------------------------------------------- |
| `--image`       | `image`                 | Catalog ref: `name:version`, bare name, or hash                              |
| `--kernel`      | `kernel`                | Explicit kernel path (skips the catalog)                                     |
| `--initrd`      | `initrd`                | Explicit initrd path                                                         |
| `--rootfs`      | `rootfs`                | Explicit rootfs path (skips the catalog)                                     |
| `--cmdline`     | `cmdline`               | Explicit kernel cmdline                                                      |
| `--cpus`        | `cpus`                  | vcpus, 1–64 (daemon default: 2)                                              |
| `--mem-mib`     | `mem_mib`               | Guest memory MiB, 64–32768 (daemon default: 8192)                            |
| `--root-mib`    | `root_mib`              | Persistent root overlay size (daemon default)                                |
| `--home-mib`    | `home_mib`              | Persistent /home volume size (daemon default)                                |
| `--user-data`   | `user_data`             | First-boot provisioning payload file; `-` reads stdin (#41)                  |
| `--daemon-mint` | —                       | Hand the identity to the daemon instead of the client mint (#121); see below |
| `--pubkey`      | `ssh_pubkey` (verbatim) | Use a public key you already own as the identity (#132); `-` reads stdin     |
| `--key-type`    | —                       | The client mint's key type: `ed25519` (the default), `ecdsa`, or `rsa`       |
| `--user`        | `user`                  | The workspace's login user (#248); default: your username                    |

Only the flags you pass are sent — unset flags let the daemon apply
its own defaults. An `--image` reference resolves against the
daemon's image catalog (`docs/images.md`); explicit `--kernel` and
`--rootfs` bypass it. Size ranges are enforced server-side; a value
outside them comes back as a validation error (below).

`--user NAME` records the workspace's login user (#248): the
name rides the row, and the guest's first boot seeds the account
— `useradd -m`, a home on the persistent `/home` volume populated
from `/etc/skel`, the workspace identity in its `authorized_keys`,
and the same passwordless-sudo grant the image's own workspace
user carries. `msks ssh`, `msks rsync`, and `msks console` then use
the recorded name as their default login, so a workspace answers
the same identity for every operator that reaches it. Without
`--user` the create fills the invoking user's name — a bare create
lands your own account. The name must fit the login-name charset
(lowercase letters, digits, dashes, underscores; a lowercase
letter or underscore first; at most 32 characters); a username
that does not (a capitalized one) is refused with a line pointing
at `--user`. Naming `root` or the image's `msks` account keeps the
shipped account and seeds nothing new. A name that lands on a
system account the image already ships (Debian carries
charset-valid names like `sync` and `man`) seeds nothing either —
the first boot says so in its log, and the login stays with the
accounts the guest already serves; pick a name the image uses
for no one. Create-time and immutable,
like `user_data`: a workspace created before #248 keeps the
image's `msks` user as its login.

`--start` boots the workspace right after creating it:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
attach with: msks console my-workspace
```

The confirmation line prints both halves of the workspace's
identity — the label you chose and the id the daemon minted — as
soon as the create succeeds and before the boot is
attempted. A failed boot still leaves the workspace created — the
error message says so and names the recovery command:

```text
created my-workspace (id 9f2c41ab77)
msks: 503: vmm launch failed
msks: 9f2c41ab77 is created; boot it later with: msks start my-workspace
```

Creating without `--start` prints the same line and exits; boot it
whenever with `msks start` — or just `msks console` it: the console
command boots a not-running workspace on its own (below).

`--user-data` is the first-boot provisioning hook (#41): the file's
contents travel to the daemon and run once on the
workspace's first boot (see `docs/images.md` for the seed-disk
mechanism, the payload forms each image provisioner accepts, and
the create-time immutability). It composes with `--start`:

```bash
$ printf '#!/bin/sh\napt-get update\n' | msks create ws --user-data - --start
created ws (id 77eedd0199)
attach with: msks console ws
```

The client mint is the create default (#121): `msks create` mints
the workspace's ssh keypair on this client, sends the public half
only, and keeps the private half — the daemon never holds it (no
escrow). The private half is written mode 0600 under the client
data root — `~/.local/share/msks/<id>/identity`, honoring
`XDG_DATA_HOME` or `MSKSC_DATA_DIR` — after the create succeeds,
and `msks ssh` picks it up from there:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
client identity (mode 0600): /home/you/.local/share/msks/9f2c41ab77/identity
attach with: msks console my-workspace
```

Losing that file loses ssh to the workspace and the console with
it (a seeded guest challenges the console with the same key) —
unless the operator's ssh-agent holds that key, which the console
consults next; move it somewhere safe or keep backups. The file lives
under the data root, not the cache, so cache sweeps leave it alone.
A client-minted workspace answers `msks key` with its public half
only. The key type of a _minted_ key is the machine's choice
(`--key-type`, defaulting to `ed25519`, the same FIPS-approvable
default the daemon mints).

`--pubkey FILE` builds the workspace around a public key you
already own (#132): the file's one line travels to the daemon at
any well-formed key type, the private half stays wherever you keep
it, and nothing is written client-side. Log in with that key
directly — `ssh -i` through a forward, or the `Host msks-*` alias
with `IdentityFile` pointing at it; `msks ssh` on such a workspace
exits with a line saying exactly that. The three identity modes are
exclusive: `--pubkey` conflicts with `--daemon-mint`, and
`--key-type` pairs with the mint alone.

`--daemon-mint` hands the identity to the daemon instead
(#111): it mints the keypair at create and stores both halves with
its state — the private half is then fetchable with `msks key
--private`.

## `msks start`

Boots one created workspace (`POST /api/v1/workspaces/{id-or-name}/start`)
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

Powers one running workspace off (`POST /api/v1/workspaces/{id-or-name}/stop`)
and prints the result, mirroring `msks start`:

```bash
$ msks stop my-workspace
my-workspace stopped
```

## `msks resize`

Moves a **stopped** workspace's disk sizes and topology
(#184, #277) — the ceilings its guest sees as quotas, and the cpus
and memory it boots with — through `POST
/api/v1/workspaces/{id-or-name}/resize`:

```text
$ msks resize ws4 --home-mib 4096
resized ws4: root 10240 MiB, home 4096 MiB

$ msks resize ws4 --cpus 4 --mem-mib 4096
resized ws4: root 10240 MiB, home 4096 MiB, cpus 4, mem 4096 MiB
(the new topology applies on its next boot)
```

- `--home-mib` grows or shrinks the `/home` volume. The daemon
  quiets the filesystem, `resize2fs` moves it, and a shrink that
  would cut into used blocks answers a named `409` (free data in
  the workspace or shrink less).
- `--root-mib` grows the root overlay only; the next start's
  cloud-init fills the larger device for free (the command's output
  says so when the root moved — a home resize's bytes are already in
  place). The grow-only rule is measured against the overlay's actual
  virtual size, which sits above the row when create clamped it to
  the base image. Shrinking the root stays unsupported — `msks rm`
  and a fresh create, or a factory reset, reclaim a root instead.
- `--cpus` and `--mem-mib` set the vCPU count and guest memory. The
  daemon records them in the workspace row and boots it with the new
  topology at the next `msks start` — a running workspace is never
  reconfigured live. The bounds are create's (`--cpus` 1–64,
  `--mem-mib` 64–32768 MiB), and the command's output carries the
  boot note when they changed. The flags mix freely with the disk
  flags: one invocation can move the disks and the topology
  together.

The workspace must be in a free lifecycle state (`created`,
`stopped`, `absent`) — a running or paused workspace answers `409`.
The row follows immediately (`msks ls --json`, `msks storage` show
the new ceiling), and a completed resize is announced on the events
channel (`workspace.resized`). At least one flag is required.

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

Deletes workspaces (`DELETE /api/v1/workspaces/{id-or-name}`) — the
row, the VMM (stopped first, killed if wedged), and the persistent
artifacts: the root overlay and the `/home` volume. The data does
not come back; creating a workspace under the same name mints a
fresh id and starts from the image's pristine root, with nothing
of the deleted instance left to collide with. One workspace or
several:

```bash
$ msks rm my-workspace
my-workspace deleted
$ msks rm scratch-1 scratch-2
scratch-1 deleted
scratch-2 deleted
```

Workspaces are removed one at a time, in the order given; a failure
stops the run there with the API's one-line error, and the ones
already removed stay removed (each success printed its confirmation
line). There is no confirmation prompt — deleting is what `rm` means,
and a workspace is recoverable by recreating it. A workspace recorded
on another host answers 409 with the host mismatch named in the error,
like every lifecycle command.

## `msks image`

Manages the daemon's image catalog — the surface `docs/images.md`
documents over HTTP, as CLI subcommands. Every subcommand uses the
same client environment as the workspace commands.

### `msks image ls`

One line per registered image — reference, hash (first 12 hex
chars), the default designation, and the kernel facts — on the
measured grid every listing shares (#271):

```text
$ msks image ls
ref          hash          default  kernel
debian:13    9f2c41ab77de  default  6.12.107+deb13 (raw)
alpine:3.20  33aa9db1c4ef  -        6.12.7 (raw)
```

The image the daemon designates as default carries the `default`
flag; a bare `msks create` resolves to it. `--json` prints the
listing as the API returns it (`GET /api/v1/images`), stable for
scripting.

### `msks image import`

Registers an archive in the catalog (`POST /api/v1/images`). The
source is a **daemon-side** path — the daemon reads the file from
its own filesystem; the command does not upload anything — or an
`https://` URL the daemon downloads itself (#258):

```text
$ msks image import /srv/images/debian-13.tar
imported debian:13 (9f2c41ab77de)

$ msks image import https://images.example.com/debian-13.tar
imported debian:13 (9f2c41ab77de)
```

A URL source is fetched into the catalog's staging area under the
import ceiling and deadline (`MSKSD_IMAGE_IMPORT_MAX_MIB`,
`MSKSD_IMAGE_IMPORT_TIMEOUT_S`), verified against system TLS
roots, and imported from the downloaded copy — the recorded hash
always reflects the fetched bytes, so the same content imports
once regardless of source. The first image imported into an empty
catalog also becomes the daemon's default. An archive the daemon
cannot read, parse, or fetch answers 400 with the reason on one
line.

### `msks image check`

Boots an image locally and verifies the guest contract point by
point (#258) — the pre-import gate `docs/images.md` describes. The
command runs on the image author's host: it needs `/dev/kvm` and
nothing else from msks (no daemon, no state):

```text
$ msks image check workspace-mine-1.0.tar
PASS archive        imported mine:1.0 (1d6a5e782fc0), prelude-v1 handshake as 'root'
PASS boot           guest answered the console in 3.4s (kernel 6.12.107+deb13-amd64)
PASS console        prelude-v1 handshake as 'root'
PASS user-data      seed payload ran on first boot
PASS acpi-shutdown  clean shutdown within 120s
PASS root-rw        root is writable and the write survived a stop/start (overlay)
PASS home-label     /home mounted by label msks-home and its write survived a stop/start (volume)
```

A broken contract point fails its row and the exit code is 1,
naming the first failure; `--keep` preserves the throwaway state
dir (serial logs) for inspection. `--egress` adds the DHCP point —
the guest must take a global address over the daemon's own net
stack — and needs root plus an egress-capable default route
(`--uplink` names another interface).

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
seed     provisioner - (none declared)
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
(moves, boots, and deletes serialize per workspace; the waiter
answers a named 409 after `MSKSD_MOVE_WAIT_TIMEOUT_S` instead of
hanging). Export streams the
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
name (a foreign host). An import keeps the
workspace's existing volume until the upload completes and passes
the ext4 check; a cut-off upload changes nothing.

The typical pairings: backup (`export`, later `import` back into
the same workspace after a `rm` + `create`), migration (`export` on
one daemon, `create` + `import` on another), and seeding (build an
ext4 image with the data a fleet of workspaces starts from, import
it into each fresh one). Inside a workspace with egress, git and
rsync over the forward (`docs/networking.md`) carry day-to-day
code; the volume moves are for the whole `/home` at once.

## `msks secret`

Placeholder secrets (#198): mint a sentinel for one workspace, and
the daemon keeps the real secret in its store — the workspace never
holds it (the full story, including where the real secret lives per
provider, is [docs/secrets.md](secrets.md)):

```bash
# from a password manager, nothing touches disk
op read 'op://Vault/github/credential' \
  | msks secret mint myws --name github_api \
      --dest api.github.com --secret-file -

msks secret mint myws --name pypi --dest .pypi.org --dest pypi.org \
  --ttl 86400 --secret-file ./token   # suffix + exact, one day

msks secret ls                          # placeholders, never sentinels
msks secret renew myws --name pypi --ttl 86400
msks secret revoke myws --name github_api
msks secret check                       # the store answers writes
```

`--secret-file` takes a path or `-` for a pipe (the value is
whitespace-stripped at both ends); the secret is
never accepted as a command-line argument (argv lands in process
lists and shell history), and an empty file is refused before any
network roundtrip. `--dest` repeats and binds the swap: an exact
host (`api.github.com`) or a suffix that covers every host under a
domain (`.github.com`). The mint prints the sentinel exactly once —
every later view omits it, so a lost sentinel is re-minted, not
recalled. `revoke` takes effect on the next request; `renew`
extends a `--ttl` lifetime in place with the sentinel unchanged.

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

The session runs as the workspace's **login user** by default
(#248 — the name `msks create --user` recorded; the image's own
`msks` account for a workspace created before #248).
`--user root` is the recovery shell, and `--user` accepts any
name the workspace serves — the image's console users or its
recorded login user, which the first-boot seed provisions. A user
neither serves is refused by name before any shell starts, and
the session's terminal
geometry rides the same request (the guest pty matches the client's
size at attach):

```text
$ msks console my-workspace --user root
(workspace prompt, as root)
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

## `msks egress`

Egress consent (#69, #195): decide, watch, and inspect a workspace's
outbound-destination verdicts. A workspace in `interactive` mode
holds each new outbound connection's first packet until a decider
allows or denies it; `static` workspaces allow only their
create-time allowlist; `allow` workspaces (the create default)
record off-list destinations and pass them.

```text
msks egress tui ws-dev            # THE decider: a live TUI (see below)
msks egress rules ws-dev          # the mode, allowlist, and in-effect verdicts
msks egress requests ws-dev       # the consent rows (audit trail), newest first
msks egress requests ws-dev --decision pending
msks egress decide ws-dev <request-id> allow --duration 5m
msks egress decide ws-dev <request-id> deny --duration forever
msks egress revoke ws-dev <request-id>
msks egress watch ws-dev          # stream frames; registers this client as
                                  # a decider (holds wait only while one is
                                  # connected)
msks egress watch ws-dev --decide --duration forever
                                  # the same, prompting y/n per request
```

`decide` and `revoke` name the request by the id `watch` and
`requests` print (the full id, copy-pasteable); both act only on
the named workspace's requests. A portless destination (a
non-TCP/UDP flow) prompts as `host (all ports)` — an allow opens
every port on the host for the duration. Durations: `once` (this connection only — a
reconnect re-prompts), `5m`, `15m`, `tilrestart` (until the
workspace VM stops; the default), `forever` (the workspace's
lifetime — replayed at every boot). `revoke` undoes an in-effect
verdict immediately: the flow rules and the destination's live
connections drop, and new connections gate again.

### `msks egress tui` — the decider's screen

The TUI is the interface a human decides from: it registers this
client as the workspace's decider (holds wait only while one is
connected), shows every held request with its countdown, and sends
verdicts through the same endpoints the subcommands use. Keys:

- `a` / `d` — allow or deny the focused hold for the default
  duration (`tilrestart`); `A` / `D` open the duration picker
  (`once / 5m / 15m / tilrestart / forever`).
- `↑`/`↓` move the queue; `r` flips to the rules screen (the
  in-effect verdicts with countdowns, and `x` to revoke the focused
  rule — the row leaves on the daemon's refreshed frame, never
  optimistically); `r` or `Escape` returns.
- `q` quits. A dropped connection reconnects with backoff and
  re-registers (the snapshot re-lands); while disconnected the
  status line says so — the daemon fail-closes new connects, and
  in-flight holds run their timeout.

The protocol state (frame parsing, countdowns) is pure and
unit-tested; the `watch`/`decide`/`revoke` subcommands remain the
scripting surface (`watch` prints frames as lines).

The create-time posture (`msks create --egress-mode`, `--allow`)
is fixed with the workspace: `--egress-mode allow|static|interactive`,
and repeatable `--allow SPEC` entries — a bare host matches the
apex only, `.host` includes subdomains, `*.host` matches subdomains
only, `host:port` scopes a port, and `10.0.0.0/8[:port]` names an
address range. Name entries gate at the daemon's resolver (the one
the DHCP lease hands the guest); address entries accept in the
per-VM kernel chain. Switching mode means recreating the workspace.

## `msks key`

The workspace's minted ssh identity (#111): every workspace a
local-backend daemon creates carries
a keypair msksd created at create-time, whose public half the
guest's first boot planted into `authorized_keys` for root and the
workspace's login user. The fetch takes the same
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
the daemon's `MSKSD_SSH_KEY_TYPE` setting (Ed25519 by default).
Both halves persist across daemon restarts and workspace stop/start.

A client-minted workspace (#121, the `msks create` default) serves
its public half; its private half never reached the daemon, so
`--private` and `--out` exit with an error naming where that half
lives — the client data root of the client that created the
workspace (`~/.local/share/msks/<id>/identity`, or that root under
`MSKSC_DATA_DIR`).

## `msks llm-token`

The workspace's LLM proxy credential (#259): the bearer token the
workspace's own LLM clients present to the daemon's proxy on its
tap (`docs/llm.md`). The seed already planted it inside the
workspace — `/etc/msks/llm.token` and the `MSKSWS_*` exports — so
this fetch is for the operator's side (a tool configured outside
the workspace) and for rotation:

```bash
msks llm-token my-workspace             # the stored credential
msks llm-token my-workspace --remint    # a fresh one, replacing it
```

A remint does not re-run the seed — export the new token inside the
workspace by hand. A workspace that predates #259 answers with the
remint hint instead of a token.

## `msks ssh`

Stock ssh into a workspace over the forward, with the minted
identity staged in memory (#112) — one command, no key steps:

```bash
msks ssh my-workspace              # as the workspace's login user
msks ssh my-workspace -- -l root   # the recovery login
msks ssh my-workspace -- -A        # forward your agent ($SSH_AUTH_SOCK)
msks ssh my-workspace -- -L 8080:localhost:80
```

The command boots the workspace first when the daemon reports it as
not running (the same notices as `msks console`), fetches the
identity over the authenticated API, and runs `ssh` with the
forward websocket as its ProxyCommand (`msks forward <ws> 22`). A
session that booted its workspace waits out the guest's first boot
(#168): the daemon reports `running` while the guest's sshd is
already up but the identity seed has yet to land in
`authorized_keys`, so the command probes the login first (a
throwaway `true` as the remote command, retried for up to 30s with
a one-line notice between attempts) and opens the real session —
interactive or one-shot — once the guest accepts the workspace
key; a remote command runs exactly once either way. The probe
carries only the session's login user and agent-forwarding setting —
msks's own transport, its
quiet flag, and a `true` command — so a session's tunnels and
other ssh options cannot hold the wait open or alter it; the
session itself keeps every option. For
a daemon-minted workspace the private half arrives over that API;
for a client-minted one (#121, the create default) the API serves
the public half and the private half comes from the local data root
(`~/.local/share/msks/<id>/identity` under the default root,
`MSKSC_DATA_DIR` when it is set; written at create) — a
missing, stale, or corrupt file exits with one line naming the path
and the recovery. Either way the session writes no new copy of the
private half anywhere: a transient in-process ssh-agent holds it in
memory for the session, ssh names the identity by its public half
(`-i`, public material only) and signs through the agent socket — a
daemon-minted half arrives over the API and goes away with the
process, and a client-minted half is read from its one file and
left exactly there. Host keys land in a per-workspace
`known_hosts` under the msks cache root — `MSKSC_CACHE_DIR` when
it is set, else `XDG_CACHE_HOME` or `~/.cache/msks`, then
`<workspace-id>/known_hosts` (#246 — the cache keys on the
workspace's immutable id, so a workspace recreated under the same
name starts with a fresh cache and trusts its own first-boot keys
again) — under `accept-new`; they persist across stop/start on the
workspace's overlay, so the first-connection entry keeps matching.
(The alias block keeps its own known_hosts under
`~/.cache/msks/msks-<ws>/` — the two paths record the same host
key independently.)

Agent forwarding asked for on the command line (`-A`, or
`-o ForwardAgent=yes`) forwards **your** agent — the socket
`SSH_AUTH_SOCK` names (#174): `ssh-add -l` inside the workspace
lists the same keys as on the host, and `git push git@github.com`,
`ssh`, and friends sign with the host's credentials. The session
authenticates through a transient in-process agent (above), and
stock ssh would forward _that_ one when asked to forward at all —
so msks rewrites the request into `ForwardAgent=<your socket>`,
an explicit path (an OpenSSH client 8.2 or newer parses the
form; an older client exits with its own usage error — RHEL 8's
8.0, for one); the workspace identity stays an
authentication credential and reaches the guest as nothing else.
A request that already names a socket
(`-o ForwardAgent=/path/to/sock`) keeps its own. `-A` with no
live agent behind `SSH_AUTH_SOCK` exits with one line naming it.
Forwarding set by an ssh config file keeps the stock meaning
under this command — it forwards the session agent; the command
line is where your agent is named.

Everything after the workspace id (the `--` is optional — any
argument ssh would take works verbatim) is passed to ssh. A
passthrough that starts with a plain word is a remote command
(`msks ssh my-workspace -- uname -a`); when it starts with an
option, ssh's own separator carries a command after the options
(`msks ssh my-workspace -- -A -- uname -a`). `-A` forwards your
agent, the same forwarding the paragraphs above describe —
before #174 it forwarded the session agent, the minted workspace
identity, which served nested logins to the same workspace (ssh
from inside to `localhost`). That use now names the identity
explicitly instead: `msks key my-workspace --private` writes the
half, or your own agent (forwarded) holds it once you `ssh-add`
the written key.
The exit code is ssh's own (255 for ssh failures), not the msks
command set. The session agent lives exactly as long as the
`msks ssh` process — `ControlPersist`/mux sessions that outlive it
belong to the alias path, whose identity is a key file. Explicit
passthrough options override the injected defaults (your own
`UserKnownHostsFile`, a different `ProxyCommand`) exactly as with
stock ssh: ssh takes the first value a repeated option receives,
and the passthrough comes first. The login user is the
workspace's recorded login user (#248 — the create-time `--user`,
defaulting to the creating operator's name; the image's `msks`
account for a workspace created before #248); ssh arguments that
name a user
(`-l root`, `-o User=root`) override it. The host argument ssh
sees is the workspace id itself — the transport is the proxy, so
the name never resolves. The ProxyCommand runs this very
client (the absolute interpreter and module form — `msks` need not
be on the ssh child's PATH), and it inherits your environment:
`MSKSC_URL`, `MSKSC_TOKEN`, and
`MSKSC_CAFILE` must be set where ssh runs (see the
networking chapter's alias workflow for the `Host msks-*`
configuration that hides all of this).

## `msks rsync`

Stock rsync against a workspace over the forward, with the minted
identity staged in memory (#190) — one command does the whole
setup itself: the workspace boots when needed, the identity
stages in memory, and the copy opens its own forward:

```bash
msks rsync my-workspace -- -av ./site/ root@:/root/site/    # push
msks rsync my-workspace -- -av root@:/root/out.tar ./out.tar  # pull
msks rsync my-workspace -- -aP ./src/ :src/                   # as msks
```

Everything after the workspace id (the `--` is optional) is given
to rsync; msks parses no rsync flags. The one shaping pass is
the empty host: a path whose host is empty (`:/root/site/`, or
`root@:/root/site/`) targets the workspace this command names,
its host filled in as the copy runs. The direction — push or
pull — comes entirely from the rsync arguments, and the login
user comes from the paths the same way: `root@:/root/site/`
logs in as root, a bare `:src/` as the workspace's login user
(whose home is the persistent `/home` volume — the shape for
copies under your own account). A path that names a host keeps
it, and the transport is the proxy either way, so the name never
resolves. `::module` paths are rsync's daemon protocol against
port 873 — the workspace image runs no rsync daemon (sshd stays
its one inbound service), so that form is left as typed and
fails as rsync's own error.

A word that starts with a colon reaches this fill even when it
is the split-apart value of an rsync option (`--filter :rules`),
because msks does not parse rsync's flags to tell the two apart:
that spelling uses the attached form (`--filter=:rules`), which
msks never touches.

The command boots the workspace first when the daemon reports it
as not running (the same notices as `msks console`), fetches the
identity over the authenticated API, and runs the host `rsync`
with the same transport `msks ssh` uses: the forward websocket as
the ssh ProxyCommand (each rsync connection opens its own), the
per-workspace `known_hosts` under `accept-new`, and the identity
staged in a transient in-process ssh-agent — the private half
exists only in memory, rsync's ssh children name it by its public
half and sign through the agent socket, and the command writes no
key file. Daemon-minted (#111) and client-minted (#121, the
create default) identities both work; a `--pubkey` workspace
(#132) exits with the line naming where the private half lives.
A session that booted its workspace waits out the guest's first
boot exactly as `msks ssh` does (#168): a probe login retries
behind the identity seed (up to 30s, one line between attempts),
and the copy runs once the guest accepts the workspace key.

The login user is the workspace's recorded login user (#248),
stated as a generated per-session ssh config — so rsync's own
`user@` path spelling overrides it (`root@:/root/site/` logs in
as root). An explicit `-e` in the passthrough replaces msks's
remote shell entirely (rsync takes the last `-e`, and the
empty-host fill keeps pointing at the workspace id your own
transport must then reach), the same override shape ssh
passthrough options have. The host prerequisites are `ssh` and
`rsync`; the exit code is rsync's own.

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
