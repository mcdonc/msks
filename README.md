# msks

Microvm workspace daemon — a Python analogue of klangkd that runs
workspaces as cloud-hypervisor microvms instead of podman containers.

## Development

Install [nix](https://nix.dev/manual/nix/latest/install/) and
[devenv](https://devenv.sh/getting-started/) on any Linux host — a
NixOS host is not required. Enter the environment (Python 3.14, uv
venv, cloud-hypervisor + ch-remote, qemu, the pytest toolchain,
xenon):

```bash
devenv shell
```

All scripted/CI invocations disable dotenv loading:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- <command>
```

Run the test suite the way CI runs it (`-n auto` is never optional —
see AGENTS.md for the coverage story):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- unit-tests
```

Scoped iteration picks only tests whose coverage touches changed lines:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- testmon
```

Complexity gate (also runs as a pre-commit hook):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks-xenon
```

The Python-side pre-commit gates' full offender list in one pass —
all ruff and deferred-import findings, every xenon offender, and the
jscpd report at once, plus, when sources under `src/msks/` changed,
every missing coverage line and branch arc for the changed files
after one gated suite run:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks-preflight
```

### VM guest assets

The workspace guest is **Debian 13 (trixie)**, straight from Debian's
official genericcloud cloud image (#30, #41): systemd as PID 1, apt,
cloud-init, and Debian's own kernel, initrd, and modules — booted
directly (no BIOS/UEFI) by cloud-hypervisor. The image is pinned by
its dated cloud.debian.org URL and sha512, and the build turns it
into the msks boot contract — `vmlinux` (Debian's bzImage,
`CONFIG_PVH=y`), `initrd`, a pristine ext4 base rootfs, and
`guest-manifest.json` — with a small overlay of msks systemd units
(vsock console, serial autologin, the `/home` mount, the cloud-init
dropins that pin NoCloud and keep cloud-init off the guest's
networking). Extraction is fully unprivileged:
qemu-img convert, partition slice, `debugfs rdump`, `mke2fs -d`.
Measured boot on bare-metal KVM: the vsock shell prompt at ~2.8s,
the serial login prompt at ~7.5-8.6s (#37 tracks the <5s interactive
goal).
Root writes persist through the per-workspace overlay (#14), and
`/home` is the workspace's own ext4 volume; `apt` reaches the
upstream through the workspace's egress NIC — served by the
daemon's consent stack whenever the daemon holds CAP_NET_ADMIN
(#52). The repack runs under
fakeroot so the image is root-owned with sane password-file modes,
and the setuid binaries the workspace user's sudo needs ride the
same fakeroot session as uid-0 inodes (#169).
Stopping a workspace presses the ACPI power button
(`vm.power-button`): the guest's systemd-logind runs the clean
poweroff that flushes its persistent disks (#14).

Every step of the build runs inside the repo on any Linux host with
nix:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks-build-guest
```

The artifacts (plus a `guest-manifest.json` describing them and the
boot cmdline) land in `.devenv/state/guest/` (relocatable with
`GUEST_DIR`). Boot one interactive VM from them —

```bash
msks-demo-vm
```

— which attaches the guest's serial console to your terminal
(`poweroff -f` inside the guest or Ctrl-C stops it) and leaves
`ch-remote` reachable on the printed API socket path.

Boot tests self-provision: when `.devenv/state/guest/` holds built
artifacts and
`/dev/kvm` is usable, the smoke tests find them without any exported
variables (`TEST_VMLINUX` / `TEST_INITRD` /
`TEST_ROOTFS` / `TEST_CMDLINE`
keep precedence when you do export them). When the
artifacts were never built, or `/dev/kvm` is missing or not accessible
to your user (add yourself to the `kvm` group,
`users.users.<name>.extraGroups = [ "kvm" ];` on NixOS, then
re-login), the smoke tests skip themselves.

### Running the daemon (msksd)

```bash
devenv --quiet -O dotenv.enable:bool false shell --
MSKSD_STATE_DIR=/tmp/msksd MSKSD_BOOTSTRAP_TOKEN=dev-secret MSKSD_PORT=8660 msksd
```

- **Trust**: with no `MSKSD_TLS_CERT`/`MSKSD_TLS_KEY`, msksd generates a
  self-signed CA + certificate into the state dir on first run and logs the
  CA fingerprint — pin it on first connect (trust-on-first-use, like SSH).
  The CA key lives beside the database under the state dir.
- **First credential**: `MSKSD_BOOTSTRAP_TOKEN` seeds one bearer token,
  inserted once when absent; it is visible in the process environment to
  the same user (acceptable for a single-user local daemon — unset it after
  minting real tokens). `--no-tls` serves plain HTTP for development.
- **Events**: `wss://host/api/v1/events?token=<token>` streams workspace
  status transitions (browsers cannot set websocket Authorization headers,
  so the token rides the query string). Because that token would appear in
  an access log, uvicorn's access log is **off by default** — set
  `MSKSD_ACCESS_LOG=true` only if you accept credentials in logs. A bad
  token rejects the websocket handshake with HTTP 403.
- **Rotating the bootstrap token**: setting `MSKSD_BOOTSTRAP_TOKEN` to a
  new value _adds_ a token; the previous bootstrap credential stays valid
  until revoked via the API.
- **Config file**: settings also live in a YAML file — `msksd --config
/path/to/msksd.yaml` reads exactly that file, `--config=none` reads env
  vars only, and a bare `msksd` resolves `$MSKSD_CONFIG_DIR/msksd.yaml`
  (default `~/.config/msksd/msksd.yaml`), generating a commented template
  on first run. Env vars override the file; see `docs/config.md` for the
  key-by-key reference.
- **Schema**: the SQLite database is created and upgraded by Alembic at
  startup (inside the package: `msks/migrations`).

### The dev daemon (the default `processes up`, #231)

msksd runs FIRST-LEVEL on the dev host — cloud-hypervisor on the
real `/dev/kvm`, per-VM taps, the egress consent stack in the host
kernel — through the `msks-caps` capability wrapper the host's
NixOS config installs. `devenv processes up` runs it attached in
the foreground (Ctrl-C stops it, workspaces get their stop cycle);
`msks-dev` runs the same script by hand in a terminal. Each
worktree state dir (`.devenv/state/msksd`, `MSKSD_STATE_DIR`
relocates) seeds its own bootstrap token, API port, and
port-derived egress subnet, so concurrent worktrees run one daemon
each; a state-dir lock refuses a second daemon on the same catalog.
The client presets below land in every devenv shell. (An older
hand-run flow without KVM — `msks-dev-ready` — still exists for
API/client-only work:)

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks-dev-ready
export MSKSD_STATE_DIR="$PWD/.devenv/state/msksd" MSKSD_BOOTSTRAP_TOKEN="$(cat .devenv/state/msksd/bootstrap-token)"
msksd &                                    # serves https://127.0.0.1:8660
MSKSC_URL=https://127.0.0.1:8660 MSKSC_CAFILE=$PWD/.devenv/state/msksd/msks-ca.pem \
  MSKSC_TOKEN="$(cat .devenv/state/msksd/bootstrap-token)" msks ls
```

The state lives in `.devenv/state/msksd/` (TLS CA, bootstrap token,
sqlite catalog, workspace volumes). The dir honors `MSKSD_STATE_DIR`
— export it before `msks-dev-ready` to relocate it, as an absolute
path: the script anchors a relative value below the repo root while
the daemon resolves one against its own CWD, so only an absolute
value moves both to the same place.
Gitignored but NOT disposable-clean — `git clean -xfd` deletes all
of it, along with the dev daemon's personal `msksd.yaml` at the
repo root (#262). Egress (and `msks ssh`, whose forwards ride the egress NIC)
needs `CAP_NET_ADMIN` (#101): the dev host's wrapper grants it, so
egress workspaces run first-level; on a host without the grant a
workspace created without `--no-egress` refuses to start — the 503
names `MSKSD_EGRESS_ENABLED`, and the fix is `--no-egress` or the
host config in `nix/module.nix`. Daemon edits restart with Ctrl-C
and re-run (`--reload` restarts on tree change, #144).

Known quirk worth knowing: cloud-hypervisor v52 rejects writes to
sector 0 on disks without an explicit `image_type` (a QCOW2
misdetection guard), which breaks any guest writing an ext4
superblock — every disk the daemon creates declares
`image_type: Raw`.

### The image catalog (#40)

Images are plural: msksd holds a catalog under
`<state_dir>/images/` — every registered archive keyed by content
hash, with the boot files unpacked once per hash (workspace launches
never unpack anything).

```bash
curl -sk -H "authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"source": "/nix/store/...-msks-guest/workspace-debian-13.6.tar"}' \
  https://127.0.0.1:8660/api/v1/images        # import
curl -sk -H "authorization: Bearer $MSKSC_TOKEN" \
  https://127.0.0.1:8660/api/v1/images        # list (name/version/hash/default)
```

A workspace create selects an image by reference — `"image":
"debian:13.6"` (or a bare `name` for its newest version, or a hash);
with no image and no explicit artifacts the designated **default**
resolves. The first import becomes the default; `MSKSD_DEFAULT_IMAGE`
points the daemon at an archive to import on first boot, and the dev
daemon seeds it from its state dir's `default-image` symlink
(`msks-build-guest-archive` converges it) — a bare
`POST /workspaces` works on a fresh daemon with nothing else
imported. Explicit `kernel`/`rootfs` fields still win over
the catalog (the shape the tests and dev flows use).

### The client CLI (`msks ls`, `msks create`, `msks start`, `msks stop`, `msks rm`) (#59, #66)

The client also covers the non-interactive half of the operator flow:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks ls
devenv --quiet -O dotenv.enable:bool false shell -- msks create my-workspace --start
```

`msks ls` prints one line per workspace (name, id, status, image
hash, host); `--json` prints one JSON document for scripting. A
workspace carries two identity fields (#246): the name you choose at
create — the label every command addresses it by — and the id the
daemon mints, immutable and never reused, keying the artifact paths
and the client caches; either reference reaches the same workspace.
`msks create`
POSTs the same body the API accepts — `--image` picks a catalog
reference, `--cpus`/`--mem-mib`/`--root-mib`/`--home-mib` size the VM,
`--user-data` attaches a first-boot provisioning script (#41, a
cidata seed disk the guest's provisioner runs once), and explicit
`--kernel`/`--rootfs` (with optional `--initrd`,
`--cmdline`) bypass the catalog. `--start` boots the workspace right
after creating it, so `msks create ws --start` then `msks console ws`
is the two-step path from nothing to a shell; `msks start <ws>` boots
an existing workspace later, `msks stop <ws>` powers one off (a
graceful, deadline-bounded shutdown; the data survives), `msks rm
<ws>…` deletes one or more workspaces together with their persistent
root overlay and `/home` volume, `msks console <ws>` boots one itself
when the daemon reports it as not running (a notice prints on
stderr while the boot runs), and `msks home export/import <ws>`
moves the whole `/home` volume through the daemon for backup,
migration, and seeding (#80). All commands use the same
`MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` environment as `msks console`;
failures (unreachable daemon, timed-out request, bad token, API or
validation errors) print one readable line instead of a traceback.
See `docs/cli.md` for the full command and environment reference.

### Workspace egress networking (#52)

Workspaces are networked from creation — `msks create ws`, or a bare
`"id"` on the API, boots with egress; `msks create ws --no-egress`
(or `"egress": false`) boots NIC-less — and the whole path lives in
the daemon's own net stack:

```text
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain
                                                │  guest → uplink: accept
                                                ▼
                                     NAT (masquerade) → host uplink
```

The daemon is the guest's only DHCP server and resolver: each
egress workspace gets a dedicated /30 from `MSKSD_EGRESS_SUBNET`, a
DHCP offer naming the tap as gateway and resolver, a small DNS
forwarder on that resolver, and NAT out the host's uplink
(`MSKSD_EGRESS_UPLINK`). Everything derives deterministically from
the workspace id, so stop/start cycles rebuild the same network;
stop and delete tear the tap, chain, and services down again. The
guest side is just the image's DHCP client (systemd-networkd +
resolved in the overlay); a workspace without egress presents no
NIC, on every backend.

Egress arms while `MSKSD_EGRESS_ENABLED=true` and the daemon holds
`CAP_NET_ADMIN` — the dev host's wrapper grants the capability, and
`nix/module.nix` does the same for a deployment host. A daemon that
cannot arm the plumbing still serves everything else, and an egress
workspace refuses to boot with the cause named (boot those with
`--no-egress`). Per-flow consent (allow/deny holds on each new
connection) is #69.
See `docs/networking.md` for the full reference.

### The workspace LLM proxy (#259)

Each workspace's tap carries its own proxy service when the daemon
is configured for it: an OpenAI-shaped proxy (`/v1/models`,
`/v1/chat/completions`, streaming included) served by msksd at
`MSKSD_LLM_PORT`, admitted by that workspace's input chain from
that tap only, and authenticated by a per-workspace credential the
first-boot seed plants (`/etc/msks/llm.token` plus the
`MSKSWS_BASE_URL`/`MSKSWS_API_KEY` exports). Provider credentials
live host-side — `MSKSD_LLM_MODELS` entries with `file:`/`cmd:`
secret indirection, a single `*` entry for single-upstream
passthrough, any other list through the litellm router. See
`docs/llm.md`.

### Developing msks inside a workspace (#77)

The dev-workspace bootstrap seed turns a pristine Debian workspace
into an msks development environment over the workspace's own
egress NIC. One create with the seed and egress is the whole of
the setup — the image catalog serves the pristine Debian base
unchanged:

```bash
msks create dev --egress --user-data scripts/dev-workspace.sh \
  --mem-mib 8192 --root-mib 20480   # the in-guest suite's budget
msks start dev   # first boot provisions; later boots resume
```

Cloud-init runs the seed once per overlay lifetime: it installs
uv (which fetches its own Python 3.14), clones the repo, and runs
`uv sync` — all into the persistent root overlay, so stop/start
cycles keep it and a factory reset re-provisions from the same
seed. Every step checks before doing, so re-running the script is
a no-op. Progress is guest-observable in
`/root/.msks-bootstrap/state` (the running step name, then `done`),
so `msks console` into a booting workspace shows where setup stands.
The suite runs inside the guest the way the `unit-tests` task runs
it — the task's exec line, from the venv uv built:

```bash
msks console dev
uv run python -m pytest src/msks/tests -v -n auto
```

devenv and nix remain an optional developer comfort inside the
guest, off the seed's critical path: building them there exercises
upstream toolchains for tens of minutes and gigabytes and tests
nothing msks owns.

The seed's downloads (PyPI, uv's Python builds, the git remote)
all ride the egress NIC the daemon serves. The end-to-end proof is
the opt-in root smoke `test_local_dev_workspace_bootstrap`
(`TEST_EGRESS=1`). A baked dev image — the same substrate the
guest build uses — remains an optional cold-start accelerator on
top of the seed, not a prerequisite.

The workspace's whole `/home` also moves through the daemon (#80):
`msks home export dev` streams the volume to the client's machine
(backup, or migration to another daemon), and `msks home import`
restores or seeds one from an exported image —

```bash
msks stop dev
msks home export dev - | gzip > dev-home.ext4.gz   # whole-home backup
```

— while day-to-day code in and out rides the workspace's own egress
(git remotes, substitutes) and the forward seam (`msks ssh`, and
`msks rsync dev -- -av ./src/ :src/` for file copies, with no
setup beyond the client environment).
Outbound, the push carries its own credentials: logging in through
the forward with `-A` delivers the operator's ssh agent into the
workspace, so a `git push` from inside authenticates to any remote
over the egress NIC with nothing stored in the image or the seed
(the loop and its proof, `test_local_egress_git_out`, are in
`docs/networking.md`).
See `docs/storage.md` for the byte-stream endpoints and their
contract.

### The workspace console (`msks console`) (#21)

An interactive shell in a running workspace, from any host that can
reach the daemon:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks console my-workspace
```

A devenv shell presets `MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE`
from the worktree's dev-daemon state; an outside shell exports the
same trio by hand (the URL from the daemon's log line, the token
and CA from `.devenv/state/msksd/`).

The client speaks the daemon's console websocket
(`/api/v1/workspaces/{id}/console`): TLS plus bearer token — the same
authentication as the REST surface, with the token on the query
string (like `/api/v1/events`). Ctrl-] detaches (Ctrl-C and Ctrl-D
reach the guest); pressing Ctrl-] twice quickly sends one literal
Ctrl-] to the guest instead (see `docs/cli.md`). The session ends
cleanly when either side closes.
Detaching leaves the workspace running; the shell process inside the
guest exits when the stream closes.

Transport (#21), in the preferred vsock-first shape:

- The workspace VM boots with a virtio-vsock device whose host side
  is a unix socket cloud-hypervisor **listens** on
  (`<state>/vms/<id>/vsock.sock`). The daemon's proxy connects, sends
  `CONNECT 1023\n`, reads the `OK <port>\n` reply, then pumps raw
  bytes both ways — no framing, backpressure is websocket/TCP flow
  control.
- The guest loads `vmw_vsock_virtio_transport` (systemd-modules-load)
  and runs `msks-console.service`: the msks console helper
  (`/usr/bin/msks-console-helper`, a static binary the image builds
  from `src/console-helper`), restarted by systemd if it dies. The
  helper owns the vsock listener, accepts host-originated connections
  only, and each connection negotiates the identity prelude (#63):
  the daemon sends the requested user, the client terminal's size
  and TERM, and the helper answers `MSKS OK <user>` (or a named
  refusal) before exec'ing that user's login shell on a fresh pty.
  The pty is a plain canonical terminal — ISIG, ONLCR, ECHO, and
  ICANON all on: Ctrl-C generates SIGINT in the guest, output
  arrives CRLF-terminated, and the line discipline echoes and edits
  input for programs that read stdin directly. The shell's TERM
  comes from the client's terminal, so readline engages and provides
  line editing and history while it is active (#61); a TERM=dumb
  client gets readline off. The session's user is **root** by
  default; `msks console --user <name>` requests the image's workspace
  user (users the image does not serve are refused by name).
- The guest pty is created at the client terminal's size (#61's
  0x0 fixed): the console request carries the geometry at connect,
  and the pty keeps that size for the session's life. The console
  stream is a raw byte pipe by design (#108's terminal-layer
  decision), so a window resized mid-session does not reach it:
  reconnect for a new size, or use an ssh session through the
  forward, where ssh's window-change channel resizes the pty live
  (#108–#112). If console resize ever returns to the queue, it
  starts with a control-channel decision — a dedicated vsock
  control port, or another design that keeps the stream raw (#78).
- `MSKSC_CAFILE` pins the daemon certificate for verification when
  you have it (the dev daemon writes `msks-ca.pem` into its state
  dir on first serve). Without it the client proceeds with
  certificate verification off and says so on stderr — the CA
  fingerprint the daemon logs at startup is the cross-check.
