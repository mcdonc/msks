# Workspace images

A workspace image is one file: a **container-image tar** — the layout
`podman load` and `skopeo` already understand — carrying a kernel, an
initrd, a rootfs disk, and a manifest that describes all three. This
chapter covers the image contract, how to build one, how to check
one, how to register it with a running daemon, and how workspaces
select an image.

msksd boots the kernel directly and attaches the rootfs as a virtio
disk. Nothing ever runs the image as a container; the container-image
packaging exists so the file is inspectable and loadable by stock
tooling (`tar tf`, `podman load`, `skopeo copy`) and can travel
through container registries unchanged.

## The image contract

The tar follows the containerDisk convention: standard container
bookkeeping (`manifest.json`, an image config, `repositories`)
wrapped around a single **uncompressed layer** whose root carries the
workspace boot files:

```text
workspace-<name>-<version>.tar
├── manifest.json                     # container bookkeeping
├── repositories
└── <image-id>/
    ├── VERSION, json                 # container bookkeeping
    └── layer.tar                     # uncompressed; the containerDisk:
        ├── boot/vmlinuz              #   bzImage kernel
        ├── boot/initrd.img           #   initramfs
        ├── disk/rootfs.ext4          #   raw ext4 root filesystem
        └── disk/image.json           #   the manifest (below)
```

`disk/image.json` is what msksd reads; schema 2:

| Field              | Meaning                                                                                        |
| ------------------ | ---------------------------------------------------------------------------------------------- |
| `schema`           | `2`                                                                                            |
| `name`             | Catalog name, e.g. `debian`                                                                    |
| `version`          | Catalog version, e.g. `13.6`; numeric segments sort correctly                                  |
| `cmdline`          | Kernel command line for workspace boots                                                        |
| `vsock_shell_port` | AF_VSOCK port the guest's console service listens on                                           |
| `kernel_version`   | e.g. `6.12.107+deb13-amd64` (informational)                                                    |
| `kernel_format`    | `bzImage` (informational)                                                                      |
| `console_protocol` | Console handshake: `prelude-v1` (identity prelude) or `legacy` (raw root shell; default)       |
| `console_users`    | Users `msks console --user` may request beyond the row's login user (#248); default `["root"]` |
| `capabilities`     | Optional capability object; `provisioner` names the seed consumer (below)                      |

The manifest is self-describing: importing the archive needs nothing
beside the archive itself.

### What the host provides

Every workspace artifact the guest touches is owned by msksd at
runtime: the root and home volumes and the cidata seed disks are
created under the state dir (`MSKSD_STATE_DIR`, the one relocation
variable), and the console reaches the guest over AF_VSOCK through
the VMM msksd launched — both deployment shapes (the NixOS module
and the dev daemon) deliver this without host-side scripting. The
host itself provides four things, all configuration:

- **`/dev/kvm`**, reachable through the `kvm` group.
- **Two ambient capabilities** for msksd's service user:
  `CAP_NET_ADMIN` (taps, addresses, nftables) and
  `CAP_NET_BIND_SERVICE` (the DHCP and DNS listeners).
- **The egress kernel modules** — `tun` and the nftables/NAT set —
  loaded at boot (the NixOS module's `boot.kernelModules`).
- **`net.ipv4.ip_forward=1`**, verified by the daemon at start
  when egress is armed; the module ships it as a sysctl.

The daemon e2e smoke (`test_smoke/test_daemon_e2e.py`) drives this
whole surface against a real `msksd` process: the image boots,
serves its console, and reaches the outside exactly as this
section describes.

### What a guest must provide

The rootfs and kernel together decide whether a workspace actually
boots. A guest image must:

- **Boot a kernel with direct-kernel boot support.** cloud-hypervisor
  loads the bzImage and initrd itself and passes `cmdline`; the guest
  never runs its own bootloader. Stock Debian/Ubuntu kernels work.
- **Ship a virtio console service.** `msks console` connects over
  AF_VSOCK, so the guest needs `vmw_vsock_virtio_transport` (module
  or built-in) and a service that binds the vsock port and spawns
  login shells — the shipped image runs `msks-console-helper` (a
  static Rust binary the image builds from `src/console-helper`) as
  `msks-console.service`, `Restart=always`. The helper accepts
  host-originated connections only, and each connection negotiates
  the identity prelude (#63): the requested user, the client
  terminal's size and TERM, then an `MSKS OK` reply (or a named
  refusal) before the helper allocates the pty at the client's
  geometry, drops to that user through the full
  setgroups/setgid/setuid sequence, and execs the user's login
  shell with a passwd-built environment. The pty is a plain
  canonical terminal — the line discipline echoes and edits input —
  and it keeps its connect-time size for the session's life (live
  resizes ride an ssh session through the forward, #108–#112). An
  image whose manifest sets `console_protocol` to `legacy` (the
  default) serves the raw root shell instead: the user, size, and
  TERM never reach the guest, and the manifest's `console_users`
  still gates which users `msks console --user` may request. For
  both protocols, the workspace's recorded login user (#248) is
  served beside the manifest's list: the first-boot seed
  provisions that account, so the console accepts it even though
  the image never shipped it (a `legacy` image still ignores the
  name on the wire — its console is the raw root shell).
- **Take an address over DHCP when a NIC is present.** Workspaces
  are networked by default (#52): the VM boots with a virtio-net NIC
  — Debian's kernel ships `virtio_net`, and the shipped overlay loads
  it at boot — and the daemon's DHCP service offers the address,
  gateway, and its own resolver. The overlay carries a
  systemd-networkd `.network` unit (`Name=en* eth*`, `DHCP=yes`)
  plus resolved's stub resolv.conf, so the guest configures whatever
  NIC appears. The offered resolver becomes resolved's per-link
  upstream: `/etc/resolv.conf` names the `127.0.0.53` stub, and the
  lease's resolver in effect is read from `resolvectl dns` (the
  git-out smoke's probe, #81). A workspace created with
  `"egress": false` presents no NIC: nothing matches the unit and
  networkd stays idle — the same image serves both postures. The
  shipped base carries no developer toolchain: git, curl, and uv
  all land over the egress NIC when a seed or the operator
  installs them (#77, #81).
- **Answer the ACPI power button.** `stop` presses the power button
  (`vm.power-button`) and waits; the guest's own handler runs the
  clean shutdown that flushes its disks — systemd-logind does this
  by default, an acpid rule works too. A guest that ignores the
  button is killed at the deadline, and whatever its page cache
  still held is lost.
- **Tolerate a read-write root.** The daemon boots each workspace's
  root read-write through a per-workspace qcow2 overlay (#14) — the
  base image stays pristine under copy-on-write — and attaches an
  ext4 volume at `/home`. Mount it by the volume label `msks-home`
  (the daemon formats the volume with that label) so the mount
  stays on the right device whatever the disk order is; `nofail`
  keeps boots moving when the volume is absent.
- **Grant passwordless sudo to the `wheel` group.** Both images
  ship the conventional admin group `wheel` whose members
  administer the VM: Debian through a `%wheel` dropin in
  `/etc/sudoers.d` (the image creates the group — Debian's own
  tree carries none), NixOS through a declarative sudo rule. The
  identity seed (#248) creates a named login user, gives it the
  workspace user's shell, and joins it to the group — the seed
  never writes sudo configuration, so a guest that is rebuilt
  keeps exactly the sudo policy its configuration declares.
- **Run cloud-init against the cidata seed disk.** A workspace
  created with `user_data` (#41) or a minted identity (#111) boots
  with a third, read-only virtio disk: a small iso9660 filesystem
  labeled `cidata` carrying `user-data` (the operator payload,
  composed beside the identity's seeding script when a key was
  minted) and `meta-data`
  (`instance-id`, keyed off the workspace id) at its root — exactly
  cloud-init's NoCloud seed layout. The image ships cloud-init (the
  Debian `genericcloud` base does), and two dropins pin the
  behavior the msks contract needs: `datasource_list: [ NoCloud,
None ]` (the seed disk answers immediately — no EC2 or OpenStack
  probing, no network timeouts) and `network: {config: disabled}`
  (the image's own networkd unit owns whatever NIC appears;
  cloud-init's netplan rendering stays out of the way). Both
  payload forms run: cloud-config YAML and `#!` scripts.

  cloud-init's run-once state (the `/var/lib/cloud` cache) lives on
  the workspace's root overlay, so `stop`/`start` never
  re-provisions and a factory reset does (the reset drops the
  overlay). Any distro cloud image that ships cloud-init (Debian's
  `generic` and `genericcloud`, Ubuntu, Fedora, ...) imports as-is
  and declares `"capabilities": {"provisioner": "cloud-init"}`.

## Building an image

### The shipped builder

```bash
msks-build-guest
```

builds the default image (`workspace-debian-13.6.tar`) from a
date-pinned, sha512-verified Debian trixie **genericcloud** qcow2 —
the cloud-init-bearing variant of Debian's cloud image family, so
cloud-init and its python3 runtime arrive with the base. The build
converts the qcow2 to raw, extracts the root filesystem, applies the
msks overlay and boot diet, repacks it as a deterministic ext4
(`mke2fs -d` under fakeroot; the intermediate rides the nix store as
one opaque tarball, never as a tree), and wraps the kernel, initrd,
rootfs, and a generated `image.json` into the container-image tar
with byte-stable tar flags (`--sort=name --mtime=@1
--numeric-owner`). Identical rebuilds hash identically, so the same
image deduplicates across hosts.

The output lands under `.devenv/state/guest/` (`MSKS_GUEST_DIR`
relocates it); `scripts/build-guest.sh` and
`nix/guest-debian.nix` document every step and are the reference for
what an image build does.

Both images bake the agent toolchain (#266, #268): pinned Node,
the pinned pi coding agent, the pinned herdr terminal
workspace manager (herdr.dev, the release's static binary, with
its license), and pinned Claude Code (the npm wrapper plus its
linux-x64 native binary, staged in npm's global layout with the
wrapper's own post-install linking done at build time), plus the
pi model-discovery extension in `/etc/skel` and root's home
(`docs/llm.md` describes what the extension does at pi
startup). The staging differs per image: Debian lands the
official Node tarball and the pinned packages under `/usr/local`;
NixOS rides nixpkgs' own Node and the same pinned packages
through the system profile, with Claude Code's native binary
loader-patched to the closure's glibc (a stock NixOS ships no
usable loader where the published interpreter points) and the
extension planted by tmpfiles copy-once rules. The pins —
`agentNodeTarball` in `nix/guest-debian.nix`; `piTarball`,
`npmDepsHash` (and, on a pi bump, the table in
`nix/pi-shrinkwrap-integrity.json`), `agentHerdrBinary` and its
license pin, and the two `agentClaude*` tarballs in
`nix/agent-toolchain.nix` — move with an image rebuild, and the
build stays pure derivations: pi's dependency closure is
prefetched against its shrinkwrap (with the five integrity gaps
the published lock leaves, closed by hash) and installed offline.
A workspace that already booted keeps the toolchain it booted
with; a rebuilt image serves the new pins to the next workspace.

pi's own tool dependencies ride the image too (#272): pi resolves
`fd` and `rg` from PATH — at interactive startup and again at
first tool use — and downloads each from GitHub releases when it
finds neither: a download a fresh workspace's first agent start
would otherwise wait on, behind the egress interceptor and the
GitHub API quota every workspace behind one address shares. The Debian image stages Debian's own `fd-find`
and `ripgrep` debs (`fdFindDeb` and `ripgrepDeb` in
`nix/guest-debian.nix`, pinned by pool URL and checksum like the
kernel and rsync debs) with the same linkage guard rsync gets;
fd-find's binary lands under `/usr/lib/cargo/bin` with
`/usr/bin/fdfind` a symlink, exactly the layout `apt install
fd-find` leaves, and pi accepts the `fdfind` name. The NixOS image
ships nixpkgs' own `fd` and `ripgrep` on the profile PATH the
toolchain rides. With both present, a first `pi` start needs no
downloads.

The tools themselves decide some of their own freshness: Claude
Code checks for updates on startup and installs a newer self into
the user's home when it finds one (its `autoUpdates` setting and
the `DISABLE_AUTOUPDATER` environment variable are the switches),
and herdr's `herdr update` does the same on demand. A workspace
user can therefore run a newer tool than the image pin — the pin
governs what a fresh workspace starts with, not what a user's home
accumulates. A no-egress workspace sees these checks fail quietly.

```bash
msks-build-guest nixos
```

builds the second catalog image (`workspace-nixos-<version>.tar`)
from a NixOS system evaluated against the same pinned nixpkgs the
development shell uses — the guest's console helper, cloud-init,
sshd, and rsync are built by nixpkgs instead of fetched as Debian
artifacts. The root filesystem is the whole system closure packed
into a fresh ext4 (the same `mke2fs -d` under fakeroot; no cloud
image exists to extract), and the guest boots with no nix
database anywhere — every store path resolves from the image
itself. The archive carries the same `image.json` schema with the
same declared capabilities (`cloud-init`, `prelude-v1`): the daemon
serves it with nothing keyed off the image's name. The output
lands under `.devenv/state/guest-nixos/` (`MSKS_GUEST_NIXOS_DIR`
relocates it); `nix/guest-nixos.nix` documents every step. The
image ships the same agent toolchain as the Debian one (#268):
nixpkgs' own Node and the shared pins (`nix/agent-toolchain.nix`)
ride the system profile — the loader-patched Claude Code and the
tmpfiles-planted extension staging described above.

Each image build also executes every staged launcher and fails on
one that does not run (#272): the Debian build runs `node`, `pi`,
`claude`, `fdfind`, and `rg` through the tree's own dynamic loader
and libraries — the same world the guest execs them in, because
the build sandbox carries no `/usr/bin/env` or `/lib64` — while
`herdr`, a static binary, runs as-is; pi's and npm's published
`#!/usr/bin/env node` shebangs, which that sandbox cannot resolve,
are asserted byte-exact instead. The NixOS build runs the profile
binaries directly. The first image that shipped the toolchain
passed every presence check while `pi` could not start: the npm
build had rewritten cli.js's shebang to a build-time Nix store
node no guest carries, and `test -x` on a symlink says nothing
about the interpreter behind it. The launcher executions turn
that class of breakage into a build failure.

### Building your own

Any rootfs that satisfies the contract above can become a workspace
image. The outline, using a distro's own cloud image as the source:

1. Fetch the source image and verify it against its published
   checksum.
2. Produce a raw ext4 of the guest root filesystem
   (`qemu-img convert` + partition extraction, or unpack the
   cloud image's root archive directly).
3. Install the console service and enable it; make sure the vsock
   module is present and `/dev/vsock` is created at boot.
4. Bring up networking. Workspaces boot with a virtio-net NIC by
   default (#52), so the image must be able to configure one — how is
   the distro's choice:
   - a kernel with the `virtio_net` driver present (module or
     built-in; on most distros udev autoloads the module when the
     device appears);
   - a DHCP client that configures whatever NIC appears and honors
     the offered address, gateway, and resolver — networkd,
     dhcpcd, or anything else that speaks DHCP;
   - name resolution must follow the resolver DHCP names (however
     the distro wires `/etc/resolv.conf`);
   - a boot with no NIC (a workspace created with `"egress": false`
     ) must still reach a usable login — the same image serves both
     postures.

   The shipped image does this with systemd-networkd + resolved; any
   equivalent stack works.

5. Ship cloud-init, configured for the cidata seed. Starting from
   a distro cloud image (Debian `generic`/`genericcloud`, Ubuntu,
   Fedora — they carry cloud-init and its python runtime) gives you
   this for free; otherwise install the distro's cloud-init package.
   Two dropins under `/etc/cloud/cloud.cfg.d/` pin the msks contract:
   - `datasource_list: [ NoCloud, None ]` — the workspace's seed
     disk answers immediately; nothing probes EC2 or OpenStack
     sources or waits on the network;
   - `network: {config: disabled}` — the network configuration from
     step 4 owns the NIC; cloud-init's renderer would only fight it.

   The guest kernel needs the `isofs` module present to mount the
   seed (every stock distro kernel carries it). Declare the consumer
   in the manifest (step 6) so listings show what eats the seed. An
   image whose guest runs no cloud-init still accepts `user_data` at
   create, but nothing executes it — the daemon cannot tell.

6. Write `disk/image.json` describing your kernel, cmdline, and
   vsock port, and declare `"capabilities": {"provisioner":
"cloud-init"}`.
7. Lay out `boot/` and `disk/` as the layer tree and wrap it:

```bash
tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
    -C layer-root -cf workspace-mine-1.0.layer.tar .
# then wrap layer.tar in a container-image tar, or simply:
podman import workspace-mine-1.0.layer.tar workspace-mine:1.0
podman save -o workspace-mine-1.0.tar workspace-mine:1.0
```

`podman import`/`save` produce a valid outer layout; msksd accepts
both compressed and uncompressed layers (uncompressed layers keep
import and in-place inspection cheaper).

## Checking an image

The daemon verifies layout and manifest at import, but a tar that
parses can still carry a guest that does not boot — the layout is
the only thing the daemon can see. `msks image check <archive>` (#258)
is the gate an image author runs before publishing: it boots the
archive exactly the way a workspace boots (same driver, overlay,
home volume, seed disk, and console path — against a throwaway
state dir under `/tmp`) and reports each contract point by name:

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

Any host with `/dev/kvm` runs it — nothing else from msks is
needed (no daemon, no state). The points map one-to-one onto "What
a guest must provide" above: the archive layout and manifest, a
guest that answers the vsock console under the protocol the
manifest declares, a root that accepts writes through the overlay,
`/home` mounted by its label (both surviving a stop/start cycle),
the seed payload running on first boot (checked when the manifest
declares a provisioner; a provisioner-less image reports the point
skipped with that reason), and the ACPI power button producing a
clean shutdown. A broken point fails its row, the exit code is 1,
and the summary names the first failure; `--keep` preserves the
throwaway state dir (serial logs included) for inspection.

`--egress` adds the networking point — the guest must take a
global address over DHCP when the checker boots it with a NIC
through the daemon's own net stack. That leg needs root (taps,
nftables, the DHCP and DNS listeners on their privileged ports)
and an egress-capable default route (`--uplink` names another
interface); the checker sets `net.ipv4.ip_forward` to 1 for the
pass and restores what it found — a checker killed outright
leaves the sysctl at 1. Run the leg on a host that is not serving
the real daemon: both bind the same privileged DHCP and DNS
ports. The no-NIC half of the posture is
the core pass itself: it boots without a NIC and requires a usable
login. The write probes (`root-rw`, `home-label`, `user-data`)
run over a root console whenever the image serves one
(`console_users`); an image whose console serves other users only
gets those users' reach reported.

## Registering an image

Import makes the daemon unpack the boot files once into a per-hash
cache and record the image in the catalog. The `msks image` commands
drive this surface from the CLI — `msks image import <path>`, `msks
image ls`, `msks image rm <ref>`, and `msks image info <ref>` (see
`docs/cli.md`); the raw HTTP form:

```bash
curl -X POST https://192.168.77.2:8660/api/v1/images \
  -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"source": "/path/on/the/daemon/workspace-mine-1.0.tar"}'
```

The `source` is a path on the **daemon's** filesystem (the daemon
reads them from its state dir), or — #258 — an `https://` URL the
daemon downloads itself:

```bash
msks image import https://images.example.com/workspace-mine-1.0.tar
curl -X POST .../api/v1/images -d '{"source": "https://images.example.com/workspace-mine-1.0.tar"}'
```

A URL download lands in the catalog's staging area under a size
ceiling and a deadline (`MSKSD_IMAGE_IMPORT_MAX_MIB`,
`MSKSD_IMAGE_IMPORT_TIMEOUT_S`; see `docs/config.md`), verifies
TLS against the system roots, refuses a redirect that leaves
https, and imports from the downloaded copy — so the recorded hash
always reflects the fetched bytes. The ceiling also clamps to the
bytes the storage floor protects, so the download itself cannot
spend the state disk's headroom. The fetch runs with the calling
token's authority: a deployment hands its API token to operators
it trusts to import images, and the URL form reaches whatever
network the daemon can reach — the same trust a daemon-side path
import already carries. Either way the daemon
copies privately and hashes that copy, so a source file changing
underneath the import cannot desync the recorded hash from the
imported content, and importing the same content twice is idempotent.

Rules worth knowing:

- The **first image imported becomes the default** — the one a bare
  workspace create uses — and keeps the designation while more
  images arrive; re-designating the default is future API work.
- `MSKSD_DEFAULT_IMAGE` (set by the dev daemon from its state dir's
  image) imports and designates at first boot; on later boots the
  daemon only re-checks the hash, not a full re-import.
- Listing shows every registered image with its hash, name, version,
  kernel facts, and which one is default:

```bash
curl -H "authorization: Bearer $TOKEN" \
  https://127.0.0.1:8660/api/v1/images
```

- Storage: one image costs roughly twice its rootfs size on the
  daemon's state dir (the retained archive plus the unpacked boot
  cache), carried beside the workspace overlays and volumes —
  `docs/storage.md` has the capacity model.

- An image with workspaces still booting it cannot be removed
  (`DELETE /api/v1/images/{hash}` answers 409 naming the workspace);
  once its workspaces are gone, deletion drops the cache and the
  retained archive.

## Using an image

Workspace create selects an image by reference:

```bash
# name:version — exact
curl -X POST .../api/v1/workspaces -d '{"id": "ws1", "image": "debian:13.6"}'

# bare name — resolves to the newest registered version
{"id": "ws1", "image": "debian"}

# name@hash — pins identity AND content
{"id": "ws1", "image": "debian@<sha256>"}

# bare hash — content-addressed
{"id": "ws1", "image": "<sha256>"}

# no image at all — the designated default
{"id": "ws1"}
```

Versions order numerically (`13.10` sorts after `13.9`). A malformed
reference (`debian@not-a-hash`) is a named 400 rather than a
miss. Explicit `kernel`/`rootfs` fields still win over the image —
the image is the convenient path, not a mandate.

## First-boot provisioning (`user_data`)

A workspace created with `user_data` gets a customization payload
that runs on its first boot — EC2-style, delivered on the workspace's
own seed disk:

```bash
# a script (any image with a provisioner; the leading #! is what
# makes it one)
msks create ws --user-data provision.sh --start

# cloud-config (needs a cloud-init image)
curl -X POST .../api/v1/workspaces -d '{
  "id": "ws", "image": "mycloud:1.0",
  "user_data": "#cloud-config\nusers:\n- name: alice\n..."
}'
```

The rules worth knowing:

- **Create-time and immutable.** The payload is part of the
  workspace's identity; `user_data` is accepted only at create
  (capped at 64 Ki characters — pydantic's 422 names the bound; an
  empty payload is a 400), and any mutation attempt answers a named
  405 (delete and recreate to change it). cloud-init keys its
  run-once semantics off the workspace, so changing it after the
  fact would silently do nothing anyway.
- **The seed is per-workspace state.** It is built at create (a
  few hundred KiB of iso9660 overhead regardless of payload size,
  `cidata`-labeled), attached read-only as the third disk, survives
  `stop`/`start` and factory reset, and is deleted with the
  workspace. It can embed tokens, so the daemon stores it mode 0600
  under the workspace's own directory — and creates its database
  file (which records the payload on the workspace's row) 0600 too.
  Listing endpoints and `msks ls --json` echo the payload back over
  the same TLS + token channel as the console.
- **Both payload forms run.** cloud-init executes `#!` scripts from
  `user_data` and applies cloud-config documents alike; declare the
  image's `capabilities.provisioner: cloud-init` so operators and
  tooling can see what consumes the seed.
- **No `user_data`, no seed.** A workspace created without a payload
  boots with two disks and no added cost: cloud-init finds no seed,
  applies nothing, and the interactive budget is unchanged (~3.0s
  start→shell).
- **Failure posture is cloud-init's.** The vsock console starts
  before cloud-init runs (the interactive budget is unaffected —
  the shipped image measures ~3.0s start→shell), and payloads run
  in cloud-init's final stage: a slow or hanging payload delays
  boot-complete, not the shell. `cloud-init status --wait` (or the
  serial log) says when provisioning finished; a factory reset
  re-runs the payload from the same seed.
