# Workspace images

A workspace image is one file: a **container-image tar** — the layout
`podman load` and `skopeo` already understand — carrying a kernel, an
initrd, a rootfs disk, and a manifest that describes all three. This
chapter covers the image contract, how to build one, how to register
it with a running daemon, and how workspaces select an image.

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

| Field              | Meaning                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------- |
| `schema`           | `2`                                                                                      |
| `name`             | Catalog name, e.g. `debian`                                                              |
| `version`          | Catalog version, e.g. `13.6`; numeric segments sort correctly                            |
| `cmdline`          | Kernel command line for workspace boots                                                  |
| `vsock_shell_port` | AF_VSOCK port the guest's console service listens on                                     |
| `kernel_version`   | e.g. `6.12.107+deb13-amd64` (informational)                                              |
| `kernel_format`    | `bzImage` (informational)                                                                |
| `console_protocol` | Console handshake: `prelude-v1` (identity prelude) or `legacy` (raw root shell; default) |
| `console_users`    | Users `msks console --user` may request; default `["root"]`                              |
| `capabilities`     | Optional capability object; `provisioner` names the seed consumer (below)                |

The manifest is self-describing: importing the archive needs nothing
beside the archive itself.

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
  still gates which users `msks console --user` may request.
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
devenv tasks run msks:build-guest
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

The output lands under `.guest/`; `scripts/build-guest.sh` and
`nix/guest-assets.nix` document every step and are the reference for
what an image build does.

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

The `source` path is a path on the **daemon's** filesystem (the
appliance reaches host files through its virtiofs share). The daemon
copies it privately and hashes that copy, so a source file changing
underneath the import cannot desync the recorded hash from the
imported content, and importing the same content twice is idempotent.

Rules worth knowing:

- The **first image imported becomes the default** — the one a bare
  workspace create uses — and keeps the designation while more
  images arrive; re-designating the default is future API work.
- `MSKSD_DEFAULT_IMAGE` (set by the appliance from its built-in
  image) imports and designates at first boot; on later boots the
  daemon only re-checks the hash, not a full re-import.
- Listing shows every registered image with its hash, name, version,
  kernel facts, and which one is default:

```bash
curl -H "authorization: Bearer $TOKEN" \
  https://192.168.77.2:8660/api/v1/images
```

- Storage: one image costs roughly twice its rootfs size on the
  state disk (the retained archive plus the unpacked boot cache).
  The appliance's state disk is sized for two images.

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
- **The k8s backend does not serve `user_data` yet** — the runner pod
  does not build seed disks; create refuses the combination by name
  (the same shape as its egress refusal).
