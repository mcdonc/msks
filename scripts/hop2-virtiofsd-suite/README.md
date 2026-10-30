# Two-hop virtiofs validation rig (#670)

Issue #670 de-risks hop 2 of the #667 share design: the msksd VM
running `virtiofsd` to export a VZ virtiofs mount (hop 1) into a
workspace VM booted under cloud-hypervisor (hop 2). NFS-serving a
virtiofs mount is known broken and no product ships this stack, so
this suite builds the actual two-hop rig and drives the seven
workloads the issue names.

## Verdict

**Hop 2 works on stock Debian.** All seven workloads pass. The
decision recorded on #667: no daemon-side mitigations are required
beyond what #667 already specifies — cloud-hypervisor's userspace
vhost-user master makes hop 2 possible on the stock cloud kernel,
virtiofsd 1.13.2 satisfies the snapshot gate, and inode identity
passes through hop 2 verbatim.

Evidence (Debian 13 trixie, kernel `6.12.111+deb13-cloud-arm64`,
virtiofsd 1.13.2-1+deb13u1, cloud-hypervisor v54.0, macOS 27.0.1 on
an Apple M3, outer VM from `lima-template.yaml` beside this README):

| workload                | load                                                                                                     | result                                                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| listing                 | 100×100 tree (10k entries, spaces + unicode names)                                                       | 10,101 entries byte-identical across the hop, in 1s                                                                                                                     |
| hardlink churn          | Docker's `linkloop` reproducer, 60s                                                                      | thousands of link/unlink iterations, zero `link()` retries, zero `stat()` ENOENT                                                                                        |
| inode stability         | create/link/unlink cycle, both hops                                                                      | inode stable across link and unlink every time                                                                                                                          |
| identity                | `stat` comparison, files created on macOS and in the outer VM                                            | hop-1 (VZ-synthetic) inodes pass through hop 2 verbatim; `st_dev` differs per hop                                                                                       |
| uid/gid                 | file created by the macOS user (uid 501), daemon and guest as root                                       | macOS 501 → outer 0 → guest 0: VZ maps the host user to root, the daemon runs as root, and the guest matches it — virtiofsd's match-the-guest-UID guidance is satisfied |
| fd pressure             | 40,000-file tree built and fully walked by the guest                                                     | clean; virtiofsd held **6 fds** the whole time (RLIMIT_NOFILE 1,000,000)                                                                                                |
| crash consistency       | 20,000-file writer (write tmp + rename), `kill -9` of virtiofsd **and** the VMM mid-write, rig restarted | 1,825 files had landed; **all complete**, zero partial entries, share remounted clean                                                                                   |
| snapshot gate           | virtiofsd version                                                                                        | 1.13.2 ≥ 1.11 — snapshot/migration capable                                                                                                                              |
| cross-writer visibility | ping/pong files, 60 rounds, idle and under a sustained guest-side find storm                             | min 10ms, median 26ms, **max 0.87s**, no spike over 1s in any round                                                                                                     |

The fd number deserves its own line: the one-O_PATH-fd-per-inode
model the issue worried about (microvm.nix #472) does not manifest
with virtiofsd 1.13.2's default `--cache auto` — the daemon opens
per operation and holds nothing per inode, so node_modules-scale
trees do not approach any fd ceiling.

## Findings

1. **cloud-hypervisor is what makes hop 2 possible on the stock
   kernel.** Debian's cloud kernel builds no `CONFIG_VHOST_USER_FS`
   (no `/dev/vhost-vfs`), so a QEMU/libvirt-shaped hop 2 would need a
   custom kernel. cloud-hypervisor implements the vhost-user master
   in userspace and needs no kernel vhost device on the VMM side; the
   guest side needs only `CONFIG_VIRTIO_FS=y`, which the cloud
   kernel builds in. #667's choice of cloud-hypervisor is load-bearing.
2. **Inode identity composes.** Hop 2 passes hop-1 inode numbers
   through verbatim (355276 on the outer mount, 355276 in the guest,
   for a file created on macOS). Device numbers differ per hop
   (st_dev 51 vs 52), so inode comparisons stay valid only within one
   hop — the virtiofsd README limitation, confirmed and bounded.
3. **Ownership collapses to root by default.** The macOS user's uid
   501 appears as 0 on the outer VM's hop-1 mount (VZ's mapping), the
   daemon runs as root, and the guest sees 0. Nothing maps uids back;
   #667's single-writer contract is the operative safety story.
4. **Crash behavior is clean.** Killing virtiofsd and the VMM
   mid-write leaves every landed file complete (the write-tmp+rename
   pattern is atomic through both hops) and the share remounts without
   residue after the restart.
5. **Cross-writer visibility is sub-second — when completion is
   signaled by a marker written after the data.** The control channel
   below was rewritten to a marker protocol after early versions
   (read the answer file directly) produced phantom multi-minute
   "wedges": a file created empty at command start, then filled,
   can be read empty by a concurrent poller through the hop-1 dentry
   cache. Marker-last is also the pattern msks workspace tooling
   should use for cross-VM signaling on a share.

## The rig

    macOS host ──VZ virtiofs──> outer Lima VM ──virtiofsd (vhost-user)──> cloud-hypervisor guest

- Outer VM: `lima-template.yaml` (the #678 rig's template, 6 GiB RAM,
  20 GiB disk, `~/hop2` mounted rw — the share under test).
- Guest: the same Debian genericcloud image and kernel the outer VM
  boots (direct kernel boot), seeded with a driver via `seed-guest.sh`
  (qemu-nbd attach, systemd unit, no cloud-init, no network).
- cloud-hypervisor v54.0 static binary; virtiofsd from Debian trixie.

## Running

On the Mac (once):

    mkdir -p ~/hop2
    limactl create --name msks-hop2 lima-template.yaml
    limactl start msks-hop2
    limactl cp msks-hop2:/dev/null /dev/null 2>/dev/null || true  # warm ssh
    echo made-by-macos-user > ~/hop2/share/identity/hostuser.txt   # optional uid probe

Copy the suite in and run it (the suite is destructive to
`~/hop2/share`; nothing else):

    limactl cp -r scripts/hop2-virtiofsd-suite msks-hop2:/root/
    limactl shell msks-hop2
    % sudo bash /root/hop2-virtiofsd-suite/bootstrap.sh --fresh
    % sudo bash /root/hop2-virtiofsd-suite/up.sh
    % sudo bash /root/hop2-virtiofsd-suite/run-suite.sh   # 8/8 PASS expected
    % sudo bash /root/hop2-virtiofsd-suite/coherence.sh 30
    % sudo bash /root/hop2-virtiofsd-suite/down.sh

`up.sh` leaves the rig running; `run-suite.sh` ends it (its crash
phase kills and restarts the rig once). `logs/` under
`/var/lib/hop2` keeps every phase's output, the guest serial console,
and virtiofsd/cloud-hypervisor logs; the guest also logs to
`/root/driver.local.log` on its own disk.

## The control channel

The suite drives the guest through the share itself: the outer side
publishes `cmd` (`<seq> <command>`), the guest driver answers into
`out.<seq>` and writes `end.<seq>` last, and the outer side polls
only `end.<seq>` — a marker that exists only after its data does.
Files are published by write-to-temp + rename. Anything that polls a
data file for existence through this stack can read it empty; see
finding 5.
