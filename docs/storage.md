# Workspace storage

Every workspace owns exactly two persistent artifacts, created with
the workspace and kept for its whole life:

- the **root overlay** — a qcow2 copy-on-write file over the
  workspace's base image. Everything the guest writes to the root
  filesystem (an `apt install`, an `/etc` edit) lands in the overlay;
  the shared base stays pristine, so every workspace on that image
  starts from an identical, unmodified root.
- the **/home volume** — an ext4 filesystem attached as a second
  virtio-blk disk the guest mounts at `/home`. User data lives here,
  outside the root, so it survives even a full factory reset.

Both artifacts have lifetimes separate from the VM's: `stop` powers
the machine off and keeps the data; `start` boots the same overlay
and volume again. Only `delete` (and factory reset, for the overlay
alone) removes them.

## Local backend layout

```text
<state_dir>/
├── vms/<workspace_id>/
│   ├── root.qcow2        # the root overlay (plus the VM's runtime files)
│   └── seed.img          # the #41 cidata seed (user_data workspaces only)
└── volumes/
    └── <workspace_id>.ext4   # the /home volume (sparse ext4)
```

The overlay is created with
`qemu-img create -f qcow2 -F raw -b <base rootfs>` — a one-level
chain, never nested overlays. Its
backing file is the rootfs recorded at workspace create, so a
workspace always runs the image version it was created with:
importing a newer image changes what _new_ workspaces boot, never
existing ones. (Rebasing an overlay onto a newer base exists as a
`qemu-img` operation and may become an explicit "upgrade workspace
image" action later; it never happens implicitly.)

The home volume is a sparse ext4 file: an idle volume costs its
metadata, not its nominal size, and grows as the guest writes.

The seed (#41) exists exactly when the workspace was created with
`user_data`: a kilobytes-small iso9660 image labeled `cidata`, built
with `mkisofs` (genisoimage), attached to the VM **read-only**, and
installed mode 0600 because the payload can embed tokens (its
staging directory is 0700, and the daemon creates its database
file — which records the payload — 0600). It rides
the workspace's vm directory, so it survives `stop`/`start` and
dies with the workspace. Factory reset keeps it — it is immutable
create-time input, so the reset workspace re-provisions from it —
and `docs/images.md` describes what runs the payload.

A start heals missing artifacts: if the overlay or the volume file
is absent (a crash mid-create, or a workspace row created before
this scheme existed), the daemon recreates it before the VM boots.
Data that exists is never touched. Creation is atomic per
artifact: each file is built under a private temporary name and
installed with one rename, and a failed create rolls back what it
made — a missing tool, ENOSPC, or a partial write leaves nothing
at the final path, so the retry starts from a clean pair instead
of booting a blank disk. A daemon crash mid-create can still leave
a finished artifact (or its `*.tmp` scratch) behind; the removal
helpers sweep the scratch files, and the next create names any
leftover instead of adopting it.

Workspace **create never reuses an existing artifact**: a file
found where a new workspace's overlay or volume would live (a
previous workspace of the same id whose cleanup could not remove
its files) fails the create with a named error instead of silently
adopting the predecessor's data. Clear the files and create again.

## Sizing

Both sizes are set at workspace create and configurable three
ways: per request (`root_mib` / `home_mib` on workspace create),
per daemon (`MSKSD_ROOT_MIB`, default 10240, and `MSKSD_HOME_MIB`,
default 2048), and per image (the catalog's manifest carries the
cmdline the guest boots, which pairs with the sizes). The overlay's
virtual size never drops below its base image's size — a smaller
disk would truncate the base filesystem.

Create is not the only time sizes move: `POST
/api/v1/workspaces/{id}/resize` (`msks resize`) revisits them on a
**stopped** workspace (#184):

- the **/home volume** grows or shrinks. The daemon quiets the
  filesystem's journal with `e2fsck -fy` — a volume a hard stop left
  dirty is corrected here, corrections being that tool's job, not an
  error — then `resize2fs` moves it and the file follows (a grow
  truncates up first; a shrink truncates down only after the
  filesystem agreed — `resize2fs` refuses to shrink below the blocks
  in use, and that refusal reaches the caller as a named `409`
  telling the operator to free data in the workspace or shrink
  less).
- the **root overlay** grows only, measured against the overlay's
  actual virtual size (create clamps it to the base image's size, so
  it can sit above the row) — its partition table and root
  filesystem belong to the guest, and the boot's cloud-init
  `growpart` fills the larger device on the next start for free;
  shrinking it is a factory reset, not a resize.

A resize carries the home-volume moves' guards (free lifecycle
statuses, the placement check, the move-lock against a concurrent
boot). The artifact files are the truth each side moves: the grow
gate reads the overlay's actual virtual size (create clamps it to
the base image, and a corrupt overlay — one qemu-img probes as raw
or empty — answers a named `503` instead of being truncated), and
the home move classifies grow-vs-shrink by the volume file, so a
volume imported above its row's size reconciles through the same
request. The row follows each side as it moves (`msks ls --json`
and `msks storage` report the new ceiling immediately, the bytes
move at once on the host), a workspace whose artifact file is
absent simply records the new size — the next start's artifact heal
builds the blank artifact at it — and a request naming sizes the
files already have answers an idempotent `200` that moves nothing. The k8s backend answers a named `400`: the workspace
lives on a claim the cluster sizes, so growth goes through the
storage class. A completed resize is announced on the events
channel (`workspace.resized`, with the new sizes).

## The appliance state disk

Inside the appliance, every workspace artifact above lives on one
ext4 state disk, beside the image catalog, the daemon database, the
TLS material, and the journal (bounded at 512M). What consumes it:

- one imported image costs roughly twice its rootfs size (the
  retained archive plus the unpacked boot cache) — about 3G for the
  Debian base the appliance ships with, and the catalog starts with
  that image already imported;
- each workspace's overlay grows with everything its guest writes to
  the root filesystem — an `apt install npm` writes well over 1G —
  and its `/home` volume grows with user data in the same way;
- the database, the tokens, and the journal are megabytes-scale.

The disk is a sparse file with a 40G ceiling: host disk is spent as
the guest writes, and an idle disk costs its content. When the
template grows across releases, an existing disk follows on its
next start — the host extends the file to the template's size, and
the appliance's state preparation grows the filesystem to match. A
disk already larger than the template keeps its size.

## Capacity reporting

`msks storage` answers the operator's three capacity questions,
each from its own source of truth (#184):

```text
$ msks storage
state disk    used 23.4G of 40G    free 16.6G    pressure ok

workspace                root cost/ceiling    home cost/ceiling    cost
ws4                      3.1G / 10G           812M / 2G            3.9G

image                    imported         cost
debian:13                2026-09-21 12:03 3G
debian:13                2026-08-02 05:11 3G
```

- **The budget line** is the daemon's own `statvfs` on the state
  disk — the number that predicts what a full state disk causes
  (guest-side I/O errors on every workspace). `pressure` names the
  condition: `warn` past `MSKSD_STORAGE_WARN_PCT` (default 90)
  percent used, `critical` at or below `MSKSD_STORAGE_FLOOR_MIB`
  (default 512) free, `ok` otherwise.
- **Cost** is the disk blocks an artifact occupies — the sparse
  files' real footprint, never their nominal size. An overlay's
  cost sits a little above the root filesystem usage the guest's
  own `df` reports (qcow2 bookkeeping rides along) and stays below
  its ceiling until the guest fills it.
- **Ceiling** is the size fixed at create (`root_mib` /
  `home_mib`) — the quota the guest's `df` shows its user.
- **Imported** is when the archive came in, local time to the
  minute (#186). Rebuilding an image and importing it again adds a
  second entry under the same reference; the time tells the
  entries apart, and the short hash `msks image ls` shows names
  the one `msks image rm` removes. Re-importing the same archive
  refreshes the time.

The fullness a workspace user feels is their own `df` inside the
workspace; the daemon never guesses at it from outside a running
VM (the ext4 journal lags the host's view of it).

The watcher probes the state disk every `MSKSD_EVENT_POLL_S` and
publishes a `storage.pressure` event on each change (a steady
condition announces once, with the daemon's first known pressure
announced as a baseline), with a named log line at `warn` and
`critical`. Below the floor, workspace creates, image imports, and
home-volume imports answer `507` naming the floor and the reclaim
path — `msks storage` names the consumers, `msks rm` and
`msks image rm` remove them, and lowering
`MSKSD_STORAGE_FLOOR_MIB` admits writes at less headroom if that
is the deployment's choice (raising the floor refuses more, not
fewer). Imports are sized when the size is knowable: an image
import must fit the floor plus twice the archive's bytes (the
retained copy plus its unpacked cache), and a home-volume import
must fit the floor plus the upload's `Content-Length` when the
client sent one — a chunked upload carries no length and gets the
floor alone. Existing workspaces keep running below
the floor; their own writes can still fill the disk, so a `warn`
line is the cue to reclaim before they do.

The report, the probe, and the floor serve the local backend: the
k8s backend keeps artifacts on per-workspace claims the cluster
places, and the daemon's own filesystem says nothing about them
(`GET /api/v1/storage` answers a named `400` there, following the
home-volume routes' precedent).

`GET /api/v1/storage` serves the same document the CLI renders,
and `msks storage <id>` narrows the workspace table to one
workspace.

## Factory reset

`POST /api/v1/workspaces/{id}/reset` deletes only the overlay. The
next start boots the pristine base image again — installed packages
and every other root change are gone — while `/home` keeps its data.
The VM is stopped first (the overlay is the running root device);
a wedged VM is killed, exactly as `delete` does.

Because the overlay is the root filesystem, reset also clears any
provisioned state the guest keeps there (cloud-init's run-once
markers live under `/var/lib/cloud`), so future provisioning
re-runs on the next boot.

## Home volume export and import

The home volume travels through the daemon's authenticated
listener as byte streams (#80) — the interim mechanism for backup,
migration to another daemon, and seeding a fresh workspace with
data:

```text
GET /api/v1/workspaces/{id}/home    → the volume file's bytes, streamed
PUT /api/v1/workspaces/{id}/home    → replaces the volume from the body
```

`msks home export` / `msks home import` drive both endpoints from
the CLI (`docs/cli.md`); a token holder may also call them with any
HTTP client.

The body is the volume file's bytes **verbatim** — an ext4 image,
labeled `msks-home` when msksd made it. An import installs what it
receives: a hand-made ext4 image (or one exported from another
workspace) lands exactly as uploaded. The ext-family magic sits
~1 KiB into the image, and the daemon checks it as the body's
first bytes arrive — a wrong file is refused with `400` at
kilobyte cost, and the workspace's existing volume is untouched.
The check cannot judge completeness: the daemon installs the bytes
that arrive, so verify a hand-made image (`e2fsck -n`) before
importing it; a transfer that dies mid-body is the disconnect
case and installs nothing.

Sparseness survives the round trip: the import writes in 1 MiB
windows and turns every all-zero window back into a sparse hole, so
a blank 2 GiB volume re-imports at its data's cost, not its
nominal size. A volume exported over a slow link composes with any
compressor (`msks home export ws - | gzip > ws.ext4.gz`) because
`-` streams the bytes to stdout.

Both endpoints answer `409` unless the workspace's row says its
VM is down — `created`, `stopped`, and `absent` move freely;
`starting`, `running`, and `paused` keep the volume (an export
under a guest mid-write is a torn image, and an import into a
mounted device would be overwritten or lost), and `unknown`
refuses too: the seam reports it when a live VMM stopped answering
its API, so a possibly-live VM gets the volume's protection. Stop
the workspace first (`msks stop`). Under the move's lock the
daemon also asks the seam itself: a row can lag a boot by one
watcher poll interval, and the live VMM's answer refuses where the
row would pass.

A move, a boot, and a delete serialize per workspace on one lock:
a boot that arrives during a move waits for it to finish and boots
the volume the move left, a move that arrives after a boot sees
the running row and answers `409`, and a delete orders against an
import instead of racing it. A waiter gives up after
`MSKSD_MOVE_WAIT_TIMEOUT_S` (120 s default) and answers a named
`409` (a volume move is in flight) rather than hanging — a stalled
export reader holds its lock as long as its connection lives. A
foreign host answers the placement `409` every artifact route
shares, and the k8s backend answers `400` — its volume lives
inside the runner pod's PVC, which only the pod's container
reaches.

An import whose filesystem size differs from the workspace's
recorded `home_mib` mounts fine either way (an ext4 filesystem
smaller than its device is legal); `home_mib` stays the size a
blank rebuilt volume gets, and growing an imported filesystem is a
guest-side `resize2fs`. The install is atomic — a failed or cut-off
upload leaves the existing volume in place — and every completed
move is announced on the events channel (`home.exported` /
`home.imported`, with the byte count; an export announces when its
stream reaches the end, so a cancelled download publishes
nothing).

## Placement and single-attach

The workspace row records the **host** that owns its artifacts. On
the local backend that is always the msksd host that created the
workspace; a start, stop, reset, or delete through a daemon on
another host fails with `409` naming where the artifacts live —
booting a workspace with an empty `/home`, marking a running VM
stopped, or dropping the row out from under a live VM are all
worse than the refusal. The host name is the daemon's
`MSKSD_HOST_NAME` (default: the host's hostname at daemon start) —
pin it explicitly when the hostname is not stable (laptops,
containers) so a rename does not strand every workspace. Deleting
a workspace whose artifacts live elsewhere happens from that host:
this one refuses rather than orphan the files. A workspace runs on
at most one host at a time — the row's placement is the authority
locally, and the volume's single-writer semantics make a double
attach impossible. The k8s backend records no host: the artifacts
live in a per-workspace claim the cluster places, so any daemon in
the cluster may run the workspace.

## Kubernetes backend

On the k8s backend both artifacts live on one per-workspace
`PersistentVolumeClaim`:

- created at workspace create (name `msks-ws-<workspace_id>`,
  access mode `ReadWriteOnce`, size from
  `MSKSD_K8S_WORKSPACE_STORAGE_GIB` — unset derives the claim from
  the workspace's `root_mib` + `home_mib`, rounded up to GiB — and
  storage class from `MSKSD_K8S_STORAGE_CLASS`, unset asks the
  cluster's default class). Recreating a workspace of the same id
  keeps the existing claim: a claim smaller than the new sizes
  fails the create with a named error instead of failing the guest
  with late ENOSPC;
- mounted into the runner pod at
  `/var/lib/msks/workspaces/<workspace_id>`, the container-side
  analogue of the local backend's `<state_dir>/vms/<id>/`;
- deleted with the workspace (the pod and the claim go together).

`ReadWriteOnce` carries the placement rule on k8s: the volume
attaches to one node, the pod schedules onto that node, and a second
pod cannot attach the same volume — the cluster enforces what the
local backend enforces with the row's host field. The PVC must hold
both the overlay and the home volume, so size it for the sum (the
derived default does); the qcow2 overlay and the sparse ext4 grow
on demand. A start recreates a missing claim the same way the local
backend heals missing artifact files.

Factory reset on k8s needs the runner agent to delete the overlay
file inside the PVC mount; until that lands, the API answers with
a named error instead of dropping the whole claim (which would take
`/home` with it).

## Image pinning

A catalog image with live workspaces cannot be removed
(`DELETE /api/v1/images/{hash}` answers `409` naming the
workspace): each workspace's overlay backs that exact image file,
and its kernel and initrd are read from the image's cache on every
boot. Deleting the workspace releases the pin.

## Environment variables

| Variable                          | Default      | Meaning                                                                                                           |
| --------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------- |
| `MSKSD_ROOT_MIB`                  | `10240`      | Default overlay (root) size for new workspaces, MiB.                                                              |
| `MSKSD_HOME_MIB`                  | `2048`       | Default `/home` volume size, MiB.                                                                                 |
| `MSKSD_STORAGE_WARN_PCT`          | `90`         | State-disk percentage used that moves pressure to `warn` (#184).                                                  |
| `MSKSD_STORAGE_FLOOR_MIB`         | `512`        | Free state-disk MiB below which pressure is `critical` and writes answer `507` (#184).                            |
| `MSKSD_QEMU_IMG`                  | `qemu-img`   | The `qemu-img` binary that creates overlays.                                                                      |
| `MSKSD_MKFS_EXT4`                 | `mkfs.ext4`  | The mkfs that formats `/home` volumes.                                                                            |
| `MSKSD_RESIZE2FS`                 | `resize2fs`  | The resize2fs that moves `/home` volumes (#184).                                                                  |
| `MSKSD_E2FSCK`                    | `e2fsck`     | The e2fsck that quiets a volume before a resize (#184).                                                           |
| `MSKSD_MKISOFS`                   | `mkisofs`    | The mkisofs (genisoimage) that builds `cidata` seed disks (#41).                                                  |
| `MSKSD_HOST_NAME`                 | the hostname | The host recorded as owning locally-created artifacts.                                                            |
| `MSKSD_SHUTDOWN_TIMEOUT_S`        | `20`         | How long `stop` waits for the guest's clean poweroff before the fallback kill; a stop answers within this bound.  |
| `MSKSD_MOVE_WAIT_TIMEOUT_S`       | `120`        | How long a boot, delete, or volume move waits for the workspace's other volume move before answering a named 409. |
| `MSKSD_K8S_STORAGE_CLASS`         | unset        | Storage class for per-workspace claims; unset asks the cluster's default.                                         |
| `MSKSD_K8S_WORKSPACE_STORAGE_GIB` | unset        | Claim size in GiB; unset derives it from `root_mib` + `home_mib`.                                                 |
