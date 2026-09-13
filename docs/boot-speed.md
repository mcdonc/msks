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

| Metric      | Meaning                                                        |
| ----------- | -------------------------------------------------------------- |
| `t_vmm`     | VMM spawn + VM create + boot accepted                          |
| `t_kernel`  | first serial output — the kernel is decompressed and printing  |
| `t_console` | the vsock console handshake completes (the service answers)   |
| `t_prompt`  | the shell rendered its first prompt — **the readiness number** |
| `t_login`   | the serial getty prompt (the last unit of the boot, diagnostic) |

It also reports the host-side cost of the running workspace: the
VMM's *peak* resident set (`VmHWM`) after first boot, next to the
guest memory the workspace was configured with. A fresh Debian
workspace measures **165–185 MiB of VMM peak RSS against a 1024 MiB
guest**: cloud-hypervisor maps guest memory on demand, so an idle
workspace costs the host only what the guest actually touched.

The console and the login prompt are measured concurrently on
purpose: the vsock console is the readiness path and answers long
before the boot's last unit renders the serial prompt.

## Where the time goes

The shipped image, measured start→prompt:

| Stage                              | t      |
| ---------------------------------- | ------ |
| VMM spawn + boot accepted          | ~0.15s |
| Kernel decompress + first output   | ~0.3s  |
| Minimal initramfs (module + mount) | ~0.05s |
| systemd to the console service     | ~2.5s  |
| **Start → interactive shell**      | **~3.1s p50** |

The dominant remaining cost is systemd bring-up — device manager
coldplug (`dev-vda.device` is the slowest single unit at ~0.8s) and
the standard mount units. Everything before PID 1 is now tens of
milliseconds; it used to be the single largest lever (see below).

## What keeps it fast

Three properties of the shipped image carry most of the win. An
image that regresses any of them pays for it at every boot.

### A kernel with the root disk built in

Debian's **cloud kernel** flavor (`linux-image-*-cloud-amd64`,
pinned by pool URL and hash in `nix/guest-assets.nix`) builds ext4
and virtio-pci into the kernel. The generic flavor ships both as
modules, which chains the boot to a general-purpose initramfs — a
34 MiB `MODULES=most` archive that cost ~2.5s to unpack and probe
before systemd's first line.

### A minimal initramfs

The msks-built initramfs (`minimalInitrd` in `nix/guest-assets.nix`)
is a static busybox, the one module the kernel cannot mount root
without (`virtio_blk.ko`), and an `/init` that mounts `/dev/vda`
read-only and `switch_root`s into systemd — 811 KiB shipped, unpacked
and done in tens of milliseconds.

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
profile loading (~0.7s), `systemd-networkd` and its socket,
`systemd-timesyncd`, `systemd-resolved`, `unattended-upgrades`,
`e2scrub_reap`. The wants symlinks are removed at image build time
in `nix/guest-assets.nix`, with the reason each removal is safe
recorded there.
