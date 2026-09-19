# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **`devenv processes up` converges to a working appliance, loudly
  (#160).** The daemon reports the image it booted from in
  `/health` (an `msksd.image` kernel-cmdline pair), a devenv shell
  presets `MSKSC_EXPECTED_IMAGE` to what the checkout builds, and
  `msks ls` names the drift with the fix (`devenv processes down`,
  then `devenv processes up -d`) when the two differ. The run
  script gates its boot on the guest serving `/health`
  (`MSKS_APPLIANCE_SERVE_TIMEOUT_S`, default 300) and exits with a
  named cause — a never-serving guest or an exited VMM — instead
  of idling as a "ready" appliance; `MSKS_APPLIANCE_AUTO_RESTART=1`
  makes a drifted appliance rebuild and gracefully restart itself
  when no workspace is live (see the README's appliance section).

- **The appliance as the one managed process, client env preset to
  it (#146, #141).** `devenv processes up` boots the appliance —
  the deployed shape: egress workspaces, `msks ssh` forwards, the
  guest network bridge, and the dev tree below — under the process
  manager (crash-restart, logs, 90s graceful grace covering the
  ACPI teardown; `msks:appliance-up`/`-down` are the detached
  wrappers). The client env presets to it: `MSKSC_URL`, the
  appliance's bootstrap token, and certificate verification via
  `.devenv/state/appliance/msks-ca.pem`, which the run script
  extracts from the state disk once the guest serves (until then the
  TOFU fingerprint on the serial log covers the first connect). No
  bare-host msksd process exists; running the daemon by hand on the
  host stays supported and documented (own
  `.devenv/state/msksd/` state, `msks:dev-ready`, no egress).
- **Appliance dev tree: daemon edits without appliance rebuilds
  (#144).** `MSKS_DEV_TREE=1` with `processes up` shares the
  checkout read-only into the appliance and runs the guest daemon
  from it with `msksd --reload` — a daemon edit restarts the guest
  daemon within seconds, no rebuild and no VM reboot; the state
  disk, egress, and TLS keep serving. The default boot (store-path
  daemon) is unchanged. `msksd --reload` is a general development
  flag: any daemon invocation restarts itself when the msks package
  tree it runs from changes.
- **L3 recursion smoke in CI (#135).**
  `.github/workflows/nightly-l3.yml` runs
  `test_appliance_l3_recursion` on a self-hosted runner labeled
  `msks-l3` — the recursion needs two nesting levels below the
  runner, one beyond what GitHub's hosted runners accelerate
  (verified empirically on the live fleet). Per-PR CI stays
  unchanged. The workflow is manual dispatch until such a runner
  registers; arming a nightly schedule is adding the trigger back.
  See README's recursion section.
- **Console challenge-response (#123, first half).** The guest
  console demands a signature the daemon cannot produce: workspaces
  seeded with an identity now also plant an allowed_signers trust
  store, and each console connection carries a fresh nonce the
  client must sign (SSHSIG, `msks-console` namespace) before any
  shell — verified by the guest's own ssh-keygen, refused closed
  otherwise. `msks console` answers transparently with the same key
  as ssh (escrow, client data root, or the operator's ssh-agent,
  hardware keys included — msks never reads an agent-held half).
  Pre-change guests serve no challenge. Enrollment, minted-key
  rotation, and the audited re-enable follow in the second half.
- **Operator-supplied ssh key at create (#132).** `msks create
--pubkey FILE` (or `-` on stdin) builds the workspace around a
  public key the operator already owns, at any well-formed key
  type: the daemon accepts the supplied line as-is (shape-checked,
  re-annotated, seeded like any identity) and holds no private
  half. Minted keys keep the FIPS-approvable type set; login uses
  the operator's own key, and `msks ssh`'s recovery text names it.
  See `docs/networking.md`.
- **Client-minted workspace identity as the create default (#121).**
  `msks create` mints the workspace's ssh keypair on the client and
  sends the public half only: the daemon stores and seeds that half
  exactly like its own minted one and holds no private half — `msks
key` answers the public line, and `msks ssh` serves the private
  half from the client data root
  (`~/.local/share/msks/<id>/identity`, mode 0600). `--daemon-mint`
  keeps the daemon-minted escrow mode (#111; the k8s backend serves
  no identity and needs the flag); `--key-type` selects the type
  (`ed25519` default, #138). See `docs/networking.md`.
- **msksd inside a workspace — the L3 recursion (#82).** The
  workspace image's module closure now carries the nested-KVM trio
  and the inner-egress stack (`kvm`/`kvm-intel`/`kvm-amd`, `tun`,
  the nftables/NAT set — twenty-nine files total), and the image
  loads KVM at boot through its own `msks-kvm.service` when the
  host exposes virt extensions, so a workspace can run msksd
  itself. The `scripts/l3-recursion.sh` seed layers the inner
  daemon on the dev bootstrap (#77) and runs it as a unit with the
  nested-virt timeouts recorded; `test_appliance_l3_recursion`
  (`MSKSD_TEST_L3=1`) is the end-to-end proof — an inner workspace
  booted by msksd inside a workspace, console reachable through the
  inner daemon. See the README's recursion section.
- **git-out through egress with a forwarded agent (#81).** The
  dogfood loop's outbound half is proven end to end by a new opt-in
  root smoke, `test_local_egress_git_out` (it runs in the KVM
  workflow's egress step): the workspace installs git from Debian's
  mirrors and fetches an unrelated HTTPS host through the NAT'd
  uplink, then pushes a commit to a scratch git server on the host
  through a test-widened input pin, authenticating only with an
  operator key that rode the forward as a forwarded agent — no
  credential in the image or the seed. The workflow and its
  ControlMaster caveat are in `docs/networking.md`.
- **`msks ssh` (#112).** One command logs into a workspace with stock
  ssh over the forward websocket: it boots the workspace if needed,
  fetches the minted identity over the authenticated API, and serves
  the private half from a transient in-process ssh-agent — ssh names
  the identity by its public half and signs through the socket, so
  the key never becomes a file on the client. Logs in as the `msks`
  workspace user by default (`-l root` for recovery); per-workspace
  `known_hosts` under `accept-new`. See `docs/cli.md` and the alias
  workflow in `docs/networking.md`.
- **Home-volume export/import (#80).** A workspace's `/home` volume
  now moves through the daemon's authenticated listener as byte
  streams: `GET`/`PUT /api/v1/workspaces/{id}/home` stream the
  volume file out and replace it from an uploaded ext4 image, with
  `msks home export` / `msks home import` on top (`-` speaks stdio,
  so the bytes compose with gzip or ssh). The workspace must be
  stopped (`unknown` refuses too — the VM may be live), moves
  serialize against boots and deletes per workspace (waiters
  answer a named 409 after `MSKSD_MOVE_WAIT_TIMEOUT_S`, and the
  daemon re-checks the live VMM under the lock), an upload that
  fails the ext4 check or dies mid-body leaves the existing volume
  in place, all-zero windows come back sparse, and each completed
  move publishes a `home.exported` / `home.imported` event. See
  [storage](/storage/#home-volume-export-and-import) and
  [the CLI](/cli/#msks-home).
- **Minted workspace identity and `msks key` (#111).** msksd mints a
  per-workspace ssh keypair at create — Ed25519 by default (#115,
  #138), with the type configurable via
  `MSKSD_SSH_KEY_TYPE` — and stores both halves with the workspace's
  state. The public half is planted into `authorized_keys` for root
  and the `msks` workspace user through the first-boot seed (composed
  with any `user_data` payload), so a fresh workspace accepts ssh
  with no manual key steps. `msks key <ws>` fetches the identity over
  the token-gated API; `--private` prints the private half, `--out`
  writes it mode 0600. The halves persist across daemon restarts and
  workspace stop/start.
- **Guest sshd and rsync (#110).** The workspace image serves TCP on
  its forward path: sshd (Debian's own package, enabled) listens on
  all interfaces behind a boot-time unit that waits for the NIC's
  address (15s ceiling), with key-only login (`PasswordAuthentication
no`, `PermitRootLogin prohibit-password`) pinned by a config dropin,
  and rsync ships in the image. Host keys are generated on first boot
  into the persistent root overlay, so they survive stop/start — the
  recorded `known_hosts` entry keeps matching. The egress input
  rules admit the conntrack-established replies to the
  appliance-originated forward dial; guest-initiated connections
  arrive state NEW and still drop. See
  [networking](/networking/#reaching-guest-services-the-forward).
- **`msks forward` and the forward websocket (#109).** A workspace's
  guest TCP ports reach the client through a new authenticated
  endpoint, `WS /api/v1/workspaces/{id}/forward/{port}`: the daemon
  dials the workspace's deterministic tap address (retrying through
  the DHCP/service bring-up race) and pumps raw bytes both ways —
  one websocket per guest TCP connection, no framing, with
  `forward.opened`/`forward.closed` events on the events channel.
  The client command bridges stdio (the ssh ProxyCommand shape) or,
  with `--local PORT`, a loopback listener where every accepted
  connection gets its own forward. The token authenticates through
  the `Authorization` header — the REST surface's Bearer form, kept
  out of URLs and their logs; a workspace without egress is refused
  with the reason naming the missing NIC. See `docs/cli.md`.

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

- **Dev state moved under `.devenv/state/` (#156).** The three
  runtime state directories that sat at the repo root — `.guest/`
  (nix-built guest assets), `.appliance/` (appliance image, sockets,
  token, CA), and `.msksd/` (bare-host daemon state) — now live at
  `.devenv/state/guest/`, `.devenv/state/appliance/`, and
  `.devenv/state/msksd/`, and each location is relocatable via an
  environment variable (`MSKS_GUEST_DIR`, `MSKS_APPLIANCE_DIR`, and
  `MSKSD_STATE_DIR` respectively — every build/run script, task, and
  test resolves the same way). Existing state does not migrate:
  move the directory (or rebuild — `devenv processes up -d`
  regenerates the appliance, `msks:dev-ready` re-mints the token).
  The devenv shell also prunes its own stale one-shot wrappers
  (`.devenv/shell-*.sh` older than an hour) automatically.
- **Minted identity keys default to `ed25519` (#138).** The
  client mint (`--key-type`, `msks create`) and the daemon mint
  (`ssh_key_type` / `MSKSD_SSH_KEY_TYPE`) now mint Ed25519 keys —
  FIPS-approvable (FIPS 186-5) and accepted by ssh clients
  restricted to the common `ssh-ed25519,ssh-rsa` set, so `msks ssh`
  into a default-created workspace needs no algorithm passthrough.
  `--key-type ecdsa` (the former default) and `--key-type rsa`
  remain available for deployments whose validated crypto module
  predates EdDSA. See `docs/networking.md`.
- **`msks console` (#117).** The interactive workspace command is
  renamed from `msks shell` to `msks console`, matching the
  `/api/v1/workspaces/{id}/console` websocket it speaks. A hard
  rename: `msks shell` now exits with the standard unknown-command
  error. See `docs/cli.md`.

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
- **Nix-built guest assets (`msks:build-guest`, `msks:demo-vm`, `msks:build-runner-image`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into the guest state dir (`.devenv/state/guest/`), plus the k8s vm-runner container archive — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `MSKSD_TEST_VMLINUX`/`MSKSD_TEST_ROOTFS`/`MSKSD_TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks:demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).

- **`msksd --reload` (development, #144).** The daemon gains a
  development flag that watches the msks package tree it runs from
  and restarts the process when it changes; the appliance dev tree
  (above) uses it to serve daemon edits without an appliance
  rebuild.

### Fixed

- **Failed starts keep an actionable status (#158).** A `vm.boot`
  failure reaps the half-created VMM and the watcher's dead-socket
  probe consults the identity check, so the workspace reports
  `stopped` — not the incident's `unknown`, which a bare pid check
  satisfied with a recycled pid — and the next `msks start`
  retries. The incident's exact shape, recycled-pid trigger
  included, is pinned by tests; the README now states that a
  running appliance serves the daemon its image was built with,
  and a stop/start lands a merged fix.

- **Stale workspace sockets no longer break start (#151).** A VMM
  killed without cleanup (host crash, appliance hard stop) left
  `api.sock`/`vsock.sock` behind under `vms/<id>/`; the next start
  died binding them and surfaced a misleading
  unreachable-API/ENOENT 503. The start path now sweeps refused
  sockets (a live listener is never unlinked), removes the stale
  `ch.pid` (a recycled pid after a host reboot no longer blocks
  starts with "already exists"), and a VMM that dies at spawn names
  its own cause in the error (the tail of its ch.log) instead of a
  bare exit code. VMM liveness is identity-checked against
  `/proc/<pid>/cmdline`: a pid that is not this workspace's VMM is
  never signaled, and one owned by another user is named in the
  error instead of silently reported stopped.
- **Legacy CA crash-loop on leaf remint (#148).** A CA minted before
  the strict-clean change parses but carries no Subject Key
  Identifier; the leaf remint that a host change triggers read the
  missing extension and exited, and `Restart=always` looped the
  daemon forever (observed live on a long-lived appliance state disk
  after #146 changed the bind host). Such a CA is now replaced
  wholesale — a stderr line names the replacement beside the new
  fingerprint on the serial log — instead of crash-looping. Current
  strict-clean pairs keep leaf-only remints (same CA fingerprint).
- **Strict-clean minted TLS certificates (#141).** The CA and leaf
  msksd generates now carry Subject/Authority Key Identifiers and
  the CA a `keyCertSign` KeyUsage, so Python 3.14 clients — whose
  default SSL context enables `VERIFY_X509_STRICT` — verify the pair
  instead of rejecting it. Existing state directories keep their
  current pair; removing the `msks-ca*.pem`/`msks-cert*.pem` files
  under the state dir lets the next start mint the strict-clean
  replacement.
- **Mid-session console stalls no longer wedge a session open (#103).**
  The guest console helper's byte pump blocked on whichever direction
  stalled first, so a wedged vsock transport froze the whole session —
  input echoed but never executed, no output, no close. The pump now
  moves each direction independently and a stream that accepts no
  writes for 300 s ends the session; on the daemon side,
  `console_stall_timeout_s` (`MSKSD_CONSOLE_STALL_TIMEOUT_S`, 60 s
  default, `0` disables) closes a console websocket with 4502 when
  client input draws no guest bytes for the window — `msks shell`
  names the close, and reconnecting opens a fresh session. The
  echo-off caveat (password prompts) and the helper-window
  relationship are documented in `docs/cli.md` and `docs/config.md`.
- **Egress DHCP/DNS under uvloop-running daemons (#144).** The
  development venv pulls uvloop (via `uvicorn[standard]`), which
  does not implement `loop.sock_recvfrom`/`sock_sendto`; egress
  workspaces created by such a daemon never received a DHCP lease
  or DNS answer. The DHCP and DNS services now receive datagrams
  through a loop-portable helper and behave identically under
  asyncio and uvloop. This was also the real cause behind the
  "guest NIC up but no frames" symptom recorded on #143 — not the
  container boundary.

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
