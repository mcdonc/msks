# The CLI client

The `msks` command is a thin client for the daemon's `/api/v1` REST
surface. It runs from any host that can reach the daemon — a dev box,
a CI runner, the appliance itself — and every command authenticates
with the same bearer token the REST API uses.

The command set covers the operator loop:

```bash
msks ls                      # what exists, and what state is it in
msks create ws                # make a workspace
msks shell ws                 # boot it if needed, then work inside it
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
`msks shell` (#21), fine for a lab network and worth closing before
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

| Flag         | API field  | Meaning                                           |
| ------------ | ---------- | ------------------------------------------------- |
| `--image`    | `image`    | Catalog ref: `name:version`, bare name, or hash   |
| `--kernel`   | `kernel`   | Explicit kernel path (skips the catalog)          |
| `--initrd`   | `initrd`   | Explicit initrd path                              |
| `--rootfs`   | `rootfs`   | Explicit rootfs path (skips the catalog)          |
| `--cmdline`  | `cmdline`  | Explicit kernel cmdline                           |
| `--cpus`     | `cpus`     | vcpus, 1–64 (daemon default: 2)                   |
| `--mem-mib`  | `mem_mib`  | Guest memory MiB, 64–32768 (daemon default: 1024) |
| `--root-mib` | `root_mib` | Persistent root overlay size (daemon default)     |
| `--home-mib` | `home_mib` | Persistent /home volume size (daemon default)     |

Only the flags you pass are sent — unset flags let the daemon apply
its own defaults. An `--image` reference resolves against the
daemon's image catalog (`docs/images.md`); explicit `--kernel` and
`--rootfs` bypass it. Size ranges are enforced server-side; a value
outside them comes back as a validation error (below).

`--start` boots the workspace right after creating it:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace
attach with: msks shell my-workspace
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
with `msks start` — or just `msks shell` it: the shell command boots
a not-running workspace on its own (below).

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

## `msks shell`

An interactive shell inside a workspace, over the daemon's console
websocket. The command boots the workspace first when the daemon
reports it as not running — the notices print on stderr while the
boot runs, then the session attaches:

```text
$ msks shell my-workspace
msks: my-workspace is stopped; starting it
msks: my-workspace running
(workspace prompt)
```

A workspace that is already running attaches with no preamble. Two
states get special handling:

- **`starting`** — another client's boot is in flight. The shell
  waits for it (polling up to two minutes) and attaches when it
  lands, instead of racing a second boot into the daemon's
  double-launch guard.
- **`paused`** — refused with the honest reason: the daemon has no
  resume, so the message names the recovery (`msks stop` it, then
  `msks start` again).

A start that loses a race — the daemon reports `stopped`, another
client boots it in the gap — re-checks and attaches to the winner.

Ctrl-] detaches and leaves the workspace running; Ctrl-C and Ctrl-D
reach the guest. The session needs a tty on both stdin and stdout.
See the README's workspace-shell section (#21) for the transport
story.

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
