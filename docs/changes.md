# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **The dev-workspace bootstrap seed (#77).**
  `scripts/dev-workspace.sh`, passed to `msks create --user-data`,
  bootstraps an msks development environment inside a workspace over
  its own egress NIC at first boot: uv (with its own Python 3.14),
  the repo checkout, and `uv sync` — the venv the `unit-tests`
  invocation runs from — all on the persistent root overlay; stop/
  start keeps it, a factory reset re-provisions, re-running is a
  no-op. Serves deployments where egress arms (the appliance);
  proved end to end by the `MSKSD_TEST_EGRESS=1` and appliance
  root smokes.

- **`msks shell --user` and the console identity prelude (#63).** The
  vsock console now serves shells as root or the image's workspace
  user: a guest-side helper negotiates the identity, the client
  terminal's size, and its TERM value in-band on each connection, and
  the listener accepts host-originated connections only (guest-local
  peers are refused). A workspace bound to an image whose record
  cannot be read is refused with close code 4501 — the console never
  silently downgrades to a raw root shell. Images declare
  `console_protocol: prelude-v1` and
  `console_users` in their manifest; older images keep today's raw
  root shell through the daemon's legacy path. The helper is a Rust
  crate under `src/console-helper`, gated at 100% line and branch
  coverage (`.github/workflows/rust.yml`).
- **YAML config file for msksd (#46).** `msksd` reads `msksd.yaml` — `--config <path>` for an explicit file, `--config=none` for env-vars-only, and a bare `msksd` resolves `$MSKSD_CONFIG_DIR/msksd.yaml` (default `~/.config/msksd/msksd.yaml`), generating a commented template on first run. Precedence is `MSKSD_*` env vars > file > defaults; a config key is its variable minus the prefix, lowercased (`MSKSD_PORT` → `port`, `MSKSD_EGRESS_SUBNET` → `egress_subnet` — flat, klangkd's convention, derived by one rule so the spellings cannot drift), keys take native YAML scalars, and unknown keys, duplicate keys, or invalid values fail at startup. SIGHUP re-reads the file into the live settings without a restart. The appliance configures itself through the file — `/run/msksd.yaml`, generated at each boot with the build's store paths, with the kernel-cmdline `msksd.<name>=<value>` bridge remaining the variable-override channel — and a bare `alembic` run resolves the database URL from the default file when present. See `docs/config.md` for the key-by-key reference.
- **`msks:preflight` (#43).** One command delivers the Python-side pre-commit gates' complete feedback before the first commit attempt: every ruff and deferred-import finding, every xenon offender, the jscpd report, and — when sources under `src/msks/` changed — the gated suite run followed by every missing coverage line and branch arc for the changed files (`scripts/covgaps.py`). AGENTS.md makes it part of the loop: fix everything it names in one editing pass, re-run, then commit. The xenon and jscpd gates now also grade untracked-but-present files, so a new file grades from the moment it exists.

- **Doubled-escape and chunked input in `msks shell` (#36).** The client reads stdin in 4,096-byte chunks and sends one websocket frame per chunk, so a large paste lands a few frames instead of one TLS frame per byte. Ctrl-] Ctrl-] — the second press within 50 ms, or both bytes in one chunk — sends one literal Ctrl-] (0x1d) to the guest; a single Ctrl-] still detaches, and bytes typed before the escape are delivered before the session closes. See `docs/cli.md`.
- **Per-workspace `user_data` provisioning (#41).** A workspace created with `user_data` (API field; `msks create --user-data FILE`, `-` for stdin, ≤64 Ki characters) runs the payload on its first boot through cloud-init: the daemon builds a `cidata`-labeled iso9660 seed disk (NoCloud's own format, `MSKSD_MKISOFS`), attaches it read-only, stores it mode 0600, and deletes it with the workspace; both `#!` scripts and cloud-config documents run. The payload is create-time and immutable (`PUT`/`PATCH` answer a named 405). See `docs/images.md`.

- **Pre-commit hook suite (#72).** Eleven hooks join the commit-time suite: the deferred-imports checker (`scripts/check_deferred_imports.py`, ported from klangk — imports live at module scope, with `# allow-deferred-import` as the escape hatch), shfmt, shellcheck, check-executables-have-shebangs, markdownlint, actionlint, trufflehog, nixfmt, check-toml, yamllint, and prettier. `devenv shell` writes a generated `.prettierignore`; the lint rules live in `devenv.nix`; CI's lint workflow runs the whole suite (`pre-commit run --all-files`). trufflehog scans staged file contents and fails only on credentials that verify live (the stock git-history variant scans no commits at commit time). The tree was formatted once to the new gates (docs, workflows, Nix, shell scripts).
- **`msks image` catalog commands (#65).** The CLI now manages the image catalog without curl: `msks image ls` prints one line per image (reference, short hash, default designation, kernel facts; `--json` for the raw listing), `msks image import <path>` registers a daemon-side archive and prints the imported reference (the help states the daemon reads the path — nothing is uploaded), `msks image rm <ref>` removes by any catalog reference form (`name:version`, bare name, `name@hash`, full hash, or a unique hash prefix) and surfaces the 409 naming a workspace that boots the image, and `msks image info <ref>` prints one image's full record. See `docs/cli.md`.

- **`msks:jscpd` token-clone gate (#71).** The jscpd scanner (5.0.16, a pinned per-platform prebuilt binary fetched from npm — linux x64/arm64 gnu and macOS arm64/x64; unsupported platforms fail at eval time — ported from klangk #2904) joins the dev environment as both a report (`devenv tasks run msks:jscpd`) and a pre-commit gate: `scripts/jscpd-gate.sh` is the single definition of the invocation (tracked sources in `src/msks/msks`, `--min-tokens 70`), shared with the `msks:xenon` pattern. A commit fails when any exact clone of ≥ 70 tokens exists among those sources; the backend baselines clean (0 clones), so the gate starts from zero.

- **Workspace egress networking, on by default (#52).** Workspaces are networked from creation: each boots with a virtio-net NIC onto a per-VM tap inside the appliance, takes its address, gateway, and the daemon's resolver over DHCP from a per-workspace /30 of `MSKSD_EGRESS_SUBNET` (the slice recorded on the row, so the address is stable across restarts), resolves through the daemon's UDP forwarder, and NATs out the appliance uplink through a per-VM nftables table (forward: the guest's source out the uplink and established replies back, all else drops; input: DHCP and the resolver only, so guests cannot reach the appliance's own services) — create with `"egress": false` (`msks create ws --no-egress`) to boot NIC-less. The plumbing arms while `MSKSD_EGRESS_ENABLED=true` and the daemon holds `CAP_NET_ADMIN`; `scripts/appliance-setup.sh` wires the host side (forwarding + NAT for the appliance bridge) as its documented privileged step. The guest overlay ships the DHCP client (systemd-networkd unit + resolved stub); on k8s, create refuses egress until the NetworkPolicy parity (#69). Consent gating also arrives with #69. See `docs/networking.md`.

- **Client CLI: `msks stop`, `msks rm`, and the workspace listing named `msks ls` (#66).** `msks stop <id>` powers a running workspace off — a graceful, deadline-bounded guest shutdown — and prints `<id> stopped`; `msks rm <id>…` deletes one or more workspaces (stopped or running) together with their persistent root overlay and `/home` volume, printing one `<id> deleted` line per id. The workspace listing command is `msks ls` (the `#59` behavior under its final name, matching `msks image ls`), completing the lifecycle set: ls / create / start / stop / rm / shell. Missing workspaces (404), workspaces recorded on another host (409), and missed shutdown deadlines (503) print one readable line and exit non-zero. See `docs/cli.md`.

- **Client CLI: `msks ls`, `msks create`, and `msks start` (#59).** `msks ls` prints one line per workspace (id, status, image hash, host; `--json` for a machine-readable document), and `msks create` POSTs the API's create body — `--image` for a catalog reference, `--cpus`/`--mem-mib`/`--root-mib`/`--home-mib` for sizing, explicit `--kernel`/`--rootfs` to bypass the catalog — printing the new workspace's id; `--start` also boots it, and `msks start <id>` boots an existing workspace later. `msks shell` now boots a not-running workspace itself before attaching (notices on stderr). All commands use the `MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` conventions from `msks shell`; connection failures, timeouts, and API or validation errors print one readable line, not a traceback. See `docs/cli.md` for the command and environment reference.

- **Per-workspace persistent state (#14).** Every workspace owns two persistent artifacts created with it: a root overlay (qcow2, copy-on-write over its image — package installs and root edits persist across `stop`/`start`) and a `/home` volume (ext4, labeled `msks-home`, mounted at `/home`). Both survive stop/start and are removed with `delete`; the new `POST /api/v1/workspaces/{id}/reset` drops only the overlay; sizes come from the create request (`root_mib`/`home_mib`) with `MSKSD_ROOT_MIB`/`MSKSD_HOME_MIB` defaults, and the workspace row records the owning host, so a start/stop/reset/delete from another host answers a named 409. The k8s backend maps both artifacts onto a per-workspace RWO PVC whose size derives from the requested artifacts (`MSKSD_K8S_STORAGE_CLASS`/`MSKSD_K8S_WORKSPACE_STORAGE_GIB` override), and the shipped image boots its root read-write through the overlay. See `docs/storage.md` for the layout, lifecycle, and env-var reference.

- **Workspace boot performance (#37).** Start → interactive shell is now ~3.1s p50 on the reference host (was 6.7s; goal < 5s), measured by the new `scripts/perf-boot.py` harness, which also reports the VMM's peak resident set (165–185 MiB for a 1024 MiB guest). The shipped image boots Debian's cloud kernel (ext4/virtio-pci built in) with a msks-built minimal initramfs (one module, ~0.05s) and starts the vsock console before the boot completes; AppArmor, networkd, timesyncd, resolved, unattended-upgrades, and e2scrub units are off the boot. `docs/boot-speed.md` documents the breakdown and the measurement.
- **Workspace image guide (#38).** `docs/images.md` documents the image contract (container-image tar, containerDisk layout, `disk/image.json` schema 2), building an image (the shipped builder and a from-scratch outline), registering via `POST /api/v1/images` (default designation, storage cost, removal rules), and referencing images from workspace create (`name:version`, bare name, `name@hash`, bare hash).

- **Workspace image catalog with containerDisk images (#40).** The canonical image is now a container-image tar (`podman load` compatible) in the containerDisk convention (`workspace-<name>-<version>.tar`: `boot/vmlinuz`, `boot/initrd.img`, `disk/rootfs.ext4`, `disk/image.json` schema 2) — importable with stock tools and consumable by the future k8s backend. `GET/POST /api/v1/images` list and import (per-hash boot-file cache; workspace launches never unpack); workspace create accepts `"image": "name:version"` (or bare name, or hash) and falls back to the designated default, so a bare create works on a fresh appliance — `MSKSD_DEFAULT_IMAGE` (set by the appliance's cmdline bridge) imports and designates at first boot. Explicit `kernel`/`rootfs` fields still win.

- **Workspace shell (#21).** `msks shell <workspace-id>` gives an interactive shell inside a running workspace microvm, from any host that can reach the daemon: the client speaks the authenticated `/api/v1/workspaces/{id}/console` websocket (TLS + token, Ctrl-] detach, raw tty mode), and the daemon proxies it over virtio-vsock — the VM's vsock unix socket after a `CONNECT <port>` handshake — into a per-connection busybox ash on a pty served by static socat in the guest (root shell today, per #5's guest userland; `MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` configure the client). The guest assets gained the vsock module, `/dev/vsock` creation, devpts/ptmx setup, and socat; the daemon retries the console handshake across the guest's post-boot bring-up window.

### Changed

- **The appliance boots with 6 GiB of memory (#77).** Nested
  workspace VMs ride the appliance's own RAM, and inside 2 GiB a
  1 GiB guest beside the daemon OOM-killed the VMM mid-run.
  `MSKS_APPLIANCE_MEM_MIB` overrides for smaller hosts.

- **The appliance starts without sudo (#101).** The host-side network
  (bridge, tap, host forwarding, NAT) moves from per-start `sudo -n`
  calls in `scripts/appliance-setup.sh` to a one-time root install:
  `sudo bash scripts/appliance-host-setup.sh` writes a `sysctl.d`
  forwarding drop-in and a systemd unit that re-arms the bridge, tap,
  and firewall rules at every host reboot. `appliance-setup.sh` now
  verifies the install and names it when something is missing, so
  `devenv processes up` runs entirely unprivileged after the one-time
  install; re-run the installer to re-arm after a firewall reload or
  to change the tap's owning user. `docs/networking.md` documents the
  fully static forms — networkd + nftables files, and a
  copy-pasteable NixOS configuration equivalent to the installer.

- **The appliance runs msksd as a non-root service user (#101).**
  The daemon executes as a dedicated `msksd` user holding exactly two
  ambient capabilities — `CAP_NET_ADMIN` (taps, nftables, and the
  VMM's tap opens) and `CAP_NET_BIND_SERVICE` (DHCP 67, DNS 53) —
  and nothing in its process tree runs as uid 0; `/dev/kvm` reaches
  it through the `kvm` group. `net.ipv4.ip_forward=1` moves from a
  daemon-time write to a boot-time `sysctl.d` setting: msksd verifies
  it and refuses egress with the cause naming `net.ipv4.ip_forward`
  when it reads `0`. The appliance pins its NIC to `eth0`
  (`net.ifnames=0` on its kernel cmdline) so the default
  `MSKSD_EGRESS_UPLINK` matches — full udev in the trixie base would
  otherwise rename the NIC and silently break forwarded egress — and
  the daemon's state moves to the service-user-owned `/state/msksd`
  (an existing state disk migrates its daemon files on first boot).
  See `docs/networking.md`.

- **The workspace guest boots Debian's generic kernel (#96).** One
  kernel pin now serves both the guest and the appliance (#92): a
  host fetches a single kernel deb instead of two, and the workspace
  image archive shrinks ~19 MiB compressed (147.8 → 128.7 MiB xz)
  as Debian's full cloud module tree leaves in favor of the
  twelve-file runtime closure the build derives from `modprobe`
  metadata (and pins by comparing the full tree's closure against
  the shipped tree's at build time). Boot
  speed holds (p50 start→prompt 3.24 s against the cloud flavor's
  2.95–3.26 s host spread; goal < 5 s), and guest memory at first
  prompt is unchanged (~140 MiB); numbers recorded in
  `docs/boot-speed.md`.

- **The appliance runs Debian 13 trixie with systemd (#92).** The
  hand-rolled busybox-init image is replaced by the same genericcloud
  base the workspace guest builds from, booted with Debian's generic
  kernel and systemd units instead of a shell-script init: msksd is a
  supervised service that restarts in place on a crash, journald
  persists to the state disk (readable from the host after teardown),
  and logind answers the ACPI power button. The read-only `/nix/store`
  virtiofs share, the state-disk layout (an existing disk upgrades in
  place), the `msksd.<name>=<value>` kernel-cmdline bridge, and the
  boot-generated `/run/msksd.yaml` are unchanged; warm boot-to-API
  measures ~27.5s p50 against the old image's ~25.2s
  (`scripts/perf-appliance.py`, see `docs/boot-speed.md`). An
  appliance built from current `main` cannot boot at all — #46's init
  change broke the image's `/init` shebang — so rebuild with
  `msks:appliance-build` when updating.

- **The workspace image ships cloud-init (#41).** The image is built from Debian's `genericcloud` cloud image instead of the cloud-init-free `nocloud` variant: cloud-init and its python3 runtime arrive with the base (~130M larger; the appliance state disk grows to 8G to keep fitting two images), and two dropins pin NoCloud as the only datasource and keep cloud-init off the guest's networking. The interactive boot budget is unchanged (vsock shell ~3.0s p50); existing workspaces keep the images they were created with — rebuild the appliance and recreate workspaces to move them onto the new image.

- **Workspace stop is now a clean poweroff (#14).** The local backend's stop presses the ACPI power button (`vm.power-button`) and the guest's systemd-logind runs a full shutdown before the VMM exits. The endpoint stop used before was cloud-hypervisor v52's hard stop: the guest was never notified, and with persistent disks every stop dropped the writes still sitting in the guest's page cache.
- **The workspace guest is Debian 13 trixie (#30).** The rootfs comes from Debian's official nocloud cloud image (pinned by dated URL + sha512): systemd as PID 1, apt (present but inert while the root is read-only), and Debian's own kernel direct-booted. `msks:build-guest`, the manifest contract, and the smoke path are unchanged; the busybox guest is gone. See the README for build details, timings, and the setuid/ownership notes.

- **The appliance runs under the devenv process manager (#25).** `processes.appliance` — one supervised process owning both the VM and its store-share daemon — replaces the daemonizing `msks:appliance-up` task: crash-restart, `devenv processes logs`, and clean graceful teardown (ACPI-first TERM trap) come from the supervisor. `devenv processes up -d` / `down` are the supported lifecycle (the `msks:appliance-up`/`-down` tasks remain as thin wrappers), `scripts/appliance-down.sh` is gone, and the smoke test drives and asserts the supervised lifecycle. Background semantics verified live: detached `up -d` survives shells, double-up and double-down are no-ops, a `kill -9` VMM restarts under the supervisor, and manager-daemon death (processes keep running unsupervised) has a documented manual recovery.

- **The msksd appliance (`msks:appliance-build`/`msks:appliance-up`/`msks:appliance-down`, #10).** The daemon now ships as a bootable appliance: a pure-nixpkgs direct-kernel-boot image (kernel, busybox init, module tree with nested-KVM + virtiofs support) whose heavy runtime — the nix-built msksd closure and the workspace VMM — resolves through a read-only virtiofs share of the host's `/nix/store`, so workspace assets built by `msks:build-guest` flow in with zero copying. The host supervisor is the devenv itself (bridge/tap via one documented `sudo`, virtiofsd and cloud-hypervisor from the pinned shell) — any Linux distro with nix + KVM, no NixOS anywhere. Verified end-to-end: appliance boots, serves the API over HTTPS (TOFU fingerprint on its serial log), and runs a workspace microvm under nested KVM driven through the API.
- **Appliance hardening fixes found by the e2e and the fresh-eyes review (#10).** The local driver now honors the absent-VM contract on stop/kill (a stale api socket behind a dead VMM reported ECONNREFUSED and delete-after-stop 500'd), the rootfs disk is declared `readonly` + `image_type: Raw` in `vm.create` (v52's autodetection otherwise disables sector-0 writes, and O_RDWR on the read-only store share fails), and `tls._write` loops `os.write` (a short write through virtio-backed storage left a truncated CA key).

- **The `msksd` daemon and its `/api/v1` API (#8).** msksd now serves versioned endpoints over one HTTPS+WSS listener — public health, hashed bearer-token auth with revocation (`MSKSD_BOOTSTRAP_TOKEN` seeds the first credential), workspace create/list/status/start/stop/delete driving cloud-hypervisor through the microvm seam, and a websocket event channel for lifecycle transitions. TLS is operator-provided (`MSKSD_TLS_CERT`/`KEY`) or a self-signed CA generated on first run whose fingerprint is logged for trust-on-first-use pinning; `--no-tls` serves plain HTTP for development. State lives in an SQLite database under the state dir (`MSKSD_STATE_DIR`), managed by Alembic migrations.
- **Nix-built guest assets (`msks:build-guest`, `msks:demo-vm`, `msks:build-runner-image`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into `.guest/`, plus the k8s vm-runner container archive — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `MSKSD_TEST_VMLINUX`/`MSKSD_TEST_ROOTFS`/`MSKSD_TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks:demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).

### Fixed

- **`/home` could fail to mount on slow boots (#14).** The guest fstab mounted the home volume by label under `x-systemd.device-timeout=2s`; that clock starts at sysinit job enqueue, before udevd runs, and on a first boot from a fresh overlay (every root read a copy-on-write miss) the udev label probe can exceed what is left of the budget — `home.mount` then fails for the whole boot (`nofail` keeps the boot moving and never retries). The device timeout is now 30s, so the mount rides out a slow coldplug; a boot with no volume at all waits the same 30s once and continues.

- **`scripts/appliance-setup.sh` on iptables-nft hosts (#36).** The NAT rule was invoked as `iptables -C -t nat …`, and iptables-nft 1.8.13 rejects a table option after the command, so setup died before seeding the state disk. The rule helper now takes the table explicitly and places it before `-C`/`-A`, which legacy and nf_tables variants both accept.

- **Egress workspaces failed to start on the appliance: missing `nft_ct` module (#36).** The egress ruleset's `ct state` expression needs the `nft_ct` kernel module; the appliance image's module list loaded `nft_masq` but never `nft_ct`, and the appliance has no udev autoload, so a workspace with a NIC failed at start with "Could not process rule: No such file or directory". The module now loads with the rest of the nftables set.

- **Egress workspaces never took a DHCP lease on fast hosts (#36).** The guest reaches multi-user immediately after sysinit, and a networkd that enumerates its NIC while udev is still renaming it (eth0 to ens3) never manages the renamed link — DHCP never runs. The guest image orders networkd after `systemd-udev-trigger.service` and `systemd-udevd.service`, so the interface name is final before the first enumeration.

- **`ws_url` quotes the token and workspace id (#36).** The console URL interpolates `MSKSC_TOKEN` raw into the query string; minted tokens are urlsafe today, so this was theoretical, but any future charset with `+`, `&`, `=`, or `%` would have mangled it. The token now passes through `urllib.parse.quote_plus` (a query value), and the workspace id through `urllib.parse.quote` with no safe characters (a path segment, where `+` would reach the server literally).

- **Workspace cleanup and image import release their resources (#56).** `cleanup` now stops the workspace's VMM before deleting its directory — tracked or pidfile, like `kill` — where it previously orphaned the process. Image import closes the container-image archive after unpacking; each import previously left the archive's file handle open until garbage collection.
- **Workspace shell input now echoes (#61).** The vsock console's
  guest pty ran with `echo=0,icanon=0` while the client keeps the
  local tty raw, and the service environment's `TERM=dumb` left
  bash's readline off — so typed characters reached nothing that
  would show them. The pty is now a plain canonical terminal
  (`echo=1,icanon=1`) and the unit sets `TERM=xterm`: line editing
  and history work, and programs reading stdin directly get normal
  tty echo. The console ships in the workspace image; a workspace
  keeps the image it was created with, so rebuild the appliance,
  then delete existing workspaces and the old image from the
  catalog — new workspaces then bind the new image.
- **`devenv shell` no longer runs the pre-commit suite at shell entry (#32).** A devenv 2.3.x scheduler regression pulled `devenv:git-hooks:run` into the shell's task graph, so a failing hook (e.g. the xenon complexity gate) aborted shell entry before it opened — with no way to use the shell to fix the failure. The suite still runs on `git commit` and as the `msks:xenon` task.
