# Host share mounts

A workspace can mount host directory trees into its guest. The
host side is the operator's decision: the daemon's config names
the trees a guest may see, as _shares_. The guest side is the
requester's: each workspace names a share, the absolute guest
path it lands at, and a mode — `ro` or `rw`, defaulting to `rw`.

```bash
# the operator names the shares (msksd.yaml; reloadable on SIGHUP)
mount_shares:
  - name: projects
    path: /srv/projects
  - name: refs
    path: /srv/refs
    writable: false      # read-only in every guest
  - name: state
    path: /srv/state
    multi: false         # rw attaches exclusive, one workspace at a time

# a workspace mounts them (applies at its next start)
msks create ws1 --mount projects:/mnt/projects
msks mount ws1 refs:/mnt/refs:ro
msks umount ws1 refs
```

## What a share is

The config's `mount_shares` list is the complete set of trees any
guest can see. Listing a share is the trust decision: a listed
share serves read-write to any number of workspaces by default,
and the two bounds narrow that. `writable: false` serves the
tree read-only in every guest — an `rw` mount on it is refused
with the mismatch named, and `ro` on a writable share is a valid
self-restriction. `multi: false` makes read-write attaches
exclusive: one workspace holds the share at a time, and a second
read-write attach skips with a conflict that names the holder.
Read-only attaches are never exclusive — any number sit beside
one read-write holder.

Shares reload on `SIGHUP`: each workspace's next start reads the
shares the reload left. A share the reload removed reads as a
skip at start (below), and a share narrowed to read-only refuses
the workspace's `rw` mount the same way. Running VMs keep the
devices they attached.

## What a mount is

A workspace's mount names a share, an absolute guest path, and a
mode. The guest path becomes a root-owned mount point inside the
VM: absolute, below the filesystem root, outside the
pseudo-filesystems (`/proc`, `/sys`, `/dev`, `/run`) and outside
`/home` (the home volume owns it — see the [storage
chapter](storage.md)). Two mounts of one workspace never overlap,
and a share appears once per workspace. Mounts follow next-start
semantics, like resize: setting them never touches a running VM,
and the next start attaches the current set — the seed that boot
carries bakes exactly these mounts, so a restart after a change
lands the new set and prunes the dropped one. Two mounts set at
once from two terminals race last-write-wins — the surface sets
the whole list; a workspace served by one operator at a time
never sees the race.

## The skip behavior

A start proceeds when a mount cannot attach, and the skip is
state, not a log line. A share gone from the config, an `rw`
mount a reload narrowed its share under, an `rw` attach whose
exclusive share another workspace holds, or a share path that
moved since the daemon loaded its config — each publishes an
event (`mount.skipped`, `mount.denied`, `mount.conflict`,
`mount.drift`) naming
the mount and the reason, and the row carries the standing
marker (`msks ls` prints `running, 1 mount skipped`) until the
config and the row agree again. The workspace's other mounts
attach.

## Coherence between guests

A `multi: true` share (the default) attaches read-write to any
number of workspaces at once, and every attach forces
`cache=never`: data writes become promptly coherent through the
one host page cache, and lockfile coordination (`O_EXCL` creates
— git, cargo) works because the host kernel serializes it. Two
things do not coordinate between guests on the same share:
`flock`/`fcntl` locks (each attach is its own virtiofsd), and
`mmap` coherence. `multi: false` sidesteps the class the other
way — the exclusive holder is the only writer.

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
every write (#657).
