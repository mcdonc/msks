# The CLI client

Along with invoking the TUI, the `msks` command is a client for the daemon's
`/api/v1` REST surface.

Here's an overview of the common command set:

```bash
msks                         # the workspace tree TUI
msks ls                      # what exists, and what state is it in
msks create ws                # make a workspace
msks clone ws fork            # copy a workspace into a new one
msks console ws               # boot it if needed, then work inside it
msks forward ws 22            # bridge a guest TCP port to stdio
msks key ws                   # fetch the workspace's ssh identity
msks ssh ws                   # log in over the forward (boots it if needed)
msks rsync ws -- -av ./src/ root@:/src/  # copy files over the forward
msks home export ws           # download its /home volume (backup, seed)
msks start ws                 # boot it without attaching
msks stop ws                  # power it off
msks rm ws                    # delete it (and its data)
```

The client and the daemon ship in the same Python distribution, but they might
be run from different installation directories, so daemon and client versions
may not be in sync, which may cause problems. To cope with this, every command
makes one best-effort `/health` roundtrip by the client to the daemon to do a
version compatibility check. If the daemon is a different version than the
client, the invocation prints one stderr line giving both and a remedy. This is
most usually encountered on MacOS, because the daemon runs in a Lima VM
separate from the client. If you see this on MacOS, rerun
`msks-setup-macos-host` on the Mac to update the daemon in the Lima VM.

## Machine-readable output

Every command that prints a report or a confirmation takes `--json`.
The flag switches the command's stdout to one JSON document and its
failures to one JSON error object on stderr. The default output stays
human-readable and keeps its shape.

The mutating commands emit the row they affected: `create` and `clone`
emit the new row, `start` and `stop` emit the settled row (the verbs
block until the transition completes), `resize`, `mount`, and `umount`
emit the refreshed row. `msks rm` prints one compact JSON line per
removed workspace, in order, so a partial run's survivors stay on the
record. `msks secret mint` emits the new row including the sentinel:
the sentinel prints once, in whichever mode you asked for, and is
never available again.

A failure with `--json` prints one object on stderr and nothing on
stdout:

```json
{ "error": { "status": 404, "detail": "no such workspace" } }
```

`status` is the daemon's HTTP status for an API refusal, or `null` for
a failure that never reached one (an unreachable daemon, a timeout, a
locally refused argument). The object is one line; a harness reads it
whole.

Every command exits with a code from this table:

| Code | Meaning                                                                  |
| ---- | ------------------------------------------------------------------------ |
| 0    | Success                                                                  |
| 1    | A failure the table names no code for (network, timeout, other refusals) |
| 2    | Bad invocation (the usage error)                                         |
| 3    | Not found (the daemon answered 404)                                      |
| 4    | Name conflict (a create or clone with a name another row holds)          |
| 5    | Wrong state (the workspace's state or placement refuses the action)      |
| 130  | Interrupted (Ctrl-C)                                                     |

`msksd doctor --json` prints its check rows as one JSON document (one
object per check: `name`, `ok`, `warning`, `message`, `hint`) and keeps
its exit convention: 0 when the report passes, 1 when a check fails.

## Client environment

The client reads environment variables prefixed `MSKSC_` (client) to stay apart
from the daemon's `MSKSD_*` (server) namespace. Each of the environment
variables has a config-file equivalent.

| Variable                   | Config file key      | Meaning                                                                                                                                                                                                                                                                            | Default                                                   |
| -------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `MSKSC_URL`                | `url`                | The remote daemon's base URL, the TCP + TLS + token shape; the client dials TCP only when a URL gives one                                                                                                                                                                          | unset (the local socket answers, else the client refuses) |
| `MSKSC_SOCKET`             | `socket`             | The local listener's socket path: peer-credential authentication, with no token and no TLS                                                                                                                                                                                         | unset (the default socket is looked for)                  |
| `MSKSC_TOKEN`              | `token_file`         | A daemon bearer token (see tokens below); a socket connection needs none                                                                                                                                                                                                           | — (required for a URL)                                    |
| `MSKSC_CAFILE`             | `cafile`             | A PEM file to verify the daemon's TLS certificate; a socket connection verifies nothing (the kernel vouches for both ends)                                                                                                                                                         | unverified with warning                                   |
| `MSKSC_EXPECTED_IMAGE`     | `expected_image`     | An image reference the operator sets; `msks ls` compares it with the image the daemon reports in `/health` and reports drift                                                                                                                                                       | unset (no check)                                          |
| `MSKSC_CACHE_DIR`          | `cache_dir`          | The directory the per-workspace host-key caches live under; the per-workspace directories are created below it                                                                                                                                                                     | `~/.cache/msks`                                           |
| `MSKSC_IDENTITY_FILE`      | `identity_file`      | Your own private key file, the ssh identity `msks create` plants into every workspace (msks generates none; [the identity section](first-run.md#the-key-files) owns the whole story); a leading `~` expands                                                                        | unset (`pubkey_file` answers, else `msks create` refuses) |
| `MSKSC_PUBKEY_FILE`        | `pubkey_file`        | Your key's public half, what a bare `msks create` plants instead of reading `identity_file`. The form for a key you keep encrypted (your agent holds it; msks reads no private half); same naming rule, global-only                                                                | unset (`identity_file` answers)                           |
| `MSKSC_TERMINAL_TITLE`     | `terminal_title`     | The title template for the workspace-shell windows the workspace page opens (the file key's section lives in [The TUI](tui.md)); `{workspace}` resolves to the workspace's id                                                                                                      | unset (the terminal's own title)                          |
| `MSKSC_IMAGE_MANIFEST_URL` | `image_manifest_url` | Where the TUI's Import image action (`i` on the workspace list) reads the published channel's manifest (a fork's or self-hoster's channel gives its own); the picker shows each row's ref, description, and size, and the pick POSTs the row's URL to the daemon's import endpoint | this repository's latest release's `images.json`          |

`MSKSC_CACHE_DIR` exists because the host-key cache is disposable: a swept
cache costs one trust-on-first-use re-pin. Pointing it at a per-project,
disposable location is the intended use.

Each directory-valued setting takes an absolute path (a relative value is
refused with a line giving the fix; a leading `~` expands; an empty value
counts as unset). The per-workspace directories are created below it.

`MSKSC_IDENTITY_FILE` points at a file, not a directory: your own private
key, read in place. msks derives the public half from it at create. `msks
ssh` and `msks rsync` sign through your own ssh-agent when it holds that key
(`ssh-add` it). The match is on the key material, so the file it came from
and its name do not matter. Otherwise they stage the private half in memory.
The key file itself is never copied into msks's state.

A key msks stages itself is an unencrypted OpenSSH-format key (`ed25519`,
`ecdsa` P-256/P-384/P-521, or `rsa`). An encrypted key no agent holds is
refused with a line giving the fixes in order: `ssh-add` the key, decrypt a
copy, or pass `--pubkey` at create.

`MSKSC_PUBKEY_FILE` points at your key's public half, a `.pub`
file ([the identity section](first-run.md#the-key-files)
owns the agent requirement this form carries). A create with it
set plants that one line as the identity and reads no private
material at all. It is the form for a key you keep encrypted:
the public half is in a `.pub` file while the private half
answers from your agent.

A missing `MSKSC_TOKEN` is an error before any network activity (the client
gives the variable and exits), unless the daemon is reached through the
local socket, where the kernel vouches for the client and no token exists to
read.

An unconfigured client (no `MSKSC_URL`, no `MSKSC_SOCKET`, no
config-file daemon) looks for the default socket a same-host
daemon binds (`$XDG_RUNTIME_DIR/msks/msksd.sock`) and connects
through it with peer credentials alone. Finding none, it exits
with the paths it looked for and the two settings that pick a
daemon outright. The client never dials a TCP address
implicitly: a well-known port could belong to any daemon.

The daemon serves TLS with a self-signed certificate. Point
`MSKSC_CAFILE` at the daemon's CA (`msks-ca.pem` under its state
directory) and the client verifies the certificate chain. A
`cafile` that is missing or holds no parsable certificate is a
one-line refusal giving the daemon's CA file.

## The client config file

The environment variables above have a durable home: a YAML file at
`~/.config/msks/msks.yaml` (relocated by `MSKSC_CONFIG_DIR`, or
`XDG_CONFIG_HOME`). The variables keep working beside it, and each one
overrides the same key in the file. A fresh devenv shell with its presets
exported behaves exactly as it does today. Precedence, highest first:

```text
--daemon flag > MSKSC_* environment > the file's daemon aliases >
    the file's global keys > built-in defaults
```

A bare `msks` reads the default path and, on the first run,
generates a commented near-empty template there (the directory
0700, the file 0600). `--config <path>` reads exactly that file:
a missing one is an error, and an explicit path is never
generated. `--config=none` reads the environment and the
built-in defaults only.

Every scalar key is its `MSKSC_*` variable with the prefix stripped and
lowercased (`MSKSC_URL` → `url`, `MSKSC_EXPECTED_IMAGE` → `expected_image`),
with the same semantics as the variable. A leading `~` expands, and a key
set to nothing (`key:` with no value, or `""`) is the unset form. Hyphens
and underscores spell the same key: `token-file` and `token_file` are one
key (klangk's file spelled kebab; msks's variables spell snake). A file that
carries both spellings of one key is refused, the second treated as a
duplicate.

`token_file` is the one key whose variable carries a different
shape: the file points at a file holding one daemon token. The
config tree therefore stays free of inline credentials, and the
token keeps the permissions of the file that holds it.
`MSKSC_TOKEN` still carries an inline token when the environment
is the more convenient place for one. For the invocation, the
token is read out of its file into the process environment, the
substrate every reader already speaks, where child processes
(console shells, the terminal launcher) inherit it. The file
itself stays the durable, permissioned home. An unreadable or
empty token file is an error before any network activity, giving
both places a token can come from.

`identity_file` follows the `token_file` pattern for a different
secret: it gives your private key file by path, read in place
and never copied (see the create default below). It is
global-only, and a `daemons:` entry that carries it is refused,
because an ssh identity belongs to you, not to one daemon
connection.

Unknown keys and duplicate keys are refused at load, each error
giving the key and the valid ones, the same fail-fast rules
msksd's config file carries.

### Daemon aliases

`daemons:` specifies one entry per daemon you talk to. An entry picks its
daemon one way: `url` for a remote daemon, or `socket` for a local listener
(peer-credential authentication; the entry carries no `token_file` and no
`cafile`). The keys `token_file`, `cafile`, and `expected_image` override their
globals for that alias:

```yaml
url: https://127.0.0.1:8660
token_file: ~/.config/msks/dev.token
daemons:
  lab:
    url: https://hv-1.lab.example.com:8660
    token_file: ~/.config/msks/lab.token
    cafile: ~/.config/msks/lab-ca.pem
    expected_image: msks/debian13:1.1
  local:
    socket: /run/user/1000/msks/msksd.sock

active_daemon: lab
```

`active_daemon` gives the alias a bare `msks` addresses. The `--daemon
<alias-or-url-or-path>` flag picks one for a single invocation and also
takes a raw URL or an absolute socket path. The two selections sit at
different layers of the precedence rule. A `--daemon` alias outranks the
environment for that invocation, because you picked the daemon and its
entry's keys win over the ambient exports. An `active_daemon` alias sits
below the environment: the devenv presets keep winning. A worktree shell
still talks to its own dev daemon beside a user config file that picks
another.

### `ssh_options`

`ssh_options` remembers ssh options for every ssh session msks
runs: `msks ssh`, the workspace page's shell action (it runs
`msks ssh`; [The TUI](tui.md) documents the action), and `msks
rsync`'s ssh transport. The value is the ssh options you would
type after `--` on the `msks ssh` command line, the options
themselves. A `--` token in the value is refused: msks adds the
options to its own, and a remembered separator would turn
msks's transport into the remote command:

```yaml
ssh_options: -A -o ServerAliveInterval=30   # string form (shell-split)
ssh_options:                                # list form (tokens stay whole)
  - -A
  - -o
  - ServerAliveInterval=30
```

`ssh_options`'s environment form (`MSKSC_SSH_OPTIONS`) overrides
the file value with the string form.

The remembered options sit behind the command line's own, so an
option you pass for one invocation wins over the remembered one.
msks's own transport settings (the forward as ProxyCommand, the
per-workspace known_hosts, the identity) stay defaults beneath
both, the same first-obtained precedence stock ssh applies.

Agent forwarding gets the one extra rule the ordering alone
cannot give. When the command line carries any forwarding
setting (`-A`, `-a`, or a `ForwardAgent` value), the remembered
forwarding options are dropped for that invocation and the
command line decides alone. Otherwise a remembered `-A` would
reassign a typed `-a`, because ssh's flags assign in order. A
remembered `-A` forwards your agent, the one `SSH_AUTH_SOCK`
gives, exactly as a typed one does: the guest receives your
keys, not the workspace identity.

The remembered options carry the command-line passthrough's
full power, including the ability to override msks's transport
for every session (a remembered `ProxyCommand=`,
`UserKnownHostsFile=`, or `IdentityAgent=` re-routes or re-keys
every ssh msks runs). The command-line passthrough has the same
power per invocation; the remembered form persists it, so the
key is the one to check first when a remembered option breaks
the seam.

`msks console` is a websocket session, not ssh, so `ssh_options`
applies to the ssh surfaces above and leaves it untouched.

## Workspace identity

A workspace carries two identity fields. The **name** is the
label you choose at create, the positional argument of every
command, unique among the daemon's workspaces. The **id** is
minted by the daemon at create: 10 hex digits (5 random bytes,
re-rolled until it collides with nothing on this daemon),
immutable, and never reused while its workspace lives.

Every host-side surface keys on the id: the artifact directories under the
state dir, the catalog row, and the client-side caches. Deleting a workspace
and creating another under the same name yields a different id, with nothing of
the first instance left to collide. The second workspace's `msks ssh` works on
the first try, with a fresh host-key cache of its own.

Every command that takes a workspace accepts either reference: `msks console
ws` and `msks console 1a2b3c4d5e` reach the same workspace. The name is the
everyday reference. The id is the reference no other workspace shares.

## `msks ls`

Prints one line per workspace the daemon knows, aligned in seven columns:
name, id, status, egress mode, created date, image (catalog reference, or
the digest when the catalog cannot resolve one),

```text
$ msks ls
name          id          status                    egress       created     image             host
my-workspace  1a2b3c4d5e  running                   interactive  2026-05-14  debian:13.6       hv-1
scratch       77eedd0199  created                   allow        2026-06-01  -                 hv-1
refs-reader   9c1d2e3f4a  running, 1 mount skipped  allow        2026-06-18  9f2c41a…de1234ab  hv-1
```

The status column speaks the daemon's lifecycle vocabulary:
`created` (row exists, never booted), `starting`, `running`,
`paused`, `stopping`, `stopped`, `unknown`, `absent`. A workspace
reads `stopping` while a stop request's graceful shutdown runs.
The window lasts up to the shutdown timeout. A shutdown that
times out returns the row to the status it carried before the
stop. A workspace whose mounts
skip carries the count beside its status (`running, 1 mount
skipped` above), a standing marker that lasts until the config
and the row agree again; the [mounts
chapter](mounts.md#the-skip-behavior) lists every kind of skip and
its reason.

A digest the catalog cannot resolve to a reference reads as its middle-clipped
form (the `image` column's own budget). A `-` in the name column is a workspace
without a label (created nameless through the API, or before the id/name
split), addressed by its id alone. A `-` in the image column means the
workspace boots explicit kernel/rootfs paths instead of a catalog image.

`--json` replaces the table with one JSON document, the API's
workspace rows verbatim: id, name, kernel, initrd, rootfs,
cmdline, cpus, mem_mib, image_hash, image_ref, host, root_mib,
home_mib, egress, egress_slice, egress_mode, secret_coverage,
egress_allowlist, mounts, user_data, ssh_pubkey, login_user,
status, created_at, and mount_issues (the derived per-mount state
behind the status marker):

```bash
msks ls --json | jq -r '.[] | select(.status == "running") | .name'
```

A daemon with zero workspaces prints nothing (an empty table) and
an empty JSON array under `--json`.

## `msks storage`

Reports the state-disk budget and its consumers: the budget line,
each workspace's cost beside its ceiling, and each catalog image's
cost:

```text
$ msks storage
state disk    used 23.4G of 40G    free 16.6G    pressure ok

┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ workspace ┃ root cost/ceiling ┃ home cost/ceiling ┃ cost ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ ws4       │ 3.1G / 10G        │ 812M / 2G         │ 3.9G │
│ scratch   │ 61M / 10G         │ 12M / 2G          │ 73M  │
└───────────┴───────────────────┴───────────────────┴──────┘

┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ image     ┃ imported         ┃ cost ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ debian:13 │ 2026-09-21 12:03 │ 3G   │
│ debian:13 │ 2026-08-02 05:11 │ 3G   │
└───────────┴───────────────────┴──────┘
```

- **cost** is the disk blocks the artifact occupies on the state
  disk. An idle artifact costs its content, not its ceiling (the
  files are sparse), and an overlay's cost runs a little above
  what the guest's own `df` shows for its root (qcow2
  bookkeeping).
- **imported** is when the image archive came in, local time to
  the minute; entries that share a reference (a rebuilt image
  imported again) read as distinct through it, and the listing
  orders them oldest-first.
- **ceiling** is the size fixed at create; the guest sees it as
  its quota and its user can watch it fill with `df` inside the
  workspace.
- **pressure** gives the state-disk condition: `warn` at or past
  `MSKSD_STORAGE_WARN_PCT` (default 90) percent used, `critical`
  below `MSKSD_STORAGE_FLOOR_MIB` (default 512) free. Below the
  floor, workspace creates, image imports, and home-volume
  imports answer a named `507` until space comes back. `msks
storage` gives the consumers; `msks rm` and `msks image rm`
  remove them. The message's other escape is _lowering_ the
  floor; raising it refuses more, not fewer.

`msks storage <workspace>` narrows the workspace table to one
workspace, by name or id. `--json` prints the API's `GET
/api/v1/storage` document verbatim.

## `msks create`

POSTs the API's create body. The positional argument gives the
workspace's name and follows the daemon's workspace charset:
lowercase letters, digits, and dashes, starting with a letter or
digit, up to 64 chars. The daemon mints the workspace's immutable
id; the name is the label the other commands address it by. The
create request's field is `name` (an `id` field is accepted as
the same thing, the earlier spelling).

A create with a name another row already holds is refused: the
daemon answers 409 `workspace exists` and the existing row keeps
its id. Retry with a new name; the conflict is exit code 4 under
`--json`.

Flags map onto the create request's fields (the identity flags
below generate theirs):

| Flag          | API field               | Meaning                                                                            |
| ------------- | ----------------------- | ---------------------------------------------------------------------------------- |
| `--image`     | `image`                 | Catalog ref: `name:version`, bare name, or hash                                    |
| `--kernel`    | `kernel`                | Explicit kernel path (skips the catalog)                                           |
| `--initrd`    | `initrd`                | Explicit initrd path                                                               |
| `--rootfs`    | `rootfs`                | Explicit rootfs path (skips the catalog)                                           |
| `--cmdline`   | `cmdline`               | Explicit kernel cmdline                                                            |
| `--cpus`      | `cpus`                  | vcpus, 1–64 (daemon default: 4)                                                    |
| `--mem-mib`   | `mem_mib`               | Guest memory MiB, 64–32768 (daemon default: 8192)                                  |
| `--root-mib`  | `root_mib`              | Persistent root overlay size (daemon default)                                      |
| `--home-mib`  | `home_mib`              | Persistent /home volume size (daemon default)                                      |
| `--user-data` | `user_data`             | First-boot provisioning payload file; `-` reads stdin                              |
| `--pubkey`    | `ssh_pubkey` (verbatim) | Use a public key you already own as this one workspace's identity; `-` reads stdin |
| `--user`      | `user`                  | The workspace's login user; default: your username                                 |
| `--share`     | `mounts`                | A host directory mounted into the guest: `HOST_PATH:GUEST_PATH:MODE`, repeatable   |

Only the flags you pass are sent. Unset flags let the daemon apply its own
defaults. An `--image` reference resolves against the daemon's image catalog
(`docs/images.md`); explicit `--kernel` and `--rootfs` bypass it. Size
ranges are enforced server-side; a value outside them comes back as a
validation error (below).

`--share` gives a host directory, the absolute guest path it
lands at, and a mode (`ro` or `rw`, defaulting to `rw` when the
mode is omitted):

```bash
msks create ws1 --share /srv/projects:/mnt/projects --share /srv/refs:/srv/refs:ro
```

The host path must sit under one of the daemon's allowed roots
(`mount_host_roots`, [configuration](config.md#host-share-mounts)), resolved
against symlink escapes. The mounts apply at the workspace's next start. A
`--start` create attaches them on its first boot. The [mounts
chapter](mounts.md) holds the behavior: the two gates, the mode bound, and
the coherence between guests sharing one tree. `msks mount` and `msks
umount` change the set later.

`--user NAME` records the workspace's login user, and the name is carried on
the row. The guest's first boot seeds the account: `useradd -m`; a home on
the persistent `/home` volume, populated from `/etc/skel`; the workspace
identity in its `authorized_keys`; and the same passwordless-sudo grant the
image's own workspace user carries. `msks ssh`, `msks rsync`, and `msks
console` then use the recorded name as their default login. A workspace
therefore answers the same identity for every operator that reaches it.

Without `--user` the create fills the invoking user's name: a
bare create lands your own account. The name must fit the
login-name charset (lowercase letters, digits, dashes,
underscores; a lowercase letter or underscore first; at most 32
characters). A username that does not (a capitalized one) is
refused with a line pointing at `--user`.

Naming `root` or the image's `msks` account keeps the shipped
account and seeds nothing new. A name that lands on a system
account the image already ships (Debian carries charset-valid
names like `sync` and `man`) seeds nothing either. The first
boot says so in its log, and the login stays with the accounts
the guest already serves. Pick a name the image uses for no one.

The name is create-time and immutable, like `user_data`: a
workspace created before records no user of its own. `msks ssh`
and `msks rsync` dial it as `root`, and the image's `msks`
account stays reachable by giving it directly.

`--start` boots the workspace right after creating it:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
attach with: msks console my-workspace
```

The confirmation line prints both halves of the workspace's identity, the
label you chose and the id the daemon minted, as soon as the create succeeds
and before the boot is attempted. A failed boot still leaves the workspace
created; the error message says so. The recovery command:

```text
created my-workspace (id 9f2c41ab77)
msks: 503: vmm launch failed
msks: 9f2c41ab77 is created; boot it later with: msks start my-workspace
```

Creating without `--start` prints the same line and exits; boot
it whenever with `msks start`, or just `msks console` it: the
console command boots a not-running workspace on its own
(below).

`--user-data` is the first-boot provisioning hook: the file's
contents travel to the daemon and run once on the workspace's
first boot (see `docs/images.md` for the seed-disk mechanism, the
payload forms each image provisioner accepts, and the create-time
immutability). It composes with `--start`:

```bash
$ printf '#!/bin/sh\napt-get update\n' | msks create ws --user-data - --start
created ws (id 77eedd0199)
attach with: msks console ws
```

One operator key is the identity: `msks create` plants it as
every workspace's identity, one key across workspaces, the
daemon holding the public half only. The key is yours and msks
generates nothing: `identity_file` (or `MSKSC_IDENTITY_FILE`)
gives your private key file (the config file or the environment),
and a create with neither that nor `--pubkey` refuses with a
one-line error giving both.

msks does not scan `~/.ssh` and never guesses which of your keys to take.
The create sends the key's derived public half only and writes nothing
per-workspace. Your private key file stays where it lives, read in place and
copied nowhere. The confirmation gives the identity it used:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
identity: /home/you/.ssh/id_ed25519
attach with: msks console my-workspace
```

`msks ssh` and `msks rsync` resolve the same key at session time. A
workspace re-created under the same name therefore keeps your access: a new
id, the same key. Session resolution consults your own ssh-agent first: when
`SSH_AUTH_SOCK` gives a live agent holding that key, the session signs
through the agent and the key file's encryption stops mattering.

`--pubkey FILE` builds the workspace around a public key you
already own: the file's one line travels to the daemon at any
well-formed key type, the private half stays wherever you keep
it, and nothing is written client-side. `msks ssh` and `msks
rsync` work on such a workspace when the operator identity
resolves to that key (`identity_file` pointing at it). A key that
resolves nowhere keeps the direct route: `ssh -i` through a
forward, or the `Host msks-*` alias with `IdentityFile` pointing
at it. `msks ssh` exits with a line saying exactly that.

## `msks clone`

Copies one stopped workspace into a new one (`POST
/api/v1/workspaces/{id-or-name}/clone`) and prints the new
workspace's label and id:

```bash
$ msks clone seed task-42
cloned seed into task-42 (id 9a8b7c6d5e)
```

A clone is a full fork. The daemon copies both disks — the root
overlay and the `/home` volume. Where the state filesystem can
share blocks (a reflink), the copy costs its metadata; where it
cannot, the copy writes the source's allocated bytes. The new
workspace's row carries the source's create-time config:
the image and its pin, the sizes and topology, the egress mode,
the mounts, the login user, and the `user_data` payload. The clone's own factory reset re-provisions from that
payload. The [cloning chapter](cloning.md) holds the whole
story: what copies, what stays behind, and the in-guest secrets
property.

The source must be stopped. A clone of a running workspace is
refused with the reason, and the new name follows the create
grammar (lowercase letters, digits, and dashes).

The clone's egress allowlist starts empty under the copied
mode: a `static` clone answers every name NXDOMAIN until its
list is set, an `interactive` one earns each allow. The
`--copy-allowlist` flag opts the clone into the source's list
verbatim. The [cloning chapter](cloning.md) holds the whole
story.

## `msks start`

Boots one created workspace (`POST /api/v1/workspaces/{id-or-name}/start`)
and prints the result:

```bash
$ msks start my-workspace
my-workspace running
```

The start request answers after the VMM finishes booting: a few
seconds on an idle host, longer under load. The client waits up
to two minutes before reporting a timeout, and a timeout message
notes that the daemon may still finish the boot.

## `msks stop`

Powers one running workspace off (`POST /api/v1/workspaces/{id-or-name}/stop`)
and prints the result, mirroring `msks start`:

```bash
$ msks stop my-workspace
my-workspace stopped
```

The stop asks the guest for a graceful, deadline-bounded power-off: the
daemon presses the guest's power button and the guest's systemd runs a full
shutdown. The button is the ACPI event on an x86_64 host and the
device-tree gpio button on an arm64 host. The write-out can take a moment.
The deadline is the daemon's (`MSKSD_SHUTDOWN_TIMEOUT_S`); there is no
client-side timeout flag, and like `msks start` the client waits at most
two minutes on the answer. A stop that misses the daemon's deadline
answers 503 with the endpoint's detail on one line, exit non-zero.

While the guest keeps running, the daemon presses the button again every
five seconds. A press during early boot lands before logind listens.
The guest drops it, and the next press recovers it. On an arm64 host the
re-press never delivers (#826): cloud-hypervisor's power-button pulse
drives the line one way only. The first press is the only one that can
ever deliver. A first press lost there leaves the deadline and its
fallback kill as the whole stop. Size `MSKSD_SHUTDOWN_TIMEOUT_S` to
cover the guest's slowest clean shutdown. A value above 120s outlives
the client's wait for the stop answer: the daemon still finishes the
stop, and the client reports a timeout.

Stopping a workspace that is already stopped is a no-op success: the daemon
reports it stopped either way, and the client asks without pre-filtering on
local state. The same holds for a workspace that was never booted: the row
records `stopped` without a launch ever happening. The data survives the
stop: the root overlay and the `/home` volume come back on the next `msks
start`.

## `msks resize`

Moves a **stopped** workspace's disk sizes and topology (the
ceilings its guest sees as quotas, and the cpus and memory it
boots with) through `POST
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
  cloud-init fills the larger device for free (the command's
  output says so when the root moved; a home resize's bytes are
  already in place). The grow-only rule is measured against the
  overlay's actual virtual size, which sits above the row when
  create clamped it to the base image. Shrinking the root stays
  unsupported: `msks rm` and a fresh create, or a factory reset,
  reclaim a root instead.
- `--cpus` and `--mem-mib` set the vCPU count and guest memory. The daemon
  records them in the workspace row and boots it with the new topology at
  the next `msks start`. A running workspace is never reconfigured live. The
  bounds are create's (`--cpus` 1–64, `--mem-mib` 64–32768 MiB), and the
  command's output carries the boot note when they changed. The flags mix
  freely with the disk flags: one invocation can move the disks and the
  topology together.

The workspace must be in a free lifecycle state (`created`,
`stopped`, `absent`). Every other status answers `409`:
`running`, `paused`, `starting`, `stopping`, and `unknown` may
all keep a live volume attached.
The row follows immediately (`msks ls --json`, `msks storage` show
the new ceiling), and a completed resize is announced on the
events channel (`workspace.resized`). At least one flag is
required.

## `msks mount`

Adds one host share to a workspace's mounts (`POST
/api/v1/workspaces/{id-or-name}/mounts`); the spec is the
create's own `--share` grammar:

```text
$ msks mount ws1 /srv/refs:/srv/refs:ro
ws1 mounts /srv/projects:/mnt/projects:rw, /srv/refs:/srv/refs:ro (next start)
```

The outcome line gives the resulting set, and the next-start note joins it
while the change is pending: setting mounts never touches a running VM, and
the next `msks start` attaches exactly this set. The row carries
`mounts_pending`: the daemon compares the configured set with the set the
last start attached. The note rests once a start has taken the set, and an
edit that returns the list to its attached shape shows the set alone. ([what
a mount is](mounts.md#what-a-mount-is) states the lifecycle: the seed each
boot carries bakes the current set, so a restart after a change lands it and
prunes the dropped mounts.) The daemon validates the whole list against its
gates and the row rules (a host path appears once per workspace, guest paths
are absolute and mutually disjoint); a refusal prints its reason on one
line.

## `msks umount`

Removes a workspace's mounts of one host path, every guest path
the share occupies:

```text
$ msks umount ws1 /srv/refs
ws1 mounts /srv/projects:/mnt/projects:rw
```

A host path the workspace does not mount is one local line. The
example returns the list to the shape the last start attached, so
the outcome drops the next-start note. A removal that leaves the
configured set differing from the attached shape keeps the note.
The next start attaches the remainder, and a workspace whose last
mount leaves this way mounts nothing at its next boot. The
[mounts chapter](mounts.md#what-a-mount-is) states the lifecycle.

## `msks rm`

Deletes workspaces (`DELETE /api/v1/workspaces/{id-or-name}`): the row, the
VMM (stopped first, killed if wedged), and the persistent artifacts, the
root overlay and the `/home` volume. The data does not come back. Creating a
workspace under the same name mints a fresh id and starts from the image's
pristine root, with nothing of the deleted instance left to collide with.
One workspace or several:

```bash
$ msks rm my-workspace
my-workspace deleted
$ msks rm scratch-1 scratch-2
scratch-1 deleted
scratch-2 deleted
```

Workspaces are removed one at a time, in the order given. A failure stops
the run there with the API's one-line error, and the ones already removed
stay removed (each success printed its confirmation line). There is no
confirmation prompt: deleting is what `rm` means, and a workspace is
recoverable by recreating it. A workspace recorded on another host answers
409 with the host mismatch given in the error, like every lifecycle command.

## `msks image`

Manages the daemon's image catalog, the surface `docs/images.md`
documents over HTTP, as CLI subcommands. Every subcommand uses the
same client environment as the workspace commands.

### `msks image ls`

One line per registered image (reference, hash (first 12 hex
chars), the default designation, the kernel facts, and when the
image entered the catalog) on the measured grid every listing
shares:

```text
$ msks image ls
ref            hash          default  kernel                description                      imported
debian:13.6    9f2c41ab77de  default  6.12.107+deb13 (raw)  Debian 13.6 cloud image, kernel…  2026-09-22 14:03
alpine:3.20    33aa9db1c4ef  -        6.12.7 (raw)          -                                2026-09-19 09:41
```

The image the daemon designates as default carries the `default` flag. A
bare `msks create` resolves to it. The `description` column carries the
identity the build baked into the image, capped for the row. `image info`
prints it at full length, and an image whose manifest carried none keeps the
column with a dash. The `imported` column shows the moment the archive
entered the catalog, in local time to the minute.

An entry re-imported under the same reference moves the time forward with it. A
renamed row adds an `origin` column beside the ref, showing the archive's own
pair where it differs from the registered one. Rows whose archive pair matches
keep the column with a dash. `--json` prints the listing as the API returns it
(`GET /api/v1/images`, each row's `imported` field in ISO 8601 UTC, each row's
`origin_name`/`origin_version` carrying the manifest pair), stable for
scripting.

### `msks image import`

Registers an archive in the catalog (`POST /api/v1/images`). The
source is a **daemon-side** path (the daemon reads the file from
its own filesystem; the command uploads nothing) or an `https://`
URL the daemon downloads itself:

```text
$ msks image import /srv/images/debian-13.tar
imported debian:13 (9f2c41ab77de)

$ msks image import https://images.example.com/debian-13.tar
imported debian:13 (9f2c41ab77de)

$ msks image import /srv/images/debian-13.tar --name mine --version 1.0
imported mine:1.0 (9f2c41ab77de)
```

A `--name`/`--version` override registers the archive under an
operator-chosen pair. Either key alone, the other comes from the archive's
own manifest, whose pair stays recorded as the row's origin (see
`docs/images.md`). The archive bytes and the content hash stay exactly as
they were. A pair another row already holds is refused with a named error.

A URL source is fetched into the catalog's staging area under the
import ceiling and deadline (`MSKSD_IMAGE_IMPORT_MAX_MIB`,
`MSKSD_IMAGE_IMPORT_TIMEOUT_S`), verified against system TLS
roots, and imported from the downloaded copy. The recorded hash
therefore always reflects the fetched bytes, and the same content
imports once regardless of source. The first image imported into
an empty catalog also becomes the daemon's default. An archive
the daemon cannot read, parse, or fetch answers 400 with the
reason on one line.

### `msks image rename`

Changes a cataloged image's registered name/version; the bytes,
the hash, the manifest origin, and workspaces already booting the
image stay put:

```text
$ msks image rename debian:13 --name mine --version 1.4
renamed debian:13 to mine:1.4 (9f2c41ab77de)

$ msks image rename mine:1.4 --version 1.5
renamed mine:1.4 to mine:1.5 (9f2c41ab77de)
```

Either key alone keeps the other at its registered value. Pass at least one.
The reference forms are the same as `image rm`'s: `name:version`, a bare
name (its newest version), `name@hash`, a full hash, or a unique hash
prefix. The rename goes through the daemon (`PATCH /api/v1/images/<hash>`)
after resolving the reference against the listing. A pair another row
already holds is refused with the daemon's named error (409), and a pair the
reference forms cannot carry (`:` or `@` inside a value, or an empty one) is
refused with a named error too. After a rename the old `name:version` stops
resolving; `name@hash` (with the new name) and bare-hash references keep
booting the same bytes.

### `msks image check`

Boots an image locally and verifies the guest contract point by
point, the pre-import gate `docs/images.md` describes. The
command runs on the image author's host: it needs `/dev/kvm` and
nothing else from msks (no daemon, no state):

```text
$ msks image check workspace-mine-1.0.tar
PASS archive        imported mine:1.0 (1d6a5e782fc0), root autologin getty on the console port
PASS boot           guest answered the console in 3.4s (kernel 6.12.107+deb13-amd64)
PASS console        root autologin getty on the console port
PASS user-data      seed payload ran on first boot
PASS acpi-shutdown  clean shutdown within 120s
PASS root-rw        root is writable and the write survived a stop/start (overlay)
PASS home-label     /home mounted by label msks-home and its write survived a stop/start (volume)
```

A broken contract point fails its row and the exit code is 1,
with the first failure given. `--keep` preserves the throwaway
state dir (serial logs) for inspection. `--egress` adds the DHCP
point (the guest must take a global address over the daemon's own
net stack) and needs root plus an egress-capable default route
(`--uplink` gives another interface).

### `msks image rm`

Removes an image from the catalog. The reference accepts every
form the daemon resolves for workspace create (`name:version`, a
bare name (its newest version), `name@hash` (the full 64-hex
hash), a full hash) and a unique hash prefix (the 12 chars
`image ls` prints):

```text
$ msks image rm debian:12
debian:12 deleted
```

The removal is keyed by the image's hash after resolving the reference
against the listing. An image a workspace still boots is refused (the API's
409 gives the workspace), and a reference that matches nothing exits with
the catalog spelled out so the next try can be copy-pasted. An ambiguous
hash prefix lists the images it matches. Use the full hash or `name@hash`.
(Two imports of the same `name:version`, a rebuilt archive, are the usual
ambiguity, and only the hash forms still identify one of them.)

### `msks image info`

Prints one image's full record from the same listing data:
reference, the archive's own pair (the origin), hash, kernel
facts, cmdline, and the default designation:

```text
$ msks image info debian:13
ref          mine:1.4
origin       debian:13.6
hash         9f2c41ab77de0000000000000000000000000000000000000000000000000000
kernel       6.12.107+deb13 (raw)
description  Debian 13.6 cloud image, kernel 6.12.107+deb13, cloud-init, the egress module set and consent stack
cmdline      console=ttyS0 root=/dev/vda rw
seed         provisioner - (none declared)
default      yes
```

The reference forms are the same as `image rm`'s. The `description` row is
the identity the build baked into the image's manifest. An image whose
manifest carried none prints `- (none baked)`.

### `msks image default`

Designates the image a bare `msks create` boots (the row
`image ls` marks), or clears the designation with `--unset`:

```text
$ msks image default debian:13.6
designated debian:13.6 (98ccf2e1f2db) as the default image

$ msks image default --unset
default designation removed; a bare create falls back to the sole
entry debian:13.6 (98ccf2e1f2db)
```

The reference forms are the same as `image rm`'s: `name:version`, a bare
name (its newest version), `name@hash`, a full hash, a unique hash prefix.
The designation goes through the daemon (`POST /api/v1/images/default`),
survives restarts, and the next `image ls` marks the row. A fresh
`MSKSD_DEFAULT_IMAGE` import at daemon startup still reclaims the slot. The
command is the operator's designation between restarts. A reference that
matches nothing exits with the catalog spelled out, the same miss `image rm`
answers.

`--unset` clears the designation (`DELETE
/api/v1/images/default`) and reports the fallback a bare create
now takes. The sole catalog entry stays what it boots (the line
above). On a catalog holding several images, a bare create needs
`--image` until one is designated again, and the line says so.

## `msks home`

Moves a workspace's `/home` volume through the daemon, useful for backup,
migration to another daemon or seeding a fresh workspace with data.

```bash
msks home export my-workspace              # writes my-workspace.ext4
msks home export my-workspace other.ext4   # gives the output file
msks home export my-workspace - | gzip > backup.ext4.gz
msks home import fresh-ws my-workspace.ext4
msks home import fresh-ws - < backup.ext4
```

The workspace must be stopped (a volume under a running VM answers `409`;
stop it first, `msks stop` is enough). A boot that arrives during a move
waits for it and boots the volume the move left. Moves, boots, and deletes
serialize per workspace, and the waiter answers a named 409 after
`MSKSD_MOVE_WAIT_TIMEOUT_S` instead of hanging.

Export streams the volume file's bytes verbatim and prints one confirmation
line (`exported my-workspace (2097152 bytes) to my-workspace.ext4`). `-` writes
the bytes to stdout and moves the note to stderr, so a pipe stays clean for
gzip or ssh. A reader that goes away mid-stream (`| head`, a compressor on a
full disk) prints one line on stderr and exits non-zero. Import uploads the
named ext4 image (or stdin, for `-`), the daemon replaces the volume with it,
and the reply is the byte count: `imported 2097152 bytes into fresh-ws`.

The daemon refuses with one line and exit 1. Refused: a file that is not an
ext4 image (`msks: 400: the request body is not an ext4 image ...`), a
workspace in the wrong state, and everything the API's refusals give (a
foreign host). An import keeps the workspace's existing volume until the
upload completes and passes the ext4 check. A cut-off upload changes
nothing.

The typical pairings are backup (`export`, later `import` back into the same
workspace after a `rm` + `create`) and migration (`export` on one daemon,
`create` + `import` on another). Seeding is the third: build an ext4 image
with the data a fleet of workspaces starts from, then import it into each
fresh one. Inside a workspace with egress, git and rsync over the forward
(`docs/networking.md`) carry day-to-day code. The volume moves are for the
whole `/home` at once.

## `msks secret`

Mint a secret placeholder (daemon-wide by default, scoped with `--workspace`)
and the daemon keeps your value as per-recipient ciphertext blobs in its
database. See also [docs/secrets.md](secrets.md).

```bash
# from a password manager, nothing touches disk: daemon-wide,
# one sentinel for every workspace on the daemon
op read 'op://Vault/github/credential' \
  | msks secret mint --name github_api \
      --dest api.github.com --secret-file -

msks secret mint --workspace myws --name pypi \
  --dest .pypi.org --dest pypi.org \
  --ttl 86400 --secret-file ./token    # scoped, suffix + exact

msks secret mint --workspace ci,deploy --name pypi \
  --dest .pypi.org --secret-file ./token   # one row, two workspaces

msks secret ls                           # placeholders + coverage + lock state
msks secret unlock                      # decrypt the pending blobs the
                                         # configured identity key opens
msks secret renew --workspace myws --name pypi --ttl 86400
msks secret revoke --name github_api     # the daemon-wide row of the label
msks secret revoke --workspace ci,deploy --name pypi
msks secret coverage myws scoped         # exempt myws from daemon-wide rows
```

`--secret-file` takes a path or `-` for a pipe, and the value is
whitespace-stripped at both ends. The secret is never accepted as a
command-line argument, because argv appears in process lists and shell
history.

An empty file is refused before any network roundtrip. `--dest` repeats and
binds the swap: an exact host (`api.github.com`) or a suffix that covers every
host under a domain (`.github.com`). `--workspace` takes one ref or a comma
list and repeats. Omitted, the mint covers every workspace on the daemon (one
row, one `mskssec2_` sentinel), while a scoped mint prints a `mskssec1_`
sentinel, so the string alone gives its reach. The mint prints the sentinel;
the listing and the audit omit it (`secret ls` never shows one). The daemon
serves a row's sentinel back on demand over the token-authenticated
API. `revoke` and `renew` take the same targeting the mint took. `revoke` takes
effect on the next request and retires the whole row everywhere at once, and
`renew` extends a `--ttl` lifetime in place with the sentinel unchanged.

## `msks console`

An interactive shell inside a workspace, over the daemon's console
websocket. This is useful when `msks ssh` is not working properly, because it
works when the ssh daemon is not responding for whatever reason. The command
boots the workspace first when the daemon reports it as not running. The
notices print on stderr while the boot runs, then the session attaches:

```text
$ msks console my-workspace
msks: my-workspace is stopped; starting it
msks: my-workspace running
(workspace prompt)
```

The session is a root shell: the console is the failsafe path,
served by the guest's autologin root getty on the virtio-console
port. It is a raw byte stream over the daemon's authenticated
websocket, with no user selection and no in-band protocol. Reach
a workspace user's shell with `su - <name>` inside the session,
or over ssh for the full interactive posture:

```text
$ msks console my-workspace
(workspace prompt, as root)
```

A workspace that is already running attaches with no preamble.
Two states get special handling:

- **`starting`**: another client's boot is in flight. The console
  waits for it (polling up to two minutes) and attaches when it
  lands, instead of racing a second boot into the daemon's
  double-launch guard.
- **`paused`**: refused with the honest reason, because the
  daemon has no resume; the message gives the recovery (`msks
stop` it, then `msks start` again).

A start that loses a race (the daemon reports `stopped`, another
client boots it in the gap) re-checks and attaches to the winner.

Ctrl-] detaches and leaves the workspace running. Ctrl-C and Ctrl-D reach
the guest. To type a literal Ctrl-] into the guest, press it twice quickly
(the second press within 50 ms): the pair delivers one Ctrl-] byte. A single
Ctrl-], or one followed by any other byte, detaches, and the byte that
followed the escape is consumed with it, so a paste that happens to contain
a lone Ctrl-] detaches the session. Large pastes travel as a few websocket
frames (4,096-byte chunks), not one frame per byte. The session needs a tty
on both stdin and stdout.

## `msks forward`

Forwards a guest TCP port on this command's stdio over the daemon's forward
websocket. The command boots the workspace first when the daemon reports it as
not running (the same notices as `msks console`), then bridges bytes unexamined
in both directions: a tty is not required, and binary protocols (ssh, rsync)
pass through it cleanly:

```bash
msks forward my-workspace 22                  # stdio: ProxyCommand shape
msks forward my-workspace 8080 --local 8080   # loopback listener
```

`--local PORT` binds `127.0.0.1:PORT` instead of stdio. Every accepted
connection opens its own forward websocket, so parallel clients (a browser
and a curl, two ssh sessions) are independent sessions. Refusals print one
line (the daemon's close codes give the cause: no NIC, not running, service
not listening, still booting), while a clean end of stream (the guest
service closed, stdin EOF) leaves exit code 0.

## `msks proxy`

The daemon's persisted workspace proxies: listeners the daemon owns, rows in
its database, re-established at every workspace start. (`msks forward` is the
ephemeral session — stdio or a client-side bind that dies with the command.)
[The proxies chapter](proxies.md) walks the concept and the usual work; this
section is the reference.

Two directions, written the way ssh writes them. `listen : target`, left to
right; the flag letter says which side listens:

```bash
msks proxy add my-workspace -L 2201:localhost:22
# the daemon binds 127.0.0.1:2201 on its own host and bridges every
# connection to the workspace's port 22
msks proxy add my-workspace -L 0.0.0.0:2201:localhost:22
# same, listening on every interface of the daemon's host
msks proxy add my-workspace -R 9200:localhost:9200
# reverse: the workspace's gateway listens on 9200, and every guest
# connection bridges to port 9200 on the daemon's host
msks proxy add my-workspace -R 5432:db.internal:5432 --label db
# the gateway's 5432 bridges to db.internal:5432, dialed by the daemon
```

The `-L` spec is `[BIND:]PORT:HOST:PORT`; the `-R` spec is `RPORT:HOST:TPORT`
(its bind is the workspace's tap, a runtime fact). The host field plays the
same role it plays in ssh: it names the end that makes the connection. In
`-L` that end is the workspace itself, so the field accepts exactly two
spellings — `localhost` or `127.0.0.1` — and anything else refuses:

```text
msks: -L's host field names the workspace itself; only localhost or
127.0.0.1 is allowed — to reach a daemon-side service, use -R
```

The workspace cannot dial out on a forward's behalf (no agent runs inside it),
so a forward always lands on the workspace itself. Reaching a daemon-side
service is the `-R` direction.

A remote proxy is the reverse of a forward one. The guest dials `host.msks`
on the listen port. The workspace's resolver answers that name with its DHCP
gateway address (`ip route show default` inside the workspace prints the same
address). The daemon bridges the connection to the daemon-side service the
spec names. With the example stored, `curl http://host.msks:9200` inside the
workspace reaches the host service. A workspace created without
egress holds no proxies: its row has no NIC to reach.

The daemon dials a remote proxy's target itself. The consent machinery gates
the guest's traffic through the tap, not the daemon's own connections. A
remote row therefore reaches its target without a verdict.

`add` is declarative. Asking twice for the same spec returns the standing
row. A different spec on the same bind is refused with the row that holds it.
A forward proxy binds at once, on a stopped workspace too; each connection
refuses until the guest runs. A remote proxy on a stopped workspace records
the row, and the workspace's next start brings its listener up. A stop
closes every listener the workspace owns. A proxy whose bind fails (the
port is taken) records the failure as its status with the cause, and every
later start retries it. A remote proxy whose firewall
swap fails stays unreachable from the guest, and its status carries the cause
until a later swap lands.

```bash
msks proxy ls            # every stored proxy, with live status
msks proxy ls --json     # the same rows as JSON
msks proxy rm my-workspace 3   # remove one (listener closes at once)
```

The listing shows each row's direction, listen and target endpoints, and live
status (`listening`, `stopped`, `failed` with the cause). Removing a proxy
closes its listener when one serves and drops the row from the daemon's
database.

## `msks egress`

Decide, watch, and inspect a workspace's outbound-destination egress consent
verdicts. A workspace in `interactive` mode holds each new outbound
connection's first packet until a decider allows or denies it. `static`
workspaces allow only their create-time allowlist. `allow` workspaces (the
create default) record off-list destinations and pass them.

```text
msks egress rules ws-dev          # the mode, allowlist, and in-effect verdicts
msks egress requests ws-dev       # the consent rows (audit trail), newest first
msks egress requests ws-dev --decision pending
msks egress decide ws-dev <request-id> allow --duration 5m
msks egress decide ws-dev <request-id> deny --duration forever
msks egress revoke ws-dev <request-id>
msks egress mode ws-dev interactive          # switch the posture;
msks egress mode ws-dev static --allow .debian.org   # --allow replaces
msks egress mode ws-dev static --offline     # ... the allowlist (--offline
                                              # confirms an empty static)
msks egress watch ws-dev          # stream frames; registers this client as
                                  # a decider (holds wait only while one is
                                  # connected)
msks egress watch ws-dev --decide --duration forever
                                  # the same, prompting y/n per request
```

`decide` and `revoke` identify the request by the id `watch` and `requests`
print (the full id, copy-pasteable). Both act only on the given workspace's
requests. A portless destination (a non-TCP/UDP flow) prompts as `host (all
ports)`: an allow opens every port on the host for the duration. Durations:
`once` (this connection only; a reconnect re-prompts), `5m`, `15m`,
`tilrestart` (until the workspace VM stops; the default), `forever` (the
workspace's lifetime, replayed at every boot). `revoke` undoes an in-effect
verdict immediately: the flow rules and the destination's live connections
drop, and new connections gate again.

`mode` switches a workspace's posture without recreating it. A running
workspace swaps live: the whole firewall table re-applies in one
transaction, established connections survive (an attached `msks ssh` session
stays up), the resolver flips with the chain, and any held request answers
deny when the switch leaves `interactive`. A stopped workspace builds the
new posture at its next start. The command's output line says which
happened.

Verdicts carry across switches: an `allow forever` granted under
`interactive` keeps acting under `static`. Switching to `static` therefore
freezes the workspace at everything approved so far: name-keyed verdicts in
full and, on a direct gated-to-gated switch, the kernel-side pins too. A
round-trip through `allow` rebuilds the pins from the durable rows
(`forever` verdicts re-pin at entry; a timed address-keyed allow re-prompts
or re-learns on the guest's next resolution). `--allow SPEC` (repeatable,
create's grammar) replaces the allowlist. Omitted, the workspace keeps its
list. Switching to `static` with nothing effectively allowed (an empty
allowlist and no in-effect allowed verdict) is refused with a message giving
the fix: pass `--allow` entries, or pass `--offline` to run the switch
anyway. That posture answers every name NXDOMAIN: an offline workspace
reachable only by ssh.

## `msks key`

Displays the workspace's ssh identity. Every workspace a local-backend daemon
creates carries the public half its create sent, the operator's key, whose line
the guest's first boot planted into `authorized_keys` for root and the
workspace's login user. The fetch takes the same
`MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` environment as every other command:

```bash
msks key my-workspace                    # the public authorized_keys line
```

The command is public-only: the private half is the operator's own key, and
it never crossed the API to begin with. It lives wherever `identity_file`
points, or in the agent that holds it.

## `msks ssh`

Use `ssh` to access a workspace, using a forward.

```bash
msks ssh my-workspace              # as the workspace's login user
msks ssh my-workspace -- -l root   # the recovery login
msks ssh my-workspace -- -A        # forward your agent ($SSH_AUTH_SOCK)
msks ssh my-workspace -- -L 8080:localhost:80
```

Options you pass here win for the invocation. The config file's
`ssh_options` key remembers a set for every session instead. See [its
section](#ssh_options) for the precedence between the two.

The command boots the workspace first when the daemon reports it
as not running (the same notices as `msks console`). It then
fetches the identity over the authenticated API and runs `ssh`
with the forward websocket as its ProxyCommand (`msks forward
<ws> 22`).

A session that booted its workspace waits out the guest's first boot. The
daemon reports `running` while the guest's sshd is already up but the identity
seed has yet to land in `authorized_keys`. The command therefore probes the
login first, with a throwaway `true` as the remote command, retried for up to
30s with a one-line notice between attempts. It opens the real session,
interactive or one-shot, once the guest accepts the workspace key. A remote
command runs exactly once either way.

The probe carries only the session's login user and agent-forwarding setting,
plus msks's own transport, its quiet flag, and a `true` command. A session's
tunnels and other ssh options therefore cannot hold the wait open or alter
it. The session itself keeps every option.

The API serves the public half, and the private half resolves at
session time. Your own agent comes first: when `SSH_AUTH_SOCK`
gives a live agent holding the workspace's key, matched on the
key material (so the file it came from and its name do not
matter), the session signs through that agent. The private half
never enters the msks process, encrypted or not. Then the
operator identity: `identity_file` (or `MSKSC_IDENTITY_FILE`),
the same key the create planted. When neither holds the
workspace's key, the command exits with one line giving both
rungs and the ways back in (ssh-add the key, point identity\*file
at it, or ssh -i directly).

Host keys land in a per-workspace `known_hosts` under the msks
cache root, under `accept-new`. The root is the `cache_dir` key
(`MSKSC_CACHE_DIR`) when it is set, else `XDG_CACHE_HOME` or
`~/.cache/msks`, then `<workspace-id>/known_hosts`. The cache
keys on the workspace's immutable id, so a workspace recreated
under the same name starts with a fresh cache and trusts its own
first-boot keys again. Every invocation pins under that one id
alias, whichever way the workspace was addressed: `msks ssh
<name>` and the TUI's shell action record the same `known_hosts`
entry, and an operator's own `~/.ssh/config` `Host <name>` block
no longer matches the session (the id is the hostname msks ssh
dials; use the id in a matching block instead).

Host keys persist across stop/start on the workspace's overlay, so the
first-connection entry keeps matching. When the daemon recreates the overlay
under a live workspace (a healed loss, a factory reset), the guest regenerates
its host keys: the daemon advances the workspace's disk epoch, the cache
rotates with it, and the next connection pins the fresh keys with a notice
marking the recreation instead of refusing. A renewed seed rotates the same
cache the same way. A changed mount set or a rotated interception CA renews
the seed. The renewal's new cloud-init instance-id re-provisions the guest,
and the stock cloud-init ssh module regenerates the guest's host keys. The
workspace's seed epoch counts every bake and renewal, and the cache directory
rotates with the count: `<id>.r1/`, and `<id>.e1.r2/` beside a disk rotation.
The next connection re-pins instead of refusing. A rotation leaves the previous
cache behind (the `<id>/` directory, then `<id>.e1/`, `<id>.r1/`,
`<id>.e1.r2/`, ... as rotations accumulate); those directories are dead: delete them at
will. The alias block keeps its own known_hosts under
`~/.cache/msks/msks-<ws>/`; the two paths record the same host key
independently.

Agent forwarding asked for on the command line (`-A`, or `-o
ForwardAgent=yes`) forwards **your** agent, the socket
`SSH_AUTH_SOCK` gives. `ssh-add -l` inside the workspace lists
the same keys as on the host, and `git push git@github.com`,
`ssh`, and friends sign with the host's credentials.

The session authenticates through a transient in-process agent (above), and
stock ssh would forward _that_ one when asked to forward at all. msks
therefore rewrites the request into `ForwardAgent=<your socket>`, an
explicit path. An OpenSSH client 8.2 or newer parses the form. An older
client exits with its own usage error (RHEL 8's 8.0, for one). The workspace
identity stays an authentication credential and reaches the guest as nothing
else. A request that already gives a socket (`-o
ForwardAgent=/path/to/sock`) keeps its own. `-A` with no live agent behind
`SSH_AUTH_SOCK` exits with one line marking it. Forwarding set by an ssh
config file keeps the stock meaning under this command (it forwards the
session agent); the command line is where your agent is given.

Everything after the workspace id (the `--` is optional; any argument ssh
would take works verbatim) is passed to ssh. A passthrough that starts with
a plain word is a remote command (`msks ssh my-workspace -- uname -a`). When
it starts with an option, ssh's own separator carries a command after the
options (`msks ssh my-workspace -- -A -- uname -a`). `-A` forwards your
agent, the same forwarding the paragraphs above describe (before it
forwarded the session agent, the workspace identity, which served nested
logins to the same workspace: ssh from inside to `localhost`). That use now
points at the identity explicitly instead: `identity_file` gives your own
key, and your agent holds it for the nested login.

The exit code is ssh's own (255 for ssh failures), not the msks
command set. The session agent lives exactly as long as the
`msks ssh` process; `ControlPersist`/mux sessions that outlive it
belong to the alias path, whose identity is a key file. Explicit
passthrough options override the injected defaults (your own
`UserKnownHostsFile`, a different `ProxyCommand`) exactly as with
stock ssh: ssh takes the first value a repeated option receives,
and the passthrough comes first.

The login user is the workspace's recorded login user: the
create-time `--user`, defaulting to the creating operator's name.
A workspace created before records none, and the dial is `root`.
ssh arguments that give a user (`-l root`, `-o User=root`)
override it. The host argument ssh sees is the workspace id
itself; the transport is the proxy, so the name never resolves.
The ProxyCommand runs this very client, in the absolute
interpreter and module form, so `msks` need not be on the ssh
child's PATH. It inherits your environment: `MSKSC_URL`,
`MSKSC_TOKEN`, and `MSKSC_CAFILE` must be set where ssh runs.
The networking chapter's alias workflow covers the `Host msks-*`
configuration that hides all of this.

## `msks rsync`

Stock rsync against a workspace over an ssh forward, with the
identity staged in memory: one command does the whole setup
itself. The workspace boots when needed, the identity stages in
memory, and the copy opens its own forward:

```bash
msks rsync my-workspace -- -av ./site/ root@:/root/site/    # push
msks rsync my-workspace -- -av root@:/root/out.tar ./out.tar  # pull
msks rsync my-workspace -- -aP ./src/ :src/         # as the login user
```

Everything after the workspace id (the `--` is optional) is given to rsync.
msks parses no rsync flags. The one shaping pass is the empty host: a path
whose host is empty (`:/root/site/`, or `root@:/root/site/`) targets the
workspace this command gives, its host filled in as the copy runs. The
direction, push or pull, comes entirely from the rsync arguments, and the
login user comes from the paths the same way. `root@:/root/site/` logs in as
root; a bare `:src/` logs in as the workspace's login user, whose home is
the persistent `/home` volume (the shape for copies under your own account).
A path that gives a host keeps it, and the transport is the proxy either
way, so the name never resolves. `::module` paths are rsync's daemon
protocol against port 873; the workspace image runs no rsync daemon (sshd
stays its one inbound service), so that form is left as typed and fails as
rsync's own error.

A word that starts with a colon reaches this fill even when it is
the split-apart value of an rsync option (`--filter :rules`),
because msks does not parse rsync's flags to tell the two apart.
That spelling uses the attached form (`--filter=:rules`), which
msks never touches.

The command boots the workspace first when the daemon reports it as not
running (the same notices as `msks console`), fetches the identity over the
authenticated API, and runs the host `rsync` with the same transport `msks
ssh` uses. That transport is the forward websocket as the ssh ProxyCommand
(each rsync connection opens its own), the per-workspace `known_hosts` under
`accept-new`, and the identity staged in a transient in-process ssh-agent.
The private half exists only in memory. rsync's ssh children identify it by
its public half and sign through the agent socket, and the command writes no
key file. The operator identity (`identity_file`), a `--pubkey` workspace,
and the earlier row shapes (daemon-minted, client-minted) all work: the
private half resolves where it lives. That may be the operator's agent first
(the same key-material match `msks ssh` applies), the operator's own file,
the API, or the per-workspace file. A key that resolves nowhere on this
client exits with the line giving where it can be. A session that booted its
workspace waits out the guest's first boot exactly as `msks ssh` does: a
probe login retries behind the identity seed (up to 30s, one line between
attempts), and the copy runs once the guest accepts the workspace key.

The login user is the workspace's recorded login user, stated as a generated
per-session ssh config, so rsync's own `user@` path spelling overrides it
(`root@:/root/site/` logs in as root). An explicit `-e` in the passthrough
replaces msks's remote shell entirely (rsync takes the last `-e`, and the
empty-host fill keeps pointing at the workspace id your own transport must
then reach), the same override shape ssh passthrough options have. The host
prerequisites are `ssh` and `rsync`. The exit code is rsync's own.

## Errors, exit codes, and timeouts

Every command fails with one readable line on stderr and exit
code 1, never a traceback:

```text
msks: set MSKSC_TOKEN to a daemon token (MSKSC_URL for a non-default daemon)
msks: cannot reach https://192.168.77.2:8660: [Errno 113] ...
msks: 401: invalid or revoked token
msks: 409: workspace exists
msks: 422: body.id: String should match pattern '^[a-z0-9][a-z0-9-]*$'
```

The status lines carry the daemon's `detail` field verbatim. A
validation failure (422) reports each problem as `field:
message`, joined on one line: the same facts the API returns,
minus the JSON scaffolding. A timeout says so explicitly, because
the daemon may still complete a request the client stopped
waiting for. Argument errors exit with code 2 (the convention the
argparse-era CLI set and the typer-based one keeps); success is
0; a Ctrl-C during a long boot prints `msks: interrupted` and
exits 130.
