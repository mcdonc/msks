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

### VM guest assets

The workspace guest is **Debian 13 (trixie)**, straight from Debian's
official nocloud cloud image (#30): systemd as PID 1, apt, and
Debian's own kernel, initrd, and modules — booted directly (no
BIOS/UEFI) by cloud-hypervisor. The image is pinned by its dated
cloud.debian.org URL and sha512, and the build turns it into the
msks boot contract — `vmlinux` (Debian's bzImage, `CONFIG_PVH=y`),
`initrd`, a fresh read-only ext4 rootfs, and
`guest-manifest.json` — with a small overlay of msks systemd units
(vsock console, serial autologin). Extraction is fully unprivileged:
qemu-img convert, partition slice, `debugfs rdump`, `mke2fs -d`.
Measured boot on bare-metal KVM: kernel at 1.1s, the vsock console
service at 7.4s, login prompt at 8.9s (#37 tracks the <5s goal).
`apt` is present but inert while the root is read-only and the VM
has no network — installs arrive with #26's writable volume. The
extraction runs under fakeroot so the image is root-owned with sane
password-file modes (setuid bits are lost; everything runs as root).
Stopping a workspace is API-side (`vm.shutdown`, non-graceful in
cloud-hypervisor v52 — the guest is not notified; there is no
guest-side power-button handler).

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
  new value *adds* a token; the previous bootstrap credential stays valid
  until revoked via the API.
- **Schema**: the SQLite database is created and upgraded by Alembic at
  startup (inside the package: `msks/migrations`).

### The msksd appliance (any Linux host)

The daemon runs as an appliance microvm — no NixOS required on the
host or in the guest. Requirements: any Linux with KVM + nested
virtualization enabled, nix + devenv, and `sudo -n` for a one-time
bridge/tap (the only privileged host step; everything else —
cloud-hypervisor, ch-remote, virtiofsd — comes from the devenv shell).

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:appliance-build
devenv --quiet -O dotenv.enable:bool false shell -- devenv processes up -d   # or: msks:appliance-up
curl -sk https://192.168.77.2:8660/api/v1/health   # TOFU fingerprint: .appliance/serial.log
devenv --quiet -O dotenv.enable:bool false shell -- devenv processes down     # or: msks:appliance-down
```

**The appliance runs under the devenv process manager (#25)**, not a
daemonizing task: `processes.appliance` (one supervised process that
owns both the VM and its store-share daemon) gets crash-restart,
logs, and clean teardown from the environment's own supervisor.
Background lifecycle semantics — detached `up -d`, graceful
ACPI-first teardown, crash-restart and `gave_up`, and the manual
recovery when the manager daemon dies — are documented in
AGENTS.md ("Process manager").

How it fits together (#10, #25):

- The image is pure nixpkgs derivations built like the workspace
  guest needs — the Debian image ships its own kernel, initramfs,
  and virtio modules. (#30 has the full story; the busybox-rootfs
  build it replaced carried them by hand.)
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

### The workspace shell (`msks shell`) (#21)

From any host that can reach the appliance, an interactive shell in
a running workspace:

```bash
export MSKSC_URL=https://192.168.77.2:8660
export MSKSC_TOKEN=$(cat .appliance/bootstrap-token)
devenv --quiet -O dotenv.enable:bool false shell -- msks shell my-workspace
```

The client speaks the daemon's console websocket
(`/api/v1/workspaces/{id}/console`): TLS plus bearer token — the same
authentication as the REST surface, with the token on the query
string (like `/api/v1/events`). Ctrl-] detaches (Ctrl-C and Ctrl-D
reach the guest); the session ends cleanly when either side closes.
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
  and runs `msks-console.service`: Debian's own socat (built
  WITH_VSOCK) as `VSOCK-LISTEN:1023,reuseaddr,fork
  EXEC:/bin/bash,pty,ctty,echo=0,icanon=0,stderr,setsid`, restarted
  by systemd if it dies. One Debian bash on a pty per connection.
  The pty keeps ISIG and ONLCR (no `raw`): Ctrl-C generates SIGINT in
  the guest and output arrives CRLF-terminated, while
  `echo=0,icanon=0` leave echo and line editing to the shell. The
  shell is **root**; a non-root shell is follow-up work.
- Window-size changes are not applied v1: the guest pty keeps its
  creation size; propagating a resize needs a guest-side helper that
  does not exist yet.
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
