"""Persistent-artifact unit tests (#14): overlay + home volume.

The host tools are stub scripts under test — qemu-img answers
``info`` with the file's apparent size and touches the overlay on
``create``; mkfs records its argv — so the suite exercises the
module's logic (layout, idempotence, atomic install, clamping,
error mapping) without the real binaries.
"""

import re
from pathlib import Path

import pytest
from msks.microvm.errors import MicrovmError
from msks.microvm.spec import VmSpec
from msks.settings import VmmSettings

from msks import persist

WID = "ws-persist"


def write_qemu_stub(directory: Path, record: Path) -> Path:
    """A recording qemu-img stand-in; answers info, fakes create.

    The create branch finds its target by parsing the options (any
    reorder of ``-f/-F/-b`` pairs), not by argument position.
    """
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
        "    shift\n"
        '    while [ "$#" -gt 1 ]; do\n'
        '      case "$1" in -f|-F|-b) shift 2 ;; *) break ;; esac\n'
        "    done\n"
        '    : > "$1"\n'
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub


def write_mkfs_stub(directory: Path, record: Path) -> Path:
    """A recording mkfs.ext4 stand-in."""
    stub = directory / "mkfs.ext4"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "mkfs $*" >> {record}\nexit 0\n')
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


def recorded_sizes(record: Path, base: Path) -> list[str]:
    """The overlay sizes asked of qemu-img create, in order."""
    return re.findall(
        rf"qemu-img create \S+(?: \S+)* -b {re.escape(str(base))} \S+ (\d+)",
        record.read_text(),
    )


def tmp_debris(settings: VmmSettings) -> list[Path]:
    """Scratch files left under the artifact directories."""
    homes = list((settings.state_dir / "volumes").glob("*.tmp"))
    overlays = list((settings.state_dir / "vms").rglob("*.tmp"))
    return homes + overlays


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
    # The volume is sparse at its requested size.
    assert home.stat().st_size == 2048 * 1024 * 1024
    assert home.stat().st_blocks * 512 < 1024 * 1024
    # qemu-img create targeted a scratch sibling of the final overlay
    # and asked for the requested virtual size (bigger than the base).
    argv = record.read_text()
    assert f"qemu-img create -f qcow2 -F raw -b {base} " in argv
    assert recorded_sizes(record, base) == [str(10240 * 1024 * 1024)]
    assert "mkfs -q -F -L msks-home" in argv
    assert not tmp_debris(settings)


async def test_ensure_is_idempotent(tools) -> None:
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base), settings)
    record.write_text("")
    await persist.ensure_artifacts(spec(base), settings)
    # Existing artifacts are data, not debris: no tool runs again.
    assert record.read_text() == ""


async def test_failed_mkfs_leaves_no_volume_and_retry_succeeds(tools) -> None:
    """A failed format must not wedge the workspace on a blank file.

    The old existence-equals-valid check made every later start skip
    the mkfs and boot an unformatted disk.
    """
    settings, record, base = tools
    (Path(settings.mkfs_ext4)).write_text("#!/bin/sh\nexit 1\n")
    home = persist.home_volume_path(settings.state_dir, WID)
    with pytest.raises(MicrovmError, match="mkfs on the home volume failed"):
        await persist.ensure_artifacts(spec(base), settings)
    assert not home.exists()
    assert not tmp_debris(settings)
    # The retry, with a working mkfs, formats and installs the volume.
    write_mkfs_stub(Path(settings.mkfs_ext4).parent, record)
    await persist.ensure_artifacts(spec(base), settings)
    assert home.is_file()
    assert "mkfs -q -F -L msks-home" in record.read_text()


async def test_failed_overlay_create_leaves_no_artifact(tools) -> None:
    """A create that dies mid-write installs nothing at the final
    path — and rolls back the volume it already made, so the retry
    starts from a clean pair instead of wedging the id on a leftover
    volume a strict create then refuses forever."""
    settings, record, base = tools
    qemu = Path(settings.qemu_img)
    qemu.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info)\n"
        "    for img do :; done\n"
        '    size=$(stat -c %s "$img")\n'
        '    printf \'{"format":"raw","virtual-size":%s}\\n\' "$size"\n'
        "    ;;\n"
        "  create)\n"
        "    shift\n"
        '    while [ "$#" -gt 1 ]; do\n'
        '      case "$1" in -f|-F|-b) shift 2 ;; *) break ;; esac\n'
        "    done\n"
        '    printf partial > "$1"\n'
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
    )
    overlay = persist.overlay_path(settings.state_dir, WID)
    home = persist.home_volume_path(settings.state_dir, WID)
    with pytest.raises(MicrovmError, match="qemu-img create.*failed"):
        await persist.ensure_artifacts(spec(base), settings)
    assert not overlay.exists()
    assert not home.exists()
    assert not tmp_debris(settings)
    # A retry with the working stub creates the complete pair.
    write_qemu_stub(qemu.parent, record)
    await persist.ensure_artifacts(spec(base), settings)
    assert overlay.is_file()
    assert home.is_file()


async def test_vanished_scratch_maps_to_named_error(tools) -> None:
    """A scratch file eaten by a concurrent sweep during the tool
    run becomes a named error, not a raw FileNotFoundError."""
    settings, _record, base = tools
    mkfs = Path(settings.mkfs_ext4)
    mkfs.write_text('#!/bin/sh\nfor arg do :; done\nrm -f "$arg"\nexit 0\n')
    with pytest.raises(MicrovmError, match="could not install"):
        await persist.ensure_artifacts(spec(base), settings)
    assert not persist.home_volume_path(settings.state_dir, WID).exists()
    assert not tmp_debris(settings)


async def test_overlay_size_never_shrinks_below_base(tools) -> None:
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base, root_mib=256), settings)
    assert recorded_sizes(record, base) == [str(256 * 1024 * 1024)]
    persist.remove_overlay(settings.state_dir, WID)
    await persist.ensure_artifacts(spec(base, root_mib=1), settings)
    # The base is 4 MiB; a request below the base's size would
    # truncate the base filesystem instead.
    assert recorded_sizes(record, base) == [
        str(256 * 1024 * 1024),
        str(4 * 1024 * 1024),
    ]


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


def test_remove_sweeps_crashed_scratch_files(tmp_path: Path) -> None:
    """Debris from a creator that died mid-build goes with the artifact."""
    overlay = persist.overlay_path(tmp_path, WID)
    overlay.parent.mkdir(parents=True)
    (overlay.parent / "root.qcow2.999-1.tmp").write_bytes(b"partial")
    home = persist.home_volume_path(tmp_path, WID)
    home.parent.mkdir(parents=True)
    (home.parent / f"{WID}.ext4.999-2.tmp").write_bytes(b"partial")
    persist.remove_overlay(tmp_path, WID)
    persist.remove_home_volume(tmp_path, WID)
    assert list(overlay.parent.glob("*.tmp")) == []
    assert list(home.parent.glob("*.tmp")) == []
