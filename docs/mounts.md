# Host share mounts

A workspace can mount host directory trees into its guest. The
host side is the operator's decision: the daemon's config names
the allowed roots a guest's mounts must sit under, and (on a
VM-hosted daemon) the trees its own VM carries. The guest side is
the requester's: each workspace names a host path, the absolute
guest path it lands at, and a mode — `ro` or `rw`, defaulting to
`rw`.

```bash
# the operator allows the roots (msksd.yaml; reloadable on SIGHUP)
mount_host_roots:
  - /srv/projects
  - /srv/refs

# a workspace mounts host paths (applies at its next start)
msks create ws1 --share /srv/projects:/mnt/projects
msks mount ws1 /srv/refs:/mnt/refs:ro
msks umount ws1 /srv/refs
```

Shares by name — a `mount_shares` config list and mounts that
referenced one — were removed: host paths are the one surface, and
a config that still names the key fails at load with the removal
message.

## What a mount is

A workspace's mount names a host path, an absolute guest path, and
a mode. The guest path becomes a root-owned mount point inside the
VM: absolute, below the filesystem root, outside the
pseudo-filesystems (`/proc`, `/sys`, `/dev`, `/run`) and outside
`/home` (the home volume owns it — see the [storage
chapter](storage.md)). Two mounts of one workspace never overlap,
and a host path appears once per workspace. Mounts follow
next-start semantics, like resize: setting them never touches a
running VM, and the next start attaches the current set — the seed
that boot carries bakes exactly these mounts, so a restart after a
change lands the new set and prunes the dropped one. Two mounts set
at once from two terminals race last-write-wins — the surface sets
the whole list; a workspace served by one operator at a time never
sees the race.

## The two gates

Each host path runs two gates at set time, each refusing with its
remedy:

1. **The mount gate** (VM-hosted daemons, `mount_host_mode:
prefix`): the path, translated under `/mnt/msks`, must sit
   under a root the daemon's VM actually has mounted. A path
   whose root is not mounted is refused with the instruction to
   recreate the VM from a template that mounts it — vz mounts
   are fixed at VM start, so a config edit cannot add one.
2. **The policy gate** (every host): the path, resolved against
   symlink escapes, must sit under one of the allowed roots
   (`mount_host_roots`). A path outside them is refused with the
   instruction to add it or a parent to the daemon's config. A
   symlink that resolves outside its allowed root is refused
   the same way — the share cannot walk out of what the
   operator allowed.

The spec grammar splits its fields on colons, so a host path
containing one is inexpressible on the wire (`--share` and the
TUI editor both refuse it, naming the field the colon broke).

A mount attaches read-write unless it says `ro`: the trust
decision is made before the mode matters (the root is allowed),
and a mount that wants less names `ro`. Read-write mounts hold no
exclusivity: any number of workspaces may attach the same tree,
and the coherence contract (below) is their concurrency story.

## The skip behavior

A start proceeds when a mount cannot attach, and the skip is
state, not a log line. A root the reload removed from the allowed
list, a path whose root the daemon's VM no longer mounts, a
symlink that moved out from under its allowed root, or a path
that vanished since the set — each publishes an event
(`mount.skipped`) naming the mount and the reason, and the row
carries the standing marker (`msks ls` prints `running, 1 mount
skipped`) until the config and the row agree again. The
workspace's other mounts attach.

## Coherence between guests

A host tree attaches read-write to any number of workspaces at
once, and every attach forces `cache=never`: data writes become
promptly coherent through the one host page cache, and lockfile
coordination (`O_EXCL` creates — git, cargo) works because the
host kernel serializes it. Two things do not coordinate between
guests on the same share: `flock`/`fcntl` locks (each attach is
its own virtiofsd), and `mmap` coherence.

## Shares on a macOS host

On a Mac the daemon runs inside its own Linux VM (Lima over
Apple's Virtualization.framework, on an M3-or-newer Mac), and a
shared Mac folder crosses two hops to reach a workspace: vz
virtiofs carries it from macOS into the daemon's VM under the
reserved prefix (the Mac's `/Users/chrism` at
`/mnt/msks/Users/chrism`), and one virtiofsd per attached share
carries it from there into the workspace guest. The client
speaks Mac paths throughout — `--share ~/code:/mnt/code` — and
the daemon's `prefix` host mode does the translation before
the [two gates](#the-two-gates) run. The Lima template mounts
the invoking user's home under the prefix at VM creation,
writable, so the common case needs no edit; a share root
outside it (another user's home, a `/Volumes` tree) is a
template edit plus VM recreation, and the mount gate's refusal
names that remedy. A symlink stored with an absolute Mac path
dangles past the Mac — under the prefix the tree lives at
`/mnt/msks/...`, so the link's absolute target resolves against
the daemon VM's own root, and against the workspace's own root
one hop later — while relative symlinks cross intact.
Hardlink-heavy workloads run clean on the stack's floor (macOS
15, where Lima enables the nested virtualization workspaces
need): `git repack`, `cp -al` storms, and Docker's own inode
reproducer all pass there — the inode instability older macOS
carried was fixed at the vendor.

The far end of the share is the Mac's own volume, and its
behaviors cross with the tree. Four of them are contract, not
configuration:

### Watchers poll

File events do not cross virtiofs. Changes made on the Mac — an
editor's save, a build's output — reach every attached
workspace as data, promptly coherent the way [coherence
between guests](#coherence-between-guests) describes, but no
inotify event follows them: the share carries data, not
notifications, in either direction. Watchers on shared trees
run in polling mode — `vite dev --poll`, `jest --watchAll`,
or the tool's own poll setting — and a tool that can watch
only through events cannot watch a share at all. Signaling
built on the same polling takes one care: a file's existence
becomes visible before its content does (a writer creates
empty, then fills), so a poller waits for a completion marker
the writer emits after the data, never for the file itself.

### Case and normalization follow the Mac volume

The tree a workspace mounts is the Mac's volume, and its naming
rules cross with it: the volume matches names case-insensitively,
storing them as typed (case-preserving), and compares them
Unicode-normalization-insensitively — a name typed
composed (NFC) and the same name decomposed (NFD) are one file.
Tools inside the workspace that expect Linux naming can trip:
two names differing only in case cannot coexist in one
directory (a checkout holding both `README` and `readme`
collapses them), and a tool comparing filename bytes sees NFD
where the Mac accepted NFC — archive and package tooling that
hashes or compares names byte-for-byte can report a mismatch
against a Linux-built expectation. A tree that must keep Linux
naming rules exactly lives in the workspace's own storage (the
[storage chapter](storage.md)) instead.

### The rw single-writer contract

An `rw` share names the workspace its writer. Locks taken
inside the workspace hold there and stop there: `flock` and
`fcntl` locks end at the workspace's own virtiofsd, and the Mac
side honors none of them — Finder, an open editor, or a
Mac-side build writes past any lock the workspace took.
Between workspaces, [coherence between
guests](#coherence-between-guests) governs: lockfile creates
(`O_EXCL` — git, cargo) serialize through the host kernel and
work cross-workspace; `flock`/`fcntl` do not cross an attach.
Trees whose tools lock and treat the lock as the whole story —
a cargo or bazel cache, a SQLite database, anything with a
journal on disk — take one writer at a time: the workspace's,
or the Mac's. A Mac that only reads the share (browsing in
Finder, opening a file) rides the coherence contract; a
Mac-side writer into a lock-using tree races the workspace.

### External volumes stay busy while shared

A file the stack holds open keeps its Mac volume mounted, so a
disk whose tree is shared into a running workspace refuses to
eject — the Finder reports the volume in use — until the
workspace stops and its files close. `msks stop` on the
workspace, eject the disk, and the next `msks start` brings the
share back with it; sharing the volume at all was the template
edit above, since a `/Volumes/...` root sits outside the
default home mount.

## The plumbing

Each attached share is one virtiofsd process (the platform's own
`virtiofsd` package; the default binary name resolves on PATH
first and then the package's install locations off PATH, so the
daemon finds it without configuration — `MSKSD_VIRTIOFSD` names
a different binary), spawned before the VM and stopped with it.
The mounts need virtiofsd **1.13.0 or newer**: the identity
translation and the read-only enforcement both landed upstream
there, and an older binary refuses the daemon's arguments at
start (`msksd doctor` names the gap — Linux Mint 22's archive
ships 1.10.0, so its hosts build a newer virtiofsd and point
`MSKSD_VIRTIOFSD` at it). The resolution runs at settings load
and again on SIGHUP: a binary installed after the daemon started
is one reload away.
File ownership translates in both directions. Writes from the
guest land on the host as the daemon's own user, whatever guest
account made them — host-side ownership stays advisory, as with
a typical bind mount. Files read back inside the guest present
as the workspace's own user: the login user the workspace names,
or the image's built-in `msks` account when it names none — so
the share's tree reads as the logged-in user's files, and a
file the guest creates stays writable by the account that
created it. An edge sits outside that rule: a login user the
operator's `user_data` premade at another uid presents as the
first uid the seed provisions (1001 on a pristine image), not
the premade account's own — and the host's own permission bits
still govern every write: a file that presents as the workspace
user's still refuses what its host-side mode refuses. The
guest mounts
through systemd units the workspace's seed writes — one per
mount, named for the guest path, re-baked at each start — so a
reboot remounts without cloud-init running again.

Extended attributes remap under one host prefix (`user.virtiofs.`),
so a guest cannot write host security labels or root-owned
namespaces through the share. One name the map denies outright
and leaves unmapped: `security.capability`. Denied, the boundary
does not depend on the daemon's privilege — a daemon running as
root keeps the capability that would let it honor a guest's
file-capability writes, and the map is what says no. Left
unmapped, the host kernel clears file capabilities on write
itself — a remapped name would hand that job to virtiofsd, whose
clearing path re-checks the file's mode even through a file
descriptor opened writable at creation, so a program that
creates a file without write bits and keeps writing through the
open descriptor (git's loose-object files) would get EACCES on
every write (#657). File capabilities cannot ride on share files
at all: a name an earlier version's map stored as
`user.virtiofs.security.capability` still lists in the guest as
`security.capability`, reads as absent, and clears only from the
host side.
