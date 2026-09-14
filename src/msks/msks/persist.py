"""Per-workspace persistent artifacts: root overlay + /home volume (#14).

A workspace owns exactly two persistent artifacts, with lifetimes
separate from the VM's:

- the **root overlay** — a qcow2 copy-on-write file backed by the
  workspace's base image, under ``<state_dir>/vms/<id>/root.qcow2``.
  Root-filesystem writes (package installs, ``/etc`` edits) land in
  the overlay; the shared base stays pristine and every workspace on
  that image sees an unmodified copy of it.
- the **home volume** — an ext4 image file under
  ``<state_dir>/volumes/<id>.ext4``, attached as a second virtio-blk
  disk the guest mounts at /home (labeled ``msks-home``).

Both are created at workspace create, survive ``stop``/``start``,
and are removed with the workspace. ``ensure_artifacts`` builds each
artifact under a private temporary name and installs it with one
atomic rename: a file at the final path is always complete, so a
failed create (a missing tool, ENOSPC, a partial write) can never
leave a blank or half-written artifact for a retry — or a boot — to
mistake for valid. An existing artifact is left alone, so ``launch``
can heal artifacts a crash (or a pre-#14 workspace row) lost without
touching data that exists. ``remove_overlay`` is factory reset's
half — the root returns to the pristine base, the home volume keeps
its data. A crash mid-create leaves only a ``*.tmp`` sibling behind
(harmless debris; the removal helpers sweep them).
"""

import asyncio
import contextlib
import itertools
import json
import os
from pathlib import Path

from .microvm.errors import MicrovmError
from .microvm.spec import VmSpec

MIB = 1024 * 1024
HOME_VOLUME_LABEL = "msks-home"

_tmp_counter = itertools.count()


def overlay_path(state_dir: Path, workspace_id: str) -> Path:
    """The root overlay: the file the VM's root disk boots from."""
    return state_dir / "vms" / workspace_id / "root.qcow2"


def home_volume_path(state_dir: Path, workspace_id: str) -> Path:
    """The /home volume: the ext4 file the guest mounts at /home."""
    return state_dir / "volumes" / f"{workspace_id}.ext4"


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
    """Create the workspace's overlay and home volume when absent."""
    state_dir = settings.state_dir
    home = home_volume_path(state_dir, spec.workspace_id)
    if not home.is_file():
        home.parent.mkdir(parents=True, exist_ok=True)
        scratch = tmp_sibling(home)
        try:
            with scratch.open("wb") as handle:
                # Sparse: an idle volume costs its metadata, not its size.
                handle.truncate(spec.home_mib * MIB)
            await run_tool(
                [settings.mkfs_ext4, "-q", "-F", "-L", HOME_VOLUME_LABEL, str(scratch)],
                "mkfs on the home volume",
            )
            scratch.replace(home)
        finally:
            scratch.unlink(missing_ok=True)
    overlay = overlay_path(state_dir, spec.workspace_id)
    if not overlay.is_file():
        overlay.parent.mkdir(parents=True, exist_ok=True)
        scratch = tmp_sibling(overlay)
        try:
            await create_overlay(spec, settings, scratch)
            scratch.replace(overlay)
        finally:
            scratch.unlink(missing_ok=True)


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
            str(overlay),
            str(max(spec.root_mib * MIB, size)),
        ],
        "qemu-img create of the root overlay",
    )


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


async def run_tool(argv: list[str], what: str) -> bytes:
    """Run one host tool; a failure becomes a named operator error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise MicrovmError(f"{what}: tool not found: {argv[0]}") from exc
    output, _ = await proc.communicate()
    if proc.returncode != 0:
        detail = output.decode(errors="replace").strip()[:400]
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
