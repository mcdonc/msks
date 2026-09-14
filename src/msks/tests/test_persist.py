"""Persistent-artifact unit tests (#14): overlay + home volume.

The host tools are stub scripts under test — qemu-img answers
``info`` with the file's apparent size and touches the overlay on
``create``; mkfs records its argv — so the suite exercises the
module's logic (layout, idempotence, clamping, error mapping)
without the real binaries.
"""

from pathlib import Path

import pytest
from msks.microvm.errors import MicrovmError
from msks.microvm.spec import VmSpec
from msks.settings import VmmSettings

from msks import persist

WID = "ws-persist"


def write_qemu_stub(directory: Path, record: Path) -> Path:
    """A recording qemu-img stand-in; answers info, fakes create."""
    stub = directory / "qemu-img"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "qemu-img $*" >> '
        f"{record}\n"
        'case "$1" in\n'
        "  info)\n"
        "    for img do :; done\n"
        '    size=$(stat -c %s "$img")\n'
        '    printf \'{"format":"raw","virtual-size":%s}\\n\' "$size"\n'
        "    ;;\n"
        "  create)\n"
        "    # create -f qcow2 -F raw -b BASE OVERLAY SIZE: arg 8\n"
        '    : > "$8"\n'
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub


def write_mkfs_stub(directory: Path, record: Path) -> Path:
    """A recording mkfs.ext4 stand-in."""
    stub = directory / "mkfs.ext4"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "mkfs $*" >> {record}\n')
    stub.chmod(0o755)
    return stub


@pytest.fixture
def tools(tmp_path: Path):
    """Stubbed tool settings plus the argv record; a 4 MiB base image."""
    record = tmp_path / "tools.log"
    base = tmp_path / "base.ext4"
    with base.open("wb") as handle:
        handle.truncate(4 * 1024 * 1024)
    settings = VmmSettings(
        state_dir=tmp_path / "state",
        qemu_img=str(write_qemu_stub(tmp_path, record)),
        mkfs_ext4=str(write_mkfs_stub(tmp_path, record)),
    )
    return settings, record, base


def spec(base: Path, root_mib: int = 10240, home_mib: int = 2048) -> VmSpec:
    return VmSpec(
        workspace_id=WID,
        kernel=base.parent / "vmlinux",
        rootfs=base,
        root_mib=root_mib,
        home_mib=home_mib,
    )


def test_paths_pin_the_layout(tmp_path: Path) -> None:
    assert persist.overlay_path(tmp_path, "ws1") == (
        tmp_path / "vms" / "ws1" / "root.qcow2"
    )
    assert persist.home_volume_path(tmp_path, "ws1") == (
        tmp_path / "volumes" / "ws1.ext4"
    )


async def test_ensure_creates_overlay_and_volume(tools) -> None:
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base), settings)
    overlay = persist.overlay_path(settings.state_dir, WID)
    home = persist.home_volume_path(settings.state_dir, WID)
    assert overlay.is_file()
    assert home.is_file()
    # The volume is sparse at its requested size; the overlay is
    # created at the requested virtual size (bigger than the base).
    assert home.stat().st_size == 2048 * 1024 * 1024
    assert home.stat().st_blocks * 512 < 1024 * 1024
    argv = record.read_text()
    assert f"qemu-img create -f qcow2 -F raw -b {base} {overlay} " in argv
    assert f"mkfs -q -F -L msks-home {home}" in argv


async def test_ensure_is_idempotent(tools) -> None:
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base), settings)
    record.write_text("")
    await persist.ensure_artifacts(spec(base), settings)
    # Existing artifacts are data, not debris: no tool runs again.
    assert record.read_text() == ""


async def test_overlay_size_never_shrinks_below_base(tools) -> None:
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base, root_mib=256), settings)
    overlay = persist.overlay_path(settings.state_dir, WID)
    # The base is 4 MiB; a 256 MiB request wins. A request below the
    # base's size would truncate the base filesystem instead.
    assert f"-b {base} {overlay} 268435456" in record.read_text()
    persist.remove_overlay(settings.state_dir, WID)
    await persist.ensure_artifacts(spec(base, root_mib=1), settings)
    assert f"-b {base} {overlay} 4194304" in record.read_text()


async def test_missing_base_names_the_path(tools) -> None:
    settings, _record, _base = tools
    with pytest.raises(MicrovmError, match="base image.*not-found.ext4"):
        await persist.ensure_artifacts(
            spec(Path("/nonexistent/not-found.ext4")), settings
        )


async def test_tool_failure_maps_to_named_error(tools) -> None:
    settings, _record, base = tools
    settings.qemu_img = "false"
    with pytest.raises(MicrovmError, match="qemu-img info.*failed"):
        await persist.ensure_artifacts(spec(base), settings)


async def test_garbage_info_maps_to_named_error(tmp_path: Path) -> None:
    """A qemu-img that answers something other than a JSON document
    with a virtual-size is a named error, not a KeyError."""
    stub = tmp_path / "qemu-img"
    stub.write_text('#!/bin/sh\nprintf "not json\n"\n')
    stub.chmod(0o755)
    base = tmp_path / "base.ext4"
    base.write_bytes(b"x")
    settings = VmmSettings(state_dir=tmp_path / "state", qemu_img=str(stub))
    with pytest.raises(MicrovmError, match="no virtual-size"):
        await persist.ensure_artifacts(
            VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=base), settings
        )


async def test_missing_tool_names_the_binary(tools) -> None:
    settings, _record, base = tools
    settings.mkfs_ext4 = "/nonexistent/mkfs.ext4"
    with pytest.raises(MicrovmError, match="tool not found.*mkfs.ext4"):
        await persist.ensure_artifacts(spec(base), settings)


def test_remove_overlay_and_volume(tmp_path: Path) -> None:
    overlay = persist.overlay_path(tmp_path, WID)
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"overlay")
    assert persist.remove_overlay(tmp_path, WID) is True
    assert persist.remove_overlay(tmp_path, WID) is False
    home = persist.home_volume_path(tmp_path, WID)
    home.parent.mkdir(parents=True)
    home.write_bytes(b"volume")
    persist.remove_home_volume(tmp_path, WID)
    assert not home.exists()
