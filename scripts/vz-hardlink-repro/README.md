# VZ virtiofs hardlink reproducer (the #669 landmine)

These scripts reproduce — or refute — the Apple VZ virtiofs inode
instability that gates the macOS host-share design
([#667](https://github.com/mcdonc/msks/issues/667)). Run them on
an rw virtiofs share inside a vz-backed Lima VM before relying on
read-write sharing from a new macOS release.

## The landmine

Apple's Virtualization.framework virtiofs server assigns synthetic
inode numbers to shared files. Through macOS 14 (Sonoma), creating
a hardlink sometimes assigned a **new inode to the original file**:
the Linux guest's dentry cache went stale, `link()` failed
transiently with `ENOENT`, and `stat()`/`open()` returned `ENOENT`
for files that existed. Docker Desktop hit it as intermittent
`ld: cannot find ... No such file or directory` during Rust builds
(docker/for-mac#6511, docker/for-mac#7059) and coped by carrying a
private `fs/fuse` kernel patch in its VM ("[PATCH 9/9] hardlinks:
drop the cache of the existing entry", Feb 2024 — when the dentry's
inode has `nlink > 1`, force `entry_valid = 0` so the guest never
trusts a cached dentry for a hardlinked file). podman machine
users hit the same wall in [podman discussion 23886](https://github.com/podman-container-tools/podman/discussions/23886),
which carries no patch: podman ships the stock Fedora kernel.

That patch never mainlined: `fs/fuse/dir.c` in torvalds' tree and
in the 6.12 line (what the Lima VM's Debian kernel runs) sets no
such override.

## The decision (#669)

**The msks Lima VM keeps the stock kernel. No `fs/fuse` patch, no
mount-level workaround, no UTM switch.** Four grounds:

1. **Apple fixed the server before msks's macOS floor.** The
   podman reporter could not reproduce on macOS 15 (Sequoia), and
   the full suite below runs clean on macOS 27 (Tahoe successor)
   on an M3 host. The macOS-host mode needs nested virtualization
   to run workspace VMs inside the msksd VM, and Lima itself
   refuses `nestedVirtualization: true` on macOS < 15
   (`pkg/driver/vz/vm_darwin.go`), so every supported
   configuration runs a virtiofs server from the fixed era.
2. **No stock-kernel mount knob approximates the patch.** The
   revalidation timeouts (`entry_timeout`, `negative_timeout`,
   `attr_timeout`) are values the **server** chooses per FUSE
   lookup reply; the guest kernel parses none of them as virtiofs
   mount options, and `cache=` modes belong to virtiofsd exports
   (the hop-2 daemon), not to the VZ hop-1 mount. Verified live —
   a stock 6.12 kernel rejects all four:

   ```console
   $ mount -t virtiofs -o entry_timeout=0 lima-<tag> /mnt/knob
   mount: /mnt/knob: fsconfig() failed: virtiofs: Unknown parameter 'entry_timeout'.
   $ mount -t virtiofs -o negative_timeout=0 ...   # same rejection
   $ mount -t virtiofs -o attr_timeout=0 ...       # same rejection
   $ mount -t virtiofs -o cache=never ...          # same rejection
   ```

3. **Carrying the patch is an unbounded build commitment.** The
   msksd VM boots the stock Debian cloud kernel (today
   `6.12.111+deb13-cloud-arm64`); carrying the patch means building
   `linux-image` from Debian sources in CI and re-tracking every
   Debian kernel security update, to work around a bug the
   platform vendor already fixed below our floor.
4. **UTM trades the landmine for worse ones.** UTM's QEMU backend
   shares directories as SPICE WebDAV or 9p/VirtFS — virtiofsd
   needs Linux — and 9p is exactly the transport the msks Lima
   template bans (`mountTypesUnsupported: [9p]`). Its Apple
   backend runs the same VZ virtiofs server Lima's vz does, so the
   landmine follows the switch, without limactl-grade headless
   automation.

## The evidence (macOS 27.0.1, Apple M3, limactl 2.2.1, vz)

Guest: Debian 13 trixie, stock kernel `6.12.111+deb13-cloud-arm64`,
one rw virtiofs mount (`lima-… on /Users/chrism/vzrepro type
virtiofs (rw,relatime)`), created by a template mirroring the msks
course VM (same image digest, same backend, `writable: true`).

| workload                                   | load                                                                                         | result                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `linkloop` (Docker's reproducer, verbatim) | 208,576 iterations in 3 min + a 5 min soak                                                   | clean — no `link()` retry, no `stat()` ENOENT, no inode change |
| `inochange`                                | 20,000 create/link/unlink cycles                                                             | clean — inode stable across link and unlink every time         |
| `cpal` (`cp -al` storm)                    | 271 cycles × 2,000 links                                                                     | clean — 542,000 link(2) calls, inodes stable                   |
| `gitrepack`                                | 5 min loop of `git clone --local` + `repack -adf` + `gc --aggressive` on a 6,300-object repo | clean — no transient failures                                  |

The acceptance gate for #667: a git repack loop on an rw share with
no transient `link()`/`stat()` failures — met.

## Re-running

The suite is plain shell plus one C file, runnable from any macOS
host with limactl and a scratch directory it can share read-write:

```bash
mkdir -p ~/vzrepro
limactl create --name msks-vzrepro <template mounting ~/vzrepro rw, vmType: vz>
limactl start msks-vzrepro
limactl cp linkloop.c inochange.sh cpal.sh gitrepack.sh msks-vzrepro:~/vzrepro/
limactl shell msks-vzrepro
  % cd ~/vzrepro && gcc -O2 -o linkloop linkloop.c
  % mkdir -p work && cd work && touch a && mkdir b
  % timeout 180 ../linkloop          # prints dots; any FAIL line is the landmine
  % cd .. && ./inochange.sh 20000    # direct inode-stability probe
  % ./cpal.sh 5                      # dense link(2) storm, minutes
  % ./gitrepack.sh <path-to-src-repo> 5
```

Any workload failing — `link` retry loops, ENOENT on files that
exist, an inode changing across `ln` — reopens the landmine. The
escape hatch, documented above, is Docker Desktop's private
`fs/fuse` patch carried in a custom kernel for the msksd VM; weigh
that against the CI burden in ground 3 before building it.
