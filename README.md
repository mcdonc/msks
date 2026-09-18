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
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:xenon
```

The Python-side pre-commit gates' full offender list in one pass —
all ruff and deferred-import findings, every xenon offender, and the
jscpd report at once, plus, when sources under `src/msks/` changed,
every missing coverage line and branch arc for the changed files
after one gated suite run:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:preflight
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
upstream through the workspace's egress NIC — the appliance serves
egress (#52), a host-run dev daemon does not. The
extraction runs under fakeroot so the image is root-owned with sane
password-file modes (setuid bits are lost; everything runs as root).
Stopping a workspace presses the ACPI power button
(`vm.power-button`): the guest's systemd-logind runs the clean
poweroff that flushes its persistent disks (#14).

Every step of the build runs inside the repo on any Linux host with
nix (the k8s vm-runner container archive comes from the same tree):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:build-guest
```

The artifacts (plus a `guest-manifest.json` describing them and the
boot cmdline) land in `.guest/`. Boot one interactive VM from them —

```bash
devenv tasks run msks:demo-vm
```

— which attaches the guest's serial console to your terminal
(`poweroff -f` inside the guest or Ctrl-C stops it) and leaves
`ch-remote` reachable on the printed API socket path.

Boot tests self-provision: when `.guest/` holds built artifacts and
`/dev/kvm` is usable, the smoke tests find them without any exported
variables (`MSKSD_TEST_VMLINUX` / `MSKSD_TEST_INITRD` /
`MSKSD_TEST_ROOTFS` / `MSKSD_TEST_CMDLINE` / `MSKSD_TEST_RUNNER_IMAGE`
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

### The bare-host dev daemon (the default `processes up`)

For daemon-side development, msksd runs NATIVELY on the host
(#141) — no appliance VM, no appliance artifact assembly:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv processes up -d
msks ls                                   # client env is preset (127.0.0.1:8660)
msks create h1 --no-egress && msks start h1 && msks console h1
msks rm h1
```

The state lives in `.msksd/` (TLS CA, bootstrap token, sqlite
catalog, workspace volumes); the workspace image archive is built
conditionally (`msks:build-guest-archive`, keyed on its inputs) and
imported into the catalog on the daemon's first boot. The client
environment targets this daemon by default, WITH certificate
verification (`.msksd/msks-ca.pem`). A daemon edit restarts in
seconds: `devenv processes restart msksd`.

Workspaces without egress are fully served — vsock console,
user-data seeds, stop/start persistence. Egress (and `msks ssh`,
whose forwards ride the egress NIC) holds `CAP_NET_ADMIN` (#101):
that is the appliance's job, not a dev shell's. A workspace
created without `--no-egress` refuses to start here — the 503
names `MSKSD_EGRESS_ENABLED`, which is the appliance's setting;
remove the workspace and recreate it with `--no-egress`, or move
to the appliance below.

State notes: `.msksd/` is gitignored but NOT disposable-clean —
`git clean -xfd` deletes the token, CA, catalog, and every
workspace volume with it. Each worktree owns its own `.msksd/`,
and two checkouts cannot both bind 127.0.0.1:8660 — stop one (or
`export MSKSD_PORT` for the second) before starting another.
A crashed or exited daemon leaves running workspaces in place —
the restarted daemon re-finds them (verified live). A deliberate
`devenv processes restart msksd` / `down` kills the whole process
tree, workspaces included, without their graceful stop — stop
them first (`msks stop <id>`) when a clean shutdown matters.

### The msksd appliance (opt-in, any Linux host)

The daemon also runs as an appliance microvm — the deployed shape —
for egress networking, the guest network bridge, or appliance-image
work. Requirements: any Linux with KVM + nested virtualization
enabled, nix + devenv, and a one-time root setup of the host
network:

```bash
sudo bash scripts/appliance-host-setup.sh
```

That installs the bridge, tap, host forwarding (`sysctl.d`), and NAT
rules — with a systemd unit that re-arms them on every host reboot —
and nothing needs sudo afterwards: `devenv processes up` starts the
appliance as your own user (cloud-hypervisor, ch-remote, and
virtiofsd all come from the devenv shell). Re-run the installer to
re-arm after a firewall reload; an existing tap keeps its owner, and
the installer names the one-step fix (`ip link del mskstap0`, then
re-run) when the appliance moves to another user.

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:appliance-up
curl -sk https://192.168.77.2:8660/api/v1/health   # TOFU fingerprint: .appliance/serial.log
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:appliance-down
```

**The appliance boots detached under the opt-in tasks (#141)**:
`msks:appliance-up` builds conditionally (the #140 keys apply), then
runs `scripts/appliance-run.sh` detached with a pidfile;
`msks:appliance-down` TERMs that pid — the run script's ACPI-first
trap owns the teardown (its 60s window covers a workspace's nested
stop cycle; a shorter window lost page-cache-only sqlite commits,
observed live). The default `devenv processes up`/`down` now manage
the bare-host dev daemon, not the appliance.

How it fits together (#10, #25, #92):

- The image is Debian 13 (trixie) — the same genericcloud base the
  workspace guest builds from, with Debian's **generic** kernel
  (the cloud flavor the guest boots lacks virtiofs and the KVM
  modules the appliance needs) and systemd units replacing the
  shell-script init: journald persists to the state disk, logind
  owns the ACPI power button, and msksd runs as a supervised unit
  that restarts in place on a crash. (#30 has the guest's story;
  #92 the appliance's.)
- The heavy runtime (the nix-built msksd closure, the VMM for
  workspace VMs, and the workspace assets from `msks:build-guest`)
  rides a **read-only virtiofs share of the host `/nix/store`** —
  read-only enforced by `virtiofsd --readonly`, not just the guest's
  mount: the appliance runs the same store paths the host built, and
  nothing is copied into the image. Two GC roots keep them realized:
  `msks:appliance-build` roots the appliance's own closure,
  `msks:build-guest` roots the workspace guest assets (which the
  appliance references only through the share).
- Persistent state (SQLite, workspace overlays, logs) is a second
  disk under `.appliance/state.ext4` (relocatable via
  `MSKSD_APPLIANCE_STATE`); rebuilds never clobber it.
- The bootstrap token is generated into `.appliance/bootstrap-token`
  and delivered on the kernel cmdline (`msksd.bootstrap_token=...`):
  the host file is the single source of truth, and rotation means
  editing it and restarting the processes.
- Networking: a private L2 bridge (`msksbr0`/`mskstap0`, static
  192.168.77.0/24 plan) — the API is simply reachable at the guest
  IP, no port forwarding.
- The serial log restarts from empty on every VMM start: a
  crash-restart replaces the previous boot's TOFU fingerprint with
  the new boot's (reprinted within seconds).
- Nested KVM: `kvm_intel`/`kvm_amd` load in the guest and workspace
  VMs run on the appliance's `/dev/kvm` (verified: a workspace boots,
  runs, and stops inside, driven through the API).
- Debug hatch: seed a `debug-shell` marker onto the state disk
  (`debugfs -w -R "write <file> debug-shell" .appliance/state.ext4`)
  and the init backgrounds the daemon, prints a one-way diagnostics
  dump (kvm modules, `/dev/kvm`, VMM binary, store visibility) to the
  serial log, runs `/state/diag.sh` if present, and leaves a shell on
  the console — readable interactively when the serial console is
  attached to a terminal instead of the log file.

Known quirk worth knowing: cloud-hypervisor v52 rejects writes to
sector 0 on disks without an explicit `image_type` (a QCOW2
misdetection guard), which breaks any guest writing an ext4
superblock — every disk the appliance and the daemon create declares
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
  https://192.168.77.2:8660/api/v1/images        # import
curl -sk -H "authorization: Bearer $MSKSC_TOKEN" \
  https://192.168.77.2:8660/api/v1/images        # list (name/version/hash/default)
```

A workspace create selects an image by reference — `"image":
"debian:13.6"` (or a bare `name` for its newest version, or a hash);
with no image and no explicit artifacts the designated **default**
resolves. The first import becomes the default; `MSKSD_DEFAULT_IMAGE`
points the daemon at an archive to import on first boot, and the
appliance sets it to its built-in image through the kernel-cmdline
bridge — a bare `POST /workspaces` works on a fresh appliance with
nothing else built. Explicit `kernel`/`rootfs` fields still win over
the catalog (the shape the tests and dev flows use).

### The client CLI (`msks ls`, `msks create`, `msks start`, `msks stop`, `msks rm`) (#59, #66)

The client also covers the non-interactive half of the operator flow:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- msks ls
devenv --quiet -O dotenv.enable:bool false shell -- msks create my-workspace --start
```

`msks ls` prints one line per workspace (id, status, image hash,
host); `--json` prints one JSON document for scripting. `msks create`
POSTs the same body the API accepts — `--image` picks a catalog
reference, `--cpus`/`--mem-mib`/`--root-mib`/`--home-mib` size the VM,
`--user-data` attaches a first-boot provisioning script (#41, a
cidata seed disk the guest's provisioner runs once), and explicit
`--kernel`/`--rootfs` (with optional `--initrd`,
`--cmdline`) bypass the catalog. `--start` boots the workspace right
after creating it, so `msks create ws --start` then `msks console ws`
is the two-step path from nothing to a shell; `msks start <id>` boots
an existing workspace later, `msks stop <id>` powers one off (a
graceful, deadline-bounded shutdown; the data survives), `msks rm
<id>…` deletes one or more workspaces together with their persistent
root overlay and `/home` volume, `msks console <id>` boots one itself
when the daemon reports it as not running (a notice prints on
stderr while the boot runs), and `msks home export/import <id>`
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
the appliance:

```text
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain
                                                │  guest → uplink: accept
                                                ▼
                                     NAT (masquerade) → appliance uplink
```

The daemon is the guest's only DHCP server and resolver: each
egress workspace gets a dedicated /30 from `MSKSD_EGRESS_SUBNET`, a
DHCP offer naming the tap as gateway and resolver, a small DNS
forwarder on that resolver, and NAT out the appliance uplink
(`MSKSD_EGRESS_UPLINK`). Everything derives deterministically from
the workspace id, so stop/start cycles rebuild the same network;
stop and delete tear the tap, chain, and services down again. The
guest side is just the image's DHCP client (systemd-networkd +
resolved in the overlay); a workspace without egress presents no
NIC, on every backend.

Egress arms while `MSKSD_EGRESS_ENABLED=true` and the daemon holds
`CAP_NET_ADMIN` — the appliance sets both, so workspaces are
networked there once its setup script has wired the host side
(forwarding + NAT for the appliance's bridge). A daemon that cannot
arm the plumbing still serves everything else, and an egress
workspace refuses to boot with the cause named (boot those with
`--no-egress`). On k8s, create with `"egress": false` — the backend
refuses egress creates until the NetworkPolicy parity lands (#69).
Per-flow consent (allow/deny holds on each new connection) is #69.
See `docs/networking.md` for the full reference.

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

The egress requirement is the appliance's: the seed's downloads
(PyPI, uv's Python builds, the git remote) all ride the NIC the
appliance serves. The end-to-end proof is the opt-in root smoke
`test_local_dev_workspace_bootstrap` (`MSKSD_TEST_EGRESS=1`). A
baked dev image — same substrate the guest and appliance build
from — remains an optional cold-start accelerator on top of the
seed, not a prerequisite.

The workspace's whole `/home` also moves through the daemon (#80):
`msks home export dev` streams the volume to the client's machine
(backup, or migration to another daemon), and `msks home import`
restores or seeds one from an exported image —

```bash
msks stop dev
msks home export dev - | gzip > dev-home.ext4.gz   # whole-home backup
```

— while day-to-day code in and out rides the workspace's own egress
(git remotes, substitutes) and the forward seam (rsync, ssh).
Outbound, the push carries its own credentials: logging in through
the forward with `-A` delivers the operator's ssh agent into the
workspace, so a `git push` from inside authenticates to any remote
over the egress NIC with nothing stored in the image or the seed
(the loop and its proof, `test_local_egress_git_out`, are in
`docs/networking.md`).
See `docs/storage.md` for the byte-stream endpoints and their
contract.

### The full recursion: msksd inside a workspace (#82)

The workspace image carries what a workspace needs to run msksd
itself: the nested-KVM modules (`kvm`/`kvm-intel`/`kvm-amd`, loaded
at boot by the image's own `msks-kvm.service` when the host exposes
virt extensions through the appliance) and the inner-egress stack
(`tun` plus the nftables/NAT set) — the same posture the appliance
image ships, so a workspace can be an appliance in miniature. The
L3 recursion seed layers the daemon on top of the dev bootstrap
(#77): same first steps (uv, the checkout, `uv sync`), then
cloud-hypervisor's pinned static binary and the daemon's
workspace-side tools over egress, and msksd as a systemd unit —
state on the persistent `/home` volume, egress armed behind the
workspace's own NIC, and the nested-virt timeouts the recursion
demands (`MSKSD_VSOCK_WAIT_TIMEOUT_S=75` and friends; the unit's
comments record the tuning). The bootstrap token lands in
`/root/.msks-inner/token`:

```bash
msks create l3 --egress --user-data scripts/l3-recursion.sh \
  --mem-mib 4096 --home-mib 30720
msks start l3   # first boot provisions, then msksd serves 8660 inside
```

The boot artifacts for the inner workspace are host-side build
products — the one thing the seed cannot fetch. Push them over the
forward plane (sparse, so the mostly-zero rootfs crosses as its real
blocks) and create the inner workspace over them directly; the
daemon builds the workspace's own overlay and volumes on top:

```bash
msks key l3 --out ~/.cache/msks/l3.key
msks forward l3 22 --local 2201 &
rsync -e 'ssh -i ~/.cache/msks/l3.key -p 2201' -aPS \
    .guest/vmlinux .guest/initrd .guest/rootfs.ext4 \
    root@127.0.0.1:/root/inner-artifacts/
msks console l3   # then, inside the workspace:
#   export MSKSC_URL=http://127.0.0.1:8660
#   export MSKSC_TOKEN=$(cat /root/.msks-inner/token)
#   /root/msks/.venv/bin/msks create inner1 --cpus 1 \
#     --kernel /root/inner-artifacts/vmlinux \
#     --initrd /root/inner-artifacts/initrd \
#     --rootfs /root/inner-artifacts/rootfs.ext4 \
#     --cmdline 'console=ttyS0 root=/dev/vda rootfstype=ext4 rw'
#   /root/msks/.venv/bin/msks start inner1
#   /root/msks/.venv/bin/msks console inner1
```

With `--cpus 1`, that is: the measured boundary on the reference
host is that a 1-vCPU inner guest boots to its login prompt at two
removes, while a 2-vCPU one hangs in early SMP bringup (the vCPU
executes; the kernel never reaches its first serial byte — see #82's
evidence for the full characterization). The console of the inner
workspace then appears inside the console of the workspace:
host → appliance → workspace → inner workspace.
The end-to-end proof is the opt-in smoke `test_appliance_l3_recursion`
(`MSKSD_TEST_L3=1` with the appliance built), which also pins the
two facts the recursion rests on: Debian's generic kernel ships the
KVM modules the image's closure carries, and vmx survives two
removes of cloud-hypervisor's default CPU config — an inner guest
that reaches its console is running on nested-in-nested KVM,
because cloud-hypervisor boots VMs through `/dev/kvm` and has no
software fallback.

The proving smoke also runs in CI (#135):
`nightly-l3.yml` runs `test_appliance_l3_recursion` on a
self-hosted runner labeled `msks-l3` — the recursion needs two
levels of nesting below the runner, one more than GitHub's hosted
runners accelerate (verified empirically: a KVM guest booted on a
hosted runner sees no vmx), so it runs where that depth exists.
The runner's host needs what the manual run needs (`/dev/kvm`, the
one-time `scripts/appliance-host-setup.sh` network install, and
nix for both asset builds). The workflow is manual-dispatch only
until such a runner registers — a schedule with no runner queues
forever and signals nothing; arming the nightly is adding the
trigger back.

### The workspace console (`msks console`) (#21)

From any host that can reach the appliance, an interactive shell in
a running workspace:

```bash
export MSKSC_URL=https://192.168.77.2:8660
export MSKSC_TOKEN=$(cat .appliance/bootstrap-token)
devenv --quiet -O dotenv.enable:bool false shell -- msks console my-workspace
```

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
  you have it (a directly-run msksd's CA, or the appliance CA
  exported from its state disk:
  `debugfs -R "dump /msks-ca.pem msks-ca.pem" .appliance/state.ext4`).
  Without it the client proceeds with certificate verification off
  and says so on stderr — the serial log's TOFU fingerprint is the
  cross-check.
- The appliance bridges every `msksd.<name>=<value>` pair on its
  kernel cmdline into the daemon's environment as
  `MSKSD_<NAME>`; the run script appends pairs from
  `MSKS_APPLIANCE_CMDLINE_EXTRA` (e.g.
  `msksd.vsock_wait_timeout_s=30` on slow nested-virt hosts).

### k8s (k3s) smoke path

The vm-runner container image comes from the same pinned nixpkgs as
the local backend's cloud-hypervisor:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:build-runner-image
sudo k3s ctr images import .guest/msks-vm-runner.docker.tar.gz
```

The k8s smoke tests reference the imported `msks-vm-runner:dev` image
automatically once the archive is built; they skip when
`MSKSD_TEST_KUBECONFIG` does not point at a cluster kubeconfig.
