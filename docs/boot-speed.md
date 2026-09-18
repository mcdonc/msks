# Workspace boot speed

From "start this workspace" to "an interactive shell" in under five
seconds is the performance contract (#37); this chapter documents
how the time is spent, how to measure it, and what a workspace image
must do — and must avoid — to keep boots fast. Every number below is
measured on the reference host (bare-metal KVM) with the shipped
Debian image.

## Measuring

```bash
devenv tasks run msks:build-guest   # the image under test
python scripts/perf-boot.py --runs 5
```

The harness boots real workspaces through the local backend and
reports, per run and as a p50:

| Metric      | Meaning                                                         |
| ----------- | --------------------------------------------------------------- |
| `t_vmm`     | VMM spawn + VM create + boot accepted                           |
| `t_kernel`  | first serial output — the kernel is decompressed and printing   |
| `t_console` | the vsock console handshake completes (the service answers)     |
| `t_prompt`  | the shell rendered its first prompt — **the readiness number**  |
| `t_login`   | the serial getty prompt (the last unit of the boot, diagnostic) |

It also reports the host-side cost of the running workspace: the
VMM's _peak_ resident set (`VmHWM`) after first boot, next to the
guest memory the workspace was configured with. A fresh Debian
workspace measures **165–185 MiB of VMM peak RSS against a 1024 MiB
guest**: cloud-hypervisor maps guest memory on demand, so an idle
workspace costs the host only what the guest actually touched.

The console and the login prompt are measured concurrently on
purpose: the vsock console is the readiness path and answers long
before the boot's last unit renders the serial prompt.

## Where the time goes

The shipped image, measured start→prompt:

| Stage                                   | t             |
| --------------------------------------- | ------------- |
| VMM spawn + boot accepted               | ~0.15s        |
| Kernel decompress + first output        | ~0.3s         |
| Minimal initramfs (six modules + mount) | ~0.05s        |
| systemd to the console service          | ~2.5s         |
| **Start → interactive shell**           | **~3.1s p50** |

The dominant remaining cost is systemd bring-up — device manager
coldplug (`dev-vda.device` is the slowest single unit at ~0.8s) and
the standard mount units. Everything before PID 1 is now tens of
milliseconds; it used to be the single largest lever (see below).

## What keeps it fast

Three properties of the shipped image carry most of the win. An
image that regresses any of them pays for it at every boot.

### A pinned kernel with a minimal path to root

Debian's **generic kernel** flavor (`linux-image-*-amd64`, pinned
by pool URL and hash in `nix/guest-assets.nix`) serves both the
workspace guest and the appliance (#96 — one pin, one fetch). The
property that matters is not built-ins but this: nothing sits
between the kernel and the root mount. Debian's stock initramfs
for the flavor is a 34 MiB `MODULES=most` archive that cost ~2.5s
to unpack and probe before systemd's first line; msks replaces it
with its own six-module initramfs and mounts root directly (the
cloud flavor #37 chose dodged the same archive by building ext4
in — the flavor was never the cost, the general-purpose initramfs
was). The measured cost of the swap is in the #96 section below.

### A minimal initramfs

The msks-built initramfs (`minimalInitrd` in `nix/guest-assets.nix`)
is a static busybox, the six modules the generic kernel needs to
mount the ext4 root (crc16, crc32c_generic, mbcache, jbd2, ext4,
virtio_blk — dependency order, because busybox `insmod` resolves
no dependencies), and an `/init` that mounts `/dev/vda`
read-write (#14: the per-workspace overlay carries the writes) and
`switch_root`s into systemd — ~1.4 MiB shipped, unpacked
and done in tens of milliseconds.

The runtime module tree is equally closed over: the guest's tree
is the `modprobe --show-depends` closure of the modules its
runtime loads (vsock console, virtio_net, virtio_blk, the ACPI
button pair, isofs for the seed disk), asserted at build time.

When swapping the kernel or module tree: the Debian deb ships its
modules **without depmod metadata** (its package postinst generates
it on the target). The build runs `depmod` itself; a tree without
`modules.dep` boots looking healthy while every `modprobe` — the
vsock console's module load included — silently fails.

### A console that starts before the boot finishes

`msks-console.service` sets `DefaultDependencies=no` and orders
only after the module load, so it listens as soon as the vsock
device exists instead of waiting for the full target chain. A start
that lands too early self-heals (`Restart=always`, short
`RestartSec`) — with `StartLimitIntervalSec=0`, because a fast-fail
loop would otherwise exhaust systemd's default burst limit and end
the retries permanently.

The same principle applies to anything a workspace image wants on
the critical path: order it after exactly what it needs, not after
the boot.

### The unit diet

Units a workspace never uses are absent from the boot: AppArmor
profile loading (~0.7s), `systemd-timesyncd`, `grub-common`,
`unattended-upgrades`, `e2scrub_reap`. (networkd and resolved run —
egress workspaces (#52) need the NIC configured; see
`nix/guest-assets.nix`.) The wants symlinks are removed at image
build time, with the reason each removal is safe recorded there.

## The generic-kernel unification (#96)

The workspace guest moved from Debian's cloud flavor to the
**generic** flavor the appliance boots, measured on the reference
host with `scripts/perf-boot.py --runs 5` (boot to first prompt,
p50) and its guest-memory probe (MemTotal − MemAvailable at the
first interactive prompt):

| metric                          | cloud flavor                | generic flavor  |
| ------------------------------- | --------------------------- | --------------- |
| boot p50 (start→prompt)         | 2.95–3.26 s¹                | 3.24 s          |
| t_kernel (to first serial byte) | 0.44 s                      | 0.48 s          |
| guest memory at first prompt    | 143.1 MiB                   | 134–142 MiB     |
| workspace archive (`xz -6 -T0`) | 147.8 MiB                   | 128.7 MiB       |
| host nix fetches                | two kernel debs (135.8 MiB) | one (102.9 MiB) |

¹ two sessions on the same host; the spread is host noise, not
kernel — the six-module initrd's own cost sits inside it. The
kernel-side delta is decompressing a 0.4 MiB-bigger vmlinuz
(t_kernel +40 ms). The appliance artifact set is untouched
(206 MiB rootfs.xz + 11.6 MiB vmlinux + 1.4 MiB initrd), and a
bundle shipping both images shrinks by the archive delta
(~19 MiB compressed).

## The appliance

The appliance's readiness number is boot-to-API: from
`devenv tasks run msks:appliance-up` to the first 200 from
`GET /api/v1/health`
on `https://192.168.77.2:8660` — measured with
`scripts/perf-appliance.py` (`--runs 4 --fresh`), which also
separates the first boot against a fresh state disk (image import,
token generation) from the warm boots an operator's restart pays.

```bash
python scripts/perf-appliance.py --runs 4 --fresh
```

On the reference host, the trixie-based appliance (#92) measures a
**warm p50 of ~27.5s** against the busybox-init image's ~25.2s: the
~2.3s delta is systemd's bring-up (device coldplug, journald, the
unit graph) plus the generic kernel's module set, and it buys
service supervision, journald on the state disk, and the ACPI power
button — while the dominant ~25s (msksd's Python closure resolving
over the virtiofs store share, cold in the guest's page cache every
boot) is unchanged between the two images. A cold first boot lands
at ~29s against the old image's ~47s (the default-image import into
the fresh state disk dominates that path; the old number was
measured against fully cold host caches).

What keeps the appliance boot honest: the same minimal-path-to-root
rule as the workspace (the generic kernel builds
virtio-pci and virtiofs in; the initramfs carries the six modules
it lacks), cloud-init disabled (the kernel cmdline is the config
channel), and the diet masks in `nix/appliance-image.nix` keeping
AppArmor, unattended-upgrades, and the rest out of the critical
path. The numbers above were measured against freshly built
artifacts; a fully warm host page cache drops the warm boot to
~15s — the dominant ~25s is msksd's closure reading cold off the
virtiofs share on the first boots after a build.
