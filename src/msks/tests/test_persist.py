"""Persistent-artifact unit tests (#14): overlay + home volume.

The host tools are stub scripts under test — qemu-img answers
``info`` with the file's apparent size and touches the overlay on
``create``; mkfs records its argv — so the suite exercises the
module's logic (layout, idempotence, atomic install, clamping,
error mapping) without the real binaries.
"""

import os
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


def write_mkisofs_stub(directory: Path, record: Path) -> Path:
    """A recording mkisofs stand-in for the #41 seed build.

    Parses ``-output`` out of the argv, appends the staged payload
    files' contents to the record between markers (they are gone by
    the time the caller can look), and writes a non-empty image so
    install() has a file to rename.
    """
    stub = directory / "mkisofs"
    stub.write_text(
        "#!/bin/sh\n"
        "prev=\n"
        "out=\n"
        "for arg do\n"
        '  case "$prev" in -output) out=$arg ;; esac\n'
        "  prev=$arg\n"
        "done\n"
        "{\n"
        '  printf "mkisofs $*\\n"\n'
        '  printf "staging-mode %s" "$(stat -c %a .)"\n'
        '  printf -- "--- user-data ---\\n"\n'
        "  cat user-data\n"
        '  printf "\\n--- meta-data ---\\n"\n'
        "  cat meta-data\n"
        '  printf "\\n"\n'
        f"}} >> {record}\n"
        'printf iso-content > "$out"\n'
    )
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
        mkisofs=str(write_mkisofs_stub(tmp_path, record)),
    )
    return settings, record, base


def spec(
    base: Path,
    root_mib: int = 10240,
    home_mib: int = 2048,
    user_data=None,
    ssh_pubkey=None,
) -> VmSpec:
    return VmSpec(
        workspace_id=WID,
        kernel=base.parent / "vmlinux",
        rootfs=base,
        root_mib=root_mib,
        home_mib=home_mib,
        user_data=user_data,
        ssh_pubkey=ssh_pubkey,
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
    assert persist.seed_path(tmp_path, "ws1") == (tmp_path / "vms" / "ws1" / "seed.img")


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


def test_seed_metadata_keys_off_the_workspace() -> None:
    """NoCloud meta-data: instance-id drives cloud-init's run-once
    semantics. No local-hostname — the image's hostname stays stable
    across workspaces."""
    assert persist.seed_metadata("ws-41") == "instance-id: ws-41\n"


async def test_ensure_builds_seed_when_user_data_set(tools) -> None:
    """A user_data workspace (#41) gets its cidata seed beside the
    overlay and home volume: kilobytes, iso9660-labeled, 0600 (the
    payload can embed tokens)."""
    settings, record, base = tools
    payload = "#!/bin/sh\necho provisioned > /root/stamp\n"
    await persist.ensure_artifacts(spec(base, user_data=payload), settings)
    seed = persist.seed_path(settings.state_dir, WID)
    assert seed.is_file()
    assert seed.read_bytes() == b"iso-content"
    assert seed.stat().st_mode & 0o777 == 0o600
    log = record.read_text()
    assert "staging-mode 700" in log
    assert "-volid cidata" in log
    assert f"--- user-data ---\n{payload}" in log
    assert "--- meta-data ---\ninstance-id: ws-persist\n" in log
    assert "local-hostname" not in log
    assert persist.overlay_path(settings.state_dir, WID).is_file()
    assert persist.home_volume_path(settings.state_dir, WID).is_file()
    assert not tmp_debris(settings)


async def test_ensure_skips_seed_without_user_data(tools) -> None:
    """The seed exists exactly when user_data was given: a plain
    workspace boots as before, no seed, no mkisofs run."""
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base), settings)
    assert not persist.seed_path(settings.state_dir, WID).exists()
    assert "mkisofs" not in record.read_text()


async def test_seed_is_idempotent(tools) -> None:
    """An existing seed is data, not debris: ensure runs mkisofs
    exactly once across heals."""
    settings, record, base = tools
    payload = "#!/bin/sh\ntrue\n"
    await persist.ensure_artifacts(spec(base, user_data=payload), settings)
    await persist.ensure_artifacts(spec(base, user_data=payload), settings)
    assert record.read_text().count("mkisofs") == 1


async def test_failed_seed_rolls_back_the_fresh_pair(tools) -> None:
    """A create whose seed build fails installs nothing at any final
    path — the retry starts from a clean triple, not a strict-refusal
    wedge on the pair the failed run already made."""
    settings, record, base = tools
    mkisofs = Path(settings.mkisofs)
    mkisofs.write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(MicrovmError, match="mkisofs on the cidata seed failed"):
        await persist.ensure_artifacts(
            spec(base, user_data="#!/bin/sh\ntrue\n"), settings
        )
    assert not persist.seed_path(settings.state_dir, WID).exists()
    assert not persist.overlay_path(settings.state_dir, WID).exists()
    assert not persist.home_volume_path(settings.state_dir, WID).exists()
    assert not tmp_debris(settings)
    write_mkisofs_stub(mkisofs.parent, record)
    await persist.ensure_artifacts(spec(base, user_data="#!/bin/sh\ntrue\n"), settings)
    assert persist.seed_path(settings.state_dir, WID).is_file()
    assert persist.overlay_path(settings.state_dir, WID).is_file()
    assert persist.home_volume_path(settings.state_dir, WID).is_file()


# --- Home-volume export/import (#80) ---


def ext4_image(windows: list[bytes]) -> bytes:
    """A stand-in ext4 volume: the magic at 1080, then whole 1 MiB
    windows of caller-chosen bytes (zeros for sparse regions)."""
    body = bytearray(b"".join(windows))
    body[persist.EXT4_MAGIC_OFFSET : persist.EXT4_MAGIC_OFFSET + 2] = persist.EXT4_MAGIC
    return bytes(body)


async def yielding(chunks: list[bytes]):
    """An async body iterator that hands out ``chunks`` as given —
    sizes and boundaries are the caller's (the wire chunks never
    match the import window)."""
    for chunk in chunks:
        yield chunk


async def collect(path: Path) -> bytes:
    """Everything :func:`persist.read_volume` yields from an open
    fd, joined."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return b"".join([window async for window in persist.read_volume(fd)])
    finally:
        os.close(fd)


async def test_import_home_volume_installs_the_body(tools) -> None:
    """A well-formed body lands verbatim at the volume path, the
    scratch is swept, and the return is the byte count."""
    settings, _record, _base = tools
    image = ext4_image([b"volume-data".ljust(persist.HOME_WINDOW_B, b"x")])
    total = await persist.import_home_volume(
        settings.state_dir, WID, yielding([image[:1234], image[1234:]])
    )
    assert total == len(image)
    home = persist.home_volume_path(settings.state_dir, WID)
    assert home.read_bytes() == image
    assert not tmp_debris(settings)
    # The export side reads the same bytes back (#80 round trip).
    assert await collect(home) == image


async def test_import_home_volume_keeps_zero_windows_sparse(tools) -> None:
    """All-zero windows become holes: a 3 MiB volume whose two last
    windows are blank costs ~1 MiB of real disk, not 3."""
    settings, _record, _base = tools
    blank = b"\0" * persist.HOME_WINDOW_B
    image = ext4_image([b"data".ljust(persist.HOME_WINDOW_B, b"d"), blank, blank])
    await persist.import_home_volume(settings.state_dir, WID, yielding([image]))
    home = persist.home_volume_path(settings.state_dir, WID)
    assert home.stat().st_size == len(image)
    assert home.stat().st_blocks * 512 < 2 * persist.HOME_WINDOW_B
    assert home.read_bytes() == image


async def test_import_home_volume_replaces_an_existing_volume(tools) -> None:
    """Import replaces what stood at the path — restore duty, not
    append."""
    settings, _record, _base = tools
    home = persist.home_volume_path(settings.state_dir, WID)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"stale-contents" * 10)
    image = ext4_image([b"fresh".ljust(persist.HOME_WINDOW_B, b"f")])
    await persist.import_home_volume(settings.state_dir, WID, yielding([image]))
    assert home.read_bytes() == image


async def test_import_home_volume_refuses_non_ext4(tools) -> None:
    """A body without the ext4 magic is refused the moment its
    prefix arrives (before a window is written): nothing is
    installed, the old volume survives, and the scratch is swept."""
    settings, _record, _base = tools
    home = persist.home_volume_path(settings.state_dir, WID)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"preexisting")
    with pytest.raises(ValueError, match="not an ext4 image"):
        await persist.import_home_volume(
            settings.state_dir, WID, yielding([b"garbage" * 1000])
        )
    assert home.read_bytes() == b"preexisting"
    assert not tmp_debris(settings)


async def test_import_home_volume_refuses_a_short_body(tools) -> None:
    """A non-empty body shorter than the magic's offset never
    reaches the early check; the post-stream backstop refuses it."""
    settings, _record, _base = tools
    with pytest.raises(ValueError, match="not an ext4 image"):
        await persist.import_home_volume(
            settings.state_dir, WID, yielding([b"garbage"])
        )
    assert not persist.home_volume_path(settings.state_dir, WID).exists()
    assert not tmp_debris(settings)


async def test_import_home_volume_refuses_an_empty_body(tools) -> None:
    settings, _record, _base = tools
    with pytest.raises(ValueError, match="body is empty"):
        await persist.import_home_volume(settings.state_dir, WID, yielding([]))
    assert not persist.home_volume_path(settings.state_dir, WID).exists()
    assert not tmp_debris(settings)


async def test_import_home_volume_keeps_short_zero_tail_sparse(tools) -> None:
    """A final partial window of zeros relies on the closing
    truncate for its hole — the tail skip and the window skip share
    one path."""
    settings, _record, _base = tools
    image = ext4_image([b"full".ljust(persist.HOME_WINDOW_B, b"u")])
    image += b"\0" * 4096
    await persist.import_home_volume(
        settings.state_dir, WID, yielding([image[:10], image[10:]])
    )
    home = persist.home_volume_path(settings.state_dir, WID)
    assert home.stat().st_size == len(image)
    assert home.read_bytes() == image
