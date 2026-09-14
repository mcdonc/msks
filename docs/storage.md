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

Both sizes are fixed at workspace create and configurable three
ways: per request (`root_mib` / `home_mib` on workspace create),
per daemon (`MSKSD_ROOT_MIB`, default 10240, and `MSKSD_HOME_MIB`,
default 2048), and per image (the catalog's manifest carries the
cmdline the guest boots, which pairs with the sizes). The overlay's
virtual size never drops below its base image's size — a smaller
disk would truncate the base filesystem.

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

| Variable                          | Default      | Meaning                                                                                                          |
| --------------------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------- |
| `MSKSD_ROOT_MIB`                  | `10240`      | Default overlay (root) size for new workspaces, MiB.                                                             |
| `MSKSD_HOME_MIB`                  | `2048`       | Default `/home` volume size, MiB.                                                                                |
| `MSKSD_QEMU_IMG`                  | `qemu-img`   | The `qemu-img` binary that creates overlays.                                                                     |
| `MSKSD_MKFS_EXT4`                 | `mkfs.ext4`  | The mkfs that formats `/home` volumes.                                                                           |
| `MSKSD_MKISOFS`                   | `mkisofs`    | The mkisofs (genisoimage) that builds `cidata` seed disks (#41).                                                 |
| `MSKSD_HOST_NAME`                 | the hostname | The host recorded as owning locally-created artifacts.                                                           |
| `MSKSD_SHUTDOWN_TIMEOUT_S`        | `20`         | How long `stop` waits for the guest's clean poweroff before the fallback kill; a stop answers within this bound. |
| `MSKSD_K8S_STORAGE_CLASS`         | unset        | Storage class for per-workspace claims; unset asks the cluster's default.                                        |
| `MSKSD_K8S_WORKSPACE_STORAGE_GIB` | unset        | Claim size in GiB; unset derives it from `root_mib` + `home_mib`.                                                |
