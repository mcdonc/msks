# Workspace images

A workspace image is one file, a **container-image tar**: the layout
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

| Field            | Meaning                                                                   |
| ---------------- | ------------------------------------------------------------------------- |
| `schema`         | `2`                                                                       |
| `name`           | Catalog name, e.g. `debian`                                               |
| `version`        | Catalog version, e.g. `13.6`; numeric segments sort correctly             |
| `cmdline`        | Kernel command line for workspace boots                                   |
| `kernel_version` | e.g. `6.12.107+deb13-amd64` (informational)                               |
| `kernel_format`  | `bzImage` (informational)                                                 |
| `capabilities`   | Optional capability object; `provisioner` names the seed consumer (below) |

The manifest is self-describing: importing the archive needs nothing
beside the archive itself.

### What the host provides

Every workspace artifact the guest touches is owned by msksd
at runtime. The root and home volumes and the cidata seed
disks are created under the state dir (`MSKSD_STATE_DIR`, the
one relocation variable). The console reaches the guest over
the virtio-console port of the VMM msksd launched. Both
deployment shapes (the NixOS module and the dev daemon)
deliver this without host-side scripting. The
host itself provides four things, all configuration:

- **`/dev/kvm`**, reachable through the `kvm` group.
- **Two ambient capabilities** for msksd's service user:
  `CAP_NET_ADMIN` (taps, addresses, nftables) and
  `CAP_NET_BIND_SERVICE` (the DHCP and DNS listeners).
- **The egress kernel modules**: `tun` and the nftables/NAT set,
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
- **Serve a root getty on the serial console.** `msks
console` bridges the client to the guest's ttyS0 through the
  VMM's serial socket, so the guest needs an autologin root
  getty on its serial console. The shipped images run
  `serial-getty@ttyS0` with an autologin drop-in. The kernel console (`console=ttyS0`) shares
  the port: kernel messages interleave with the session, which is
  exactly what a failsafe console wants. Authentication is the daemon's TLS + token listener, and the
  stream is raw bytes with no in-band protocol. The session
  keeps the getty's own terminal geometry for its life. A
  workspace user's shell with the client's terminal size rides
  ssh through the forward seam.
- **Take an address over DHCP when a NIC is present.**
  Workspaces are networked by default: the VM boots with a
  virtio-net NIC, and the daemon's DHCP service offers the
  address, gateway, and its own resolver. Debian's kernel
  ships `virtio_net`, and the shipped overlay loads it at
  boot. The overlay carries a
  systemd-networkd `.network` unit (`Name=en* eth*`, `DHCP=yes`)
  plus resolved's stub resolv.conf, so the guest configures whatever
  NIC appears. The offered resolver becomes resolved's per-link
  upstream: `/etc/resolv.conf` names the `127.0.0.53` stub, and the
  lease's resolver in effect is read from `resolvectl dns` (the
  git-out smoke's probe). A workspace created with
  `"egress": false` presents no NIC: nothing matches the unit and
  networkd stays idle — the same image serves both postures. The
  shipped base carries no developer toolchain: git, curl, and uv
  all land over the egress NIC when a seed or the operator
  installs them.
- **Answer the ACPI power button.** `stop` presses the power button
  (`vm.power-button`) and waits; the guest's own handler runs the
  clean shutdown that flushes its disks — systemd-logind does this
  by default, an acpid rule works too. A guest that ignores the
  button is killed at the deadline, and whatever its page cache
  still held is lost.
- **Tolerate a read-write root.** The daemon boots each workspace's
  root read-write through a per-workspace qcow2 overlay, so the
  base image stays pristine under copy-on-write, and attaches an
  ext4 volume at `/home`. Mount it by the volume label `msks-home`
  (the daemon formats the volume with that label) so the mount
  stays on the right device whatever the disk order is; `nofail`
  keeps boots moving when the volume is absent.
- **Grant passwordless sudo to the `wheel` group.** Both
  images ship the conventional admin group `wheel` whose
  members administer the VM. Debian does it through a `%wheel`
  dropin in `/etc/sudoers.d`, and the image creates the group
  because Debian's own tree carries none. NixOS does it
  through a declarative sudo rule. The identity seed creates a named login user, gives it the
  workspace user's shell, and joins it to the group. The seed
  never writes sudo configuration, so a guest that is rebuilt
  keeps exactly the sudo policy its configuration declares.
- **Run cloud-init against the cidata seed disk.** A
  workspace created with `user_data` or a seeded ssh identity
  boots with a third, read-only virtio disk: a small iso9660
  filesystem labeled `cidata`, exactly cloud-init's NoCloud
  seed layout. Its root carries `user-data` (the operator
  payload, composed beside the identity's seeding script when
  a key was supplied) and `meta-data`. The meta-data holds
  `instance-id`, keyed off the workspace id, and
  `local-hostname`, the workspace's creation name (the minted
  id when the workspace was created without one). The hostname
  lands at first boot, and a factory reset re-provisions from the
  seed with it; a stop/start never re-provisions (instance-id
  never changes). A launch heal that rebuilds a lost seed carries the hostname
  as well, but a guest that booted before keeps the hostname it
  already has. cloud-init sets the hostname per instance, and
  its per-boot update leaves a hostname it never set untouched,
  so only a factory reset moves an older workspace's.
  `/etc/hosts` carries the workspace's name as well: the Debian
  image turns on cloud-init's `manage_etc_hosts` in the mode that
  adds the conventional `127.0.1.1` record naming the host, in
  place, on every boot — the file's other entries stay in place.
  A guest first booted before whose healed seed now carries the
  name gets the record naming the creation name while its kernel
  hostname keeps the older one; the module reads the seed, not
  the kernel. Name resolution does not wait on the record: both
  images' resolvers answer the local hostname on their own, via
  the `myhostname` NSS module (Debian's `genericcloud` base
  ships `libnss-myhostname`, and it is part of the NixOS base
  system). The record is the plain files-source entry a tool
  reading `/etc/hosts` directly still finds.
  The image ships cloud-init (the Debian `genericcloud` base
  does), and dropins pin the behavior the msks contract needs.
  `datasource_list: [ NoCloud, None ]` makes the seed disk
  answer immediately, with no EC2 or OpenStack probing and no
  network timeouts. `network: {config: disabled}` leaves the
  image's own networkd unit owning whatever NIC appears, with
  cloud-init's netplan rendering out of the way. Both
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
cloud-init and its python3 runtime arrive with the base. The build converts the qcow2 to raw, extracts the root
filesystem, and applies the msks overlay and boot diet. It
repacks the result as a deterministic ext4 (`mke2fs -d` under
fakeroot; the intermediate rides the nix store as one opaque
tarball, never as a tree). It then wraps the kernel, initrd,
rootfs, and a generated `image.json` into the container-image
tar with byte-stable tar flags (`--sort=name --mtime=@1
--numeric-owner`). Identical rebuilds hash identically, so the same
image deduplicates across hosts.

The output lands under `.devenv/state/guest/` (`GUEST_DIR`
relocates it); `scripts/build-guest.sh` and
`nix/guest-debian.nix` document every step and are the reference for
what an image build does.

Both images bake the agent toolchain: pinned Node, the
pinned pi coding agent, and the pinned herdr terminal
workspace manager (herdr.dev, the release's static binary,
with its license). They also bake pinned Claude Code (the npm
wrapper plus its linux-x64 native binary, staged in npm's
global layout with the wrapper's own post-install linking done
at build time) and the pi model-discovery extension in
`/etc/skel` and root's home. The internal `docs/llm.md`
describes what the extension does at pi startup. The staging differs per image. Debian lands the official
Node tarball and the pinned packages under `/usr/local`.
NixOS rides nixpkgs' own Node and the same pinned packages
through the system profile. Claude Code's native binary is
loader-patched to the closure's glibc, because a stock NixOS
ships no usable loader where the published interpreter points.
The extension is planted by tmpfiles copy-once rules. The pins move with an image rebuild: `agentNodeTarball` in
`nix/guest-debian.nix`; `piTarball`, `npmDepsHash` (and, on a
pi bump, the table in `nix/pi-shrinkwrap-integrity.json`),
`agentHerdrBinary` and its license pin, and the two
`agentClaude*` tarballs in `nix/agent-toolchain.nix`. The
build stays pure derivations: pi's dependency closure is
prefetched against its shrinkwrap, with the five integrity
gaps the published lock leaves closed by hash, and installed
offline.
A workspace that already booted keeps the toolchain it booted
with; a rebuilt image serves the new pins to the next workspace.

pi's own tool dependencies ride the image too. pi resolves
`fd` and `rg` from PATH, at interactive startup and again at
first tool use, and downloads each from GitHub releases when
it finds neither. Without them, a fresh workspace's first
agent start would wait on that download, behind the egress
interceptor and the GitHub API quota every workspace behind
one address shares. The Debian image stages Debian's own `fd-find` and `ripgrep`
debs (`fdFindDeb` and `ripgrepDeb` in `nix/guest-debian.nix`,
pinned by pool URL and checksum like the kernel and rsync
debs) with the same linkage guard rsync gets. fd-find's binary
lands under `/usr/lib/cargo/bin` with `/usr/bin/fdfind` a
symlink, exactly the layout `apt install fd-find` leaves, and
pi accepts the `fdfind` name. The NixOS image
ships nixpkgs' own `fd` and `ripgrep` on the profile PATH the
toolchain rides. With both present, a first `pi` start needs no
downloads.

The tools themselves decide some of their own freshness.
Claude Code checks for updates on startup and installs a newer
self into the user's home when it finds one; its
`autoUpdates` setting and the `DISABLE_AUTOUPDATER`
environment variable are the switches. herdr's `herdr update`
does the same on demand. A workspace
user can therefore run a newer tool than the image pin — the pin
governs what a fresh workspace starts with, not what a user's home
accumulates. A no-egress workspace sees these checks fail quietly.

```bash
msks-build-guest nixos
```

builds the second catalog image (`workspace-nixos-<version>.tar`)
from a NixOS system evaluated against the same pinned nixpkgs the
development shell uses — the guest's cloud-init, sshd, and rsync
are built by nixpkgs instead of fetched as Debian artifacts. The root filesystem is the whole system closure packed
into a fresh ext4 (the same `mke2fs -d` under fakeroot; no cloud
image exists to extract), and every store path resolves from the
image itself. The archive carries the same `image.json` schema with
the same declared capabilities (`cloud-init`): the
daemon serves it with nothing keyed off the image's name. The
output lands under `.devenv/state/guest-nixos/`
(`GUEST_NIXOS_DIR` relocates it); `nix/guest-nixos.nix`
documents every step. The image ships the same agent toolchain as
the Debian one: nixpkgs' own Node and the shared pins
(`nix/agent-toolchain.nix`) ride the system profile — the
loader-patched Claude Code and the tmpfiles-planted extension
staging described above.

#### Rebuilding the system inside a NixOS workspace

A NixOS workspace is `nixos-rebuild`-ready. `nix` runs as on any NixOS host: the daemon, the build
users, and the CLI ship enabled. The image bakes everything a
rebuild evaluates against:

- **The store database.** Every store path the image ships
  is registered valid: the build loads the closure's
  registration into `/nix/var/nix/db/db.sqlite`. The guest's
  nix therefore answers reference queries, and a rebuild
  reuses the shipped closure instead of refetching or
  rebuilding it.
- **The pinned nixpkgs source.** The same revision the image was
  built from rides the store as root's channel profile, so
  `<nixpkgs>` resolves with no network. A `nix-channel --update`
  moves to the rolling channel deliberately — that is the stock
  NixOS posture, and the pin governs what a fresh workspace starts
  with.
- **`/etc/nixos/configuration.nix`.** It imports
  `nix/guest-nixos-configuration.nix`, the very module the
  image build evaluated, with the module's whole import chain
  (its package files, the shrinkwrap pair) shipped beside it
  at the same relative paths. The image build evaluates the
  shipped chain exactly as a rebuild does, so a configuration
  a rebuild cannot evaluate fails the build, not a
  workspace's first boot. A rebuild re-evaluates the shipped configuration. Edit the
  module or add settings in `configuration.nix` and run:

  ```bash
  sudo nixos-rebuild switch
  ```

- **The nix search path.** The same four entries ship in
  `/etc/nix/nix.conf` and in every login shell's `NIX_PATH`:
  `nixpkgs`, `nixos-config`, the `nixos-system` entrypoint the
  rebuild tool prefers, and the channels profile. `<nixpkgs>`
  therefore resolves identically for the root rebuild path
  and for the workspace user at the console.

- **The system profile as the boot's init.** The kernel cmdline
  names `init=/nix/var/nix/profiles/system/init` — the
  activation-maintained indirection a `nixos-rebuild switch`
  re-points — so a rebuilt system survives a workspace stop/start
  with no daemon involvement. The VMM always boots the image's
  frozen kernel and initrd, so a rebuilt system's new _kernel_
  takes effect only through a new image; userspace changes apply
  fully on reboot.

A rebuild that changes nothing but re-activates keeps every msks
contract item — the console getty, the sshd posture, cloud-init
seed handling, the agent toolchain on PATH — because the
configuration it evaluates is the one that shipped. nix works the same way for the unprivileged workspace user
logged in on the console. `nix-build '<nixpkgs>' -A <pkg>`
builds from the baked pin through the daemon;
`nix-env -iA nixpkgs.<pkg>` installs into the user's own
profile; `nix-shell -p <pkg>` builds and enters an
environment. Fetches the shipped store lacks come from
`cache.nixos.org` through the workspace's egress path. The image ships one background consumer of this readiness.
The `msks-interceptor-ca` oneshot folds the daemon's
interception CA (staged by the identity seed under
`/etc/msks`, one CA for every workspace) into the system
trust bundle. The module reads the staged certificate at
evaluation time; the unit runs one `nixos-rebuild switch` per
fresh certificate after `cloud-final`; a hash marker keeps
later boots no-ops. The image build itself refuses a host that
carries `/etc/msks/interceptor-ca.crt` (the module's assertion
fails the build), since that evaluation would bake a foreign CA
into every guest. A workspace with egress fetches substitutes from
`cache.nixos.org` through the workspace's egress path like any
other traffic. A no-egress workspace rebuilds against what is
already in the store; new packages that need a fetch fail with
nix's own error until egress is granted. Both images stay the same size class as before —
the nix closure, the validity database, and the nixpkgs source
grow the NixOS rootfs by roughly a gigabyte uncompressed.

Each image build also executes every staged launcher and
fails on one that does not run. The Debian build runs `node`,
`pi`, `claude`, `fdfind`, and `rg` through the tree's own
dynamic loader and libraries, the same world the guest execs
them in, because the build sandbox carries no `/usr/bin/env`
or `/lib64`. `herdr`, a static binary, runs as-is. pi's and
npm's published `#!/usr/bin/env node` shebangs, which that
sandbox cannot resolve, are asserted byte-exact instead. The NixOS build runs the profile
binaries directly. The first image that shipped the toolchain passed every
presence check while `pi` could not start: the npm build had
rewritten cli.js's shebang to a build-time Nix store node no
guest carries. `test -x` on a symlink says nothing about the
interpreter behind it. The launcher executions turn
that class of breakage into a build failure.

### Building your own

Any rootfs that satisfies the contract above can become a workspace
image. The outline, using a distro's own cloud image as the source:

1. Fetch the source image and verify it against its published
   checksum.
2. Produce a raw ext4 of the guest root filesystem
   (`qemu-img convert` + partition extraction, or unpack the
   cloud image's root archive directly).
3. Enable an autologin root getty on `/dev/hvc0`; make sure the
   `virtio_console` module is present so the device exists at boot.
4. Bring up networking. Workspaces boot with a virtio-net NIC by default, so the
   image must be able to configure one. How it does so is the
   distro's choice:
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

6. Write `disk/image.json` describing your kernel and cmdline,
   and declare `"capabilities": {"provisioner": "cloud-init"}`.
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
the only thing the daemon can see. `msks image check <archive>` is the gate an image author runs
before publishing. It boots the archive exactly the way a
workspace boots (same driver, overlay, home volume, seed disk,
and console path, against a throwaway state dir under `/tmp`)
and reports each contract point by name:

```text
$ msks image check workspace-mine-1.0.tar
PASS archive        imported mine:1.0 (1d6a5e782fc0)
PASS boot           guest answered the console in 3.4s (kernel 6.12.107+deb13-amd64)
PASS console        root autologin getty on the console port
PASS user-data      seed payload ran on first boot
PASS acpi-shutdown  clean shutdown within 120s
PASS root-rw        root is writable and the write survived a stop/start (overlay)
PASS home-label     /home mounted by label msks-home and its write survived a stop/start (volume)
```

Any host with `/dev/kvm` runs it — nothing else from msks is
needed (no daemon, no state). The points map one-to-one onto "What a guest must provide"
above. The checks cover the archive layout and manifest, a
guest whose console getty answers, a root that accepts writes
through the overlay, and `/home` mounted by its label (both
surviving a stop/start cycle). They also cover the seed
payload running on first boot (checked when the manifest
declares a provisioner; a provisioner-less image reports the
point skipped with that reason) and the ACPI power button
producing a clean shutdown. A broken point fails its row, the exit code is 1,
and the summary names the first failure; `--keep` preserves the
throwaway state dir (serial logs included) for inspection.

`--egress` adds the networking point — the guest must take a
global address over DHCP when the checker boots it with a NIC
through the daemon's own net stack. That leg needs root (taps, nftables, the DHCP and DNS
listeners on their privileged ports) and an egress-capable
default route (`--uplink` names another interface). The
checker sets `net.ipv4.ip_forward` to 1 for the pass and
restores what it found; a checker killed outright leaves the
sysctl at 1. Run the leg on a host that is not serving
the real daemon: both bind the same privileged DHCP and DNS
ports. The no-NIC half of the posture is
the core pass itself: it boots without a NIC and requires a usable
login. The write probes (`root-rw`, `home-label`, `user-data`)
run over the root console.

## Registering an image

Import makes the daemon unpack the boot files once into a per-hash
cache and record the image in the catalog. The `msks image` commands
drive this surface from the CLI — `msks image import <path>`, `msks
image ls`, `msks image rm <ref>`, `msks image info <ref>`, and `msks
image default <ref>` (see
`docs/cli.md`); the raw HTTP form:

```bash
curl -X POST https://192.168.77.2:8660/api/v1/images \
  -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"source": "/path/on/the/daemon/workspace-mine-1.0.tar"}'
```

The `source` is a path on the **daemon's** filesystem (the daemon
reads them from its state dir), or — — an `https://` URL the
daemon downloads itself:

```bash
msks image import https://images.example.com/workspace-mine-1.0.tar
curl -X POST .../api/v1/images -d '{"source": "https://images.example.com/workspace-mine-1.0.tar"}'
```

A URL download lands in the catalog's staging area under a
size ceiling and a deadline (`MSKSD_IMAGE_IMPORT_MAX_MIB`,
`MSKSD_IMAGE_IMPORT_TIMEOUT_S`; see `docs/config.md`). It
verifies TLS against the system roots, refuses a redirect that
leaves https, and imports from the downloaded copy, so the
recorded hash always reflects the fetched bytes. The ceiling also clamps to the
bytes the storage floor protects, so the download itself cannot
spend the state disk's headroom. The fetch runs with the calling token's authority. A
deployment hands its API token to operators it trusts to
import images, and the URL form reaches whatever network the
daemon can reach. That is the same trust a daemon-side path
import already carries. Either way the daemon
copies privately and hashes that copy, so a source file changing
underneath the import cannot desync the recorded hash from the
imported content, and importing the same content twice is idempotent.

An import registers the archive under an operator-chosen pair
when `name`/`version` ride the request — `msks image
import <path> --name mine --version 1.0` on the CLI, or the keys
beside `source` over HTTP. Either key alone moves; the other
falls back to the archive's own manifest. The override changes
the registered record — the listing, `image info`, and
`name:version` reference resolution. The archive bytes, the
content hash, and the archive's own `image.json` stay exactly as
they were; the manifest's pair stays recorded as the row's
**origin** beside the registered pair, so a usable image's
provenance reads from the catalog alone.

A cataloged row renames in place under the same rules:

```bash
msks image rename debian:13 --name mine --version 1.0
curl -X PATCH .../api/v1/images/<hash> \
  -H "authorization: Bearer $TOKEN" -d '{"name": "mine", "version": "1.0"}'
```

Either key alone keeps the other at its registered value. A rename validates the new pair the same way an import
override does: non-empty, and free of `:` and `@` (the
reference forms key on them). It refuses, with a named error
that names the holder by hash and by its origin when an
override or a rename moved it there, a pair another row
already holds. It keeps the origin pair and changes nothing
else: the bytes, the hash, the listed import time, and
workspaces already booting the image stay put. The refusal holds
for overrides and renames; a plain import of a rebuilt archive
under a pair a row already holds stays allowed (the long-standing
shape — `name@hash` and bare-hash forms resolve the ambiguity).
Re-importing unchanged bytes carries the row's current
registration forward; re-importing the same bytes with a new
override renames the row the same way a rename does.

Rules worth knowing:

- The **first image imported becomes the default** — the one a bare
  workspace create uses — and keeps the designation while more
  images arrive; `POST /api/v1/images/default` re-designates at any
  time:

```bash
# designate by any reference form create's image field takes
curl -X POST .../api/v1/images/default \
  -H "authorization: Bearer $TOKEN" -d '{"ref": "debian:13.6"}'

# clear the designation; the answer reports the fallback
# (the sole catalog entry, or none)
curl -X DELETE .../api/v1/images/default \
  -H "authorization: Bearer $TOKEN"
```

- The designation is the catalog's `default` pointer file.
  It survives restarts, and `msks image ls`/`image info` mark
  the designated row. With it cleared, a bare create falls
  back to the sole catalog entry; on a catalog holding
  several images it answers a named error until one is
  designated again.
- `MSKSD_DEFAULT_IMAGE` (set by the dev daemon from its state dir's
  image) imports and designates at first boot; on later boots the
  daemon only re-checks the hash, not a full re-import.
- `MSKSD_SEED_IMAGES` imports at every start, warm when the
  archive is unchanged and never designated; the dev daemon
  sets it to the NixOS guest's archive whenever
  `msks-build-guest nixos` has built one. A rebuilt guest is
  therefore what a bare-name create boots, without a manual
  re-import.
- Listing shows every registered image with its hash, name,
  version, kernel facts, which one is default, and when each
  entered the catalog (the `imported` field, ISO 8601 UTC). A
  renamed row additionally carries its origin pair
  (`origin_name`/`origin_version`, equal to `name`/`version`
  for an unrenamed row):

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

# bare name — resolves to the newest registered version; the
# most recently imported row wins when two builds carry the same
# leading numbers (e.g. a NixOS version's trailing store hash
# differs between builds)
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
  workspace's identity; `user_data` is accepted only at
  create. It is capped at 64 Ki characters (pydantic's 422
  names the bound), an empty payload is a 400, and any
  mutation attempt answers a named 405 (delete and recreate to
  change it). cloud-init keys its
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
- **The host's timezone rides the seed (#633).** A create on a
  host with a timezone other than UTC captures it, and the seed's
  script sets the guest's clock to it on first boot — `date` and
  logs inside the guest read the operator's wall clock with no
  operator action. The capture is create-time and immutable like
  the rest of the seed: a host timezone change later leaves
  existing workspaces on the zone their seed baked, and only new
  creates see the new zone. A host on UTC (or whose clock file
  reads as nothing recognizable) seeds no timezone — the guest
  keeps the image's own default.
- **A seedless boot needs a quiet create.** A workspace created
  without a payload, a key, mounts, or a timezone to carry boots
  with two disks and no added cost: cloud-init finds no seed,
  applies nothing, and the interactive budget is unchanged (~3.0s
  start→shell).
- **Failure posture is cloud-init's.** The console getty
  starts before cloud-init runs, so the interactive budget is
  unaffected (the shipped image measures ~3.0s
  start-to-shell). Payloads run in cloud-init's final stage:
  a slow or hanging payload delays boot-complete, not the
  shell. `cloud-init status --wait` (or the
  serial log) says when provisioning finished; a factory reset
  re-runs the payload from the same seed.

## The published image channel

Every release publishes the built guest images to a
hash-keyed channel on this repository's GitHub releases. Each
image lives on its own release tagged
`images/<hash-prefix>`, as a zstd-compressed archive
(`<flavor>.tar.zst`) whose sha256 the catalog records.
Importing the compressed form hashes exactly the shipped
bytes, and the daemon's import accepts the form directly (the
archive reader detects it). An unchanged
derivation publishes nothing: the workflow compares the archive
derivation's evaluation against the previous manifest and skips
the build entirely, and a channel slot that already exists is
never rewritten.

The version release carries `images.json`, the manifest that
names every published image — `ref`, `description`, `kernel`,
`size`, `hash`, `url`, and the source facts (`build`,
`built_from`) — so fetching a published image is one command:

```bash
# the URL from the manifest row
msks image import \
  https://github.com/mcdonc/msks/releases/download/images/<hash>/debian.tar.zst
```

The description the listing and `image info` print is baked into
the image itself: the build writes one line into the layer's
`image.json` naming the distro, kernel, and shipped stack, and it
rides the import into any catalog. `msks image ls` carries it
(capped for the row), `msks image info` prints it at full length,
and the manifest rows carry it for the release channel.
