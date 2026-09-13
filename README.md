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

Everything needed to boot a microvm is built by nix from the nixpkgs
revision devenv itself pins — kernel (bzImage with the PVH entry
point), an initrd carrying the virtio/ext4 modules the stock kernel
builds as modules, a read-only ext4 rootfs around a static busybox,
and the k8s vm-runner container archive. Every step of the build runs
inside the repo on any Linux host with nix:

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

Background lifecycle semantics (all verified live):

- `devenv processes up -d` starts the manager detached — it survives
  the shell that launched it, and a second `up -d` is a no-op.
- `devenv processes down` (from any fresh shell) stops gracefully:
  the supervisor TERMs the whole process session at once — the run
  script's trap drives ACPI poweroff through the CH API (up to 10s,
  within the configured 15s kill grace) while the VMM's own SIGTERM
  handling shuts it down in parallel — both processes gone, sockets
  and virtiofsd's pidfile cleaned. A second `down` is a clean no-op.
- `devenv processes list` / `status` / `logs appliance` inspect the
  supervised state; `restart` reboots it on demand.
- A killed VMM (`kill -9`) crash-restarts under the supervisor; a
  repeatedly-failing process reaches `gave_up` after five restarts
  (`devenv processes logs` shows why).
- `DEVENV_TUI=false devenv processes up` is the headless foreground
  form — the shape a systemd unit would run.
- If the manager daemon itself dies while processes run, they keep
  running unsupervised; `devenv processes down` then reports "No
  process manager is running". Recovery is manual:
  `pkill -f 'cloud-hypervisor --api-socket <repo>/.appliance/api.sock'`
  (plus the matching `virtiofsd --socket-path` pattern) and removing
  the stale sockets under `.appliance/`.

How it fits together (#10, #25):

- The image is pure nixpkgs derivations built like the workspace
  guest — kernel, initrd, busybox rootfs with kvm/virtiofs/virtio_net
  modules — no NixOS, no module system.
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
