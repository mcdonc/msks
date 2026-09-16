"""Per-workspace persistent artifacts: root overlay + /home volume (#14).

A workspace owns its persistent artifacts, with lifetimes separate
from the VM's:

- the **root overlay** — a qcow2 copy-on-write file backed by the
  workspace's base image, under ``<state_dir>/vms/<id>/root.qcow2``.
  Root-filesystem writes (package installs, ``/etc`` edits) land in
  the overlay; the shared base stays pristine and every workspace on
  that image sees an unmodified copy of it.
- the **home volume** — an ext4 image file under
  ``<state_dir>/volumes/<id>.ext4``, attached as a second virtio-blk
  disk the guest mounts at /home (labeled ``msks-home``).
- the **seed disk** (#41) — present when the workspace carries a
  ``user_data`` payload or a minted identity (#111): a small iso9660
  image under ``<state_dir>/vms/<id>/seed.img`` labeled ``cidata``,
  attached read-only as a third virtio-blk disk. Its ``user-data``
  document is the operator payload composed with the identity's
  seeding script when a key was minted — verbatim alone otherwise —
  plus a NoCloud ``meta-data`` (instance-id): exactly what
  cloud-init's datasource reads. It can embed tokens, so it is
  installed mode 0600 (the row that records the payload makes the
  same promise: the daemon creates its database file 0600).

The first two are created at workspace create; all three survive
``stop``/``start`` and are removed with the workspace (the seed
rides the same vm directory — a crash mid-seed-build leaves only
its staging directory behind, which the same rmtree owns).
``ensure_artifacts`` builds each
artifact under a private temporary name and installs it with one
atomic rename: a file at the final path is always complete, so a
failed create (a missing tool, ENOSPC, a partial write) can never
leave a blank or half-written artifact for a retry — or a boot — to
mistake for valid. An existing artifact is left alone, so ``launch``
can heal artifacts a crash (or a pre-#14 workspace row) lost without
touching data that exists. ``remove_overlay`` is factory reset's
half — the root returns to the pristine base, the home volume keeps
its data, and the seed stays: it is immutable create-time input, so
a reset workspace re-provisions from it exactly as a fresh one
would (cloud-init's run-once state, the /var/lib/cloud cache, lives
on the overlay a reset drops). A crash mid-create leaves only ``*.tmp`` scratch behind:
harmless debris, swept by the removal helpers for the file-shaped
artifacts and by the vm-dir rmtree for the seed's staging
directory.
"""

import asyncio
import contextlib
import itertools
import json
import os
import shutil
from pathlib import Path

from .identity import compose_user_data
from .microvm.errors import MicrovmError
from .microvm.spec import VmSpec

MIB = 1024 * 1024
HOME_VOLUME_LABEL = "msks-home"
SEED_LABEL = "cidata"

_tmp_counter = itertools.count()


def overlay_path(state_dir: Path, workspace_id: str) -> Path:
    """The root overlay: the file the VM's root disk boots from."""
    return state_dir / "vms" / workspace_id / "root.qcow2"


def home_volume_path(state_dir: Path, workspace_id: str) -> Path:
    """The /home volume: the ext4 file the guest mounts at /home."""
    return state_dir / "volumes" / f"{workspace_id}.ext4"


def seed_path(state_dir: Path, workspace_id: str) -> Path:
    """The #41 seed disk: the cidata iso carrying the workspace's
    user_data, attached read-only beside the root and home disks."""
    return state_dir / "vms" / workspace_id / "seed.img"


def tmp_sibling(target: Path) -> Path:
    """A private scratch name beside ``target``.

    Unique per caller so concurrent ``ensure_artifacts`` for the same
    workspace never share a scratch file; the pid makes debris from
    a crashed creator identifiable on the host.
    """
    return target.with_name(f"{target.name}.{os.getpid()}-{next(_tmp_counter)}.tmp")


def sweep_tmp_siblings(target: Path) -> None:
    """Remove scratch files left beside ``target`` by failed creates."""
    with contextlib.suppress(OSError):
        for debris in target.parent.glob(f"{target.name}.*.tmp"):
            debris.unlink(missing_ok=True)


async def ensure_artifacts(spec: VmSpec, settings) -> None:
    """Create the workspace's overlay and home volume when absent.

    Each artifact is installed atomically (private scratch file, one
    rename), and a failure rolls back what *this call* installed: a
    failed overlay create takes the freshly-made volume with it, so
    the retry starts clean instead of meeting its own half-made pair
    at the next create (a strict-refusal wedge). Only a daemon crash
    mid-create can leave an orphan — the next create names it.
    """
    state_dir = settings.state_dir
    installed: list[Path] = []
    try:
        home = home_volume_path(state_dir, spec.workspace_id)
        if not home.is_file():
            await create_home_volume(home, spec, settings)
            installed.append(home)
        overlay = overlay_path(state_dir, spec.workspace_id)
        if not overlay.is_file():
            await create_overlay(spec, settings, overlay)
            installed.append(overlay)
        await ensure_seed(spec, settings, installed)
    except BaseException:
        for artifact in installed:
            artifact.unlink(missing_ok=True)
        raise


async def ensure_seed(spec: VmSpec, settings, installed: list[Path]) -> None:
    """Build the #41 seed when the workspace carries a payload — its
    own or the minted identity's (#111) — and the file is absent; a
    fresh build joins the rollback list."""
    if spec.user_data is None and spec.ssh_pubkey is None:
        return
    seed = seed_path(settings.state_dir, spec.workspace_id)
    if seed.is_file():
        return
    await create_seed(spec, settings)
    installed.append(seed)


async def create_home_volume(target: Path, spec: VmSpec, settings) -> None:
    """Format one sparse, labeled ext4 volume and install it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = tmp_sibling(target)
    try:
        with scratch.open("wb") as handle:
            # Sparse: an idle volume costs its metadata, not its size.
            handle.truncate(spec.home_mib * MIB)
        await run_tool(
            [settings.mkfs_ext4, "-q", "-F", "-L", HOME_VOLUME_LABEL, str(scratch)],
            "mkfs on the home volume",
        )
        install(scratch, target)
    finally:
        scratch.unlink(missing_ok=True)


def install(scratch: Path, target: Path) -> None:
    """Move a finished artifact onto its final name.

    Same-directory rename, atomic on Linux. An OSError here (a
    concurrent removal sweeping the scratch file, a vanished
    directory) becomes a named error instead of a raw 500.
    """
    try:
        scratch.replace(target)
    except OSError as exc:
        raise MicrovmError(f"could not install {target}: {exc}") from exc


async def create_overlay(spec: VmSpec, settings, overlay: Path) -> None:
    """One qcow2 overlay over the workspace's base image.

    The base stays untouched; the overlay carries every root write.
    Its virtual size is the requested root size, never smaller than
    the base — a smaller disk would truncate the base filesystem.
    """
    base = spec.rootfs
    if not base.is_file():
        raise MicrovmError(f"base image for the overlay not found: {base}")
    size, base_format = await base_info(base, settings.qemu_img)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    scratch = tmp_sibling(overlay)
    try:
        await run_tool(
            [
                settings.qemu_img,
                "create",
                "-f",
                "qcow2",
                "-F",
                base_format,
                "-b",
                str(base),
                str(scratch),
                str(max(spec.root_mib * MIB, size)),
            ],
            "qemu-img create of the root overlay",
        )
        install(scratch, overlay)
    finally:
        scratch.unlink(missing_ok=True)


def seed_metadata(workspace_id: str) -> str:
    """The seed's ``meta-data``: cloud-init NoCloud keys.

    ``instance-id`` is the workspace id, so cloud-init's run-once
    semantics key off the workspace: a stop/start or a daemon restart
    never re-provisions. A factory reset DOES re-provision — the
    "already ran" state (/var/lib/cloud) lives on the overlay the
    reset drops. No ``local-hostname``: the image's own hostname
    (msks-guest) stays stable across workspaces, and per-workspace
    identity is what the id column is for.
    """
    return f"instance-id: {workspace_id}\n"


async def create_seed(spec: VmSpec, settings) -> None:
    """Build and install the workspace's #41 seed disk.

    mkisofs packs the staged ``user-data``/``meta-data`` into a
    scratch iso9660 volume labeled ``cidata`` — the exact layout
    cloud-init's NoCloud datasource expects — and the finished image
    is installed with the house atomic rename, mode 0600 (the payload
    can embed tokens). The staged user-data is the composed document:
    the operator's payload beside the minted identity's seeding
    script when a key was minted (#111), the operator's payload
    verbatim otherwise.
    """
    target = seed_path(settings.state_dir, spec.workspace_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # The staging directory keeps the tmp_sibling shape, so a crash
    # mid-build leaves visibly-temporary debris inside the vm dir
    # (removed with the workspace by the vm-dir rmtree). 0700 beats
    # the umask: the plaintext payload must not be readable by other
    # local users for the duration of the build — or forever, if a
    # crash leaves the staging directory behind (tls.py's no-window
    # rule for private keys).
    stage = tmp_sibling(target)
    stage.mkdir(mode=0o700)
    try:
        (stage / "user-data").write_text(
            compose_user_data(spec.user_data, spec.ssh_pubkey), encoding="utf-8"
        )
        (stage / "meta-data").write_text(
            seed_metadata(spec.workspace_id), encoding="utf-8"
        )
        image = stage / "seed.img"
        await run_tool(
            [
                settings.mkisofs,
                "-quiet",
                "-output",
                str(image),
                "-volid",
                SEED_LABEL,
                "-joliet",
                "-rock",
                "user-data",
                "meta-data",
            ],
            "mkisofs on the cidata seed",
            cwd=stage,
        )
        image.chmod(0o600)
        install(image, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


async def base_info(base: Path, qemu_img: str) -> tuple[int, str]:
    """The base image's ``(virtual size, format)`` from qemu-img info."""
    output = await run_tool(
        [qemu_img, "info", "--output=json", str(base)],
        "qemu-img info on the base image",
    )
    try:
        document = json.loads(output)
        return int(document["virtual-size"]), str(document["format"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MicrovmError(
            f"qemu-img info returned no virtual-size for {base}"
        ) from exc


async def run_tool(argv: list[str], what: str, cwd: Path | None = None) -> bytes:
    """Run one host tool; a failure becomes a named operator error.

    stderr stays out of the captured stdout: ``qemu-img info`` is
    parsed as JSON, and a chatty warning line ahead of the document
    must not turn into a spurious parse failure. ``cwd`` serves the
    seed build (mkisofs takes the payload paths relative to its
    staging directory).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=None if cwd is None else str(cwd),
        )
    except FileNotFoundError as exc:
        raise MicrovmError(f"{what}: tool not found: {argv[0]}") from exc
    output, err = await proc.communicate()
    if proc.returncode != 0:
        detail = (output + err).decode(errors="replace").strip()[:400]
        raise MicrovmError(f"{what} failed ({proc.returncode}): {detail}")
    return output


def remove_overlay(state_dir: Path, workspace_id: str) -> bool:
    """Delete the root overlay; False when there was none to delete."""
    overlay = overlay_path(state_dir, workspace_id)
    sweep_tmp_siblings(overlay)
    try:
        overlay.unlink()
        return True
    except FileNotFoundError:
        return False


def remove_home_volume(state_dir: Path, workspace_id: str) -> None:
    """Delete the home volume file (idempotent)."""
    home = home_volume_path(state_dir, workspace_id)
    sweep_tmp_siblings(home)
    home.unlink(missing_ok=True)
