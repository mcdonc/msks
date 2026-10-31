"""Persistent-artifact unit tests (#14): overlay + home volume.

The host tools are stub scripts under test — qemu-img answers
``info`` with the file's apparent size and touches the overlay on
``create``; mkfs records its argv — so the suite exercises the
module's logic (layout, idempotence, atomic install, clamping,
error mapping) without the real binaries.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from ext4tools import realtools, write_e2fsck_stub
from msks.microvm.errors import MicrovmError
from msks.settings import VmmSettings
from msks.spec.vm import VmSpec

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
        "  resize)\n"
        '    truncate -s "$3" "$2"\n'
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub


def write_mkfs_stub(directory: Path, record: Path) -> Path:
    """A recording mkfs.ext4 stand-in."""
    stub = directory / "mkfs.ext4"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "mkfs $*" >> {record}\nexit 0\n'
    )
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
    name=None,
    mounts: tuple = (),
    timezone: str | None = None,
) -> VmSpec:
    return VmSpec(
        workspace_id=WID,
        name=name,
        kernel=base.parent / "vmlinux",
        rootfs=base,
        root_mib=root_mib,
        home_mib=home_mib,
        mounts=mounts,
        user_data=user_data,
        ssh_pubkey=ssh_pubkey,
        timezone=timezone,
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
    assert persist.seed_path(tmp_path, "ws1") == (
        tmp_path / "vms" / "ws1" / "seed.img"
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
    with a virtual-size is a named error, not a KeyError. The mkfs
    leg is a stub too (a Mac dev host carries no mkfs.ext4): the
    subject is the info parse, not the format."""
    stub = tmp_path / "qemu-img"
    stub.write_text('#!/bin/sh\nprintf "not json\n"\n')
    stub.chmod(0o755)
    mkfs = tmp_path / "mkfs.ext4"
    mkfs.write_text("#!/bin/sh\nexit 0\n")
    mkfs.chmod(0o755)
    base = tmp_path / "base.ext4"
    base.write_bytes(b"x")
    settings = VmmSettings(
        state_dir=tmp_path / "state",
        qemu_img=str(stub),
        mkfs_ext4=str(mkfs),
    )
    with pytest.raises(MicrovmError, match="no virtual-size"):
        await persist.ensure_artifacts(
            VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=base),
            settings,
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


def test_seed_metadata_sets_the_hostname_from_the_name() -> None:
    """NoCloud meta-data: instance-id drives cloud-init's run-once
    semantics; local-hostname is the creation name (#370), the
    minted id standing in for a nameless workspace."""
    assert persist.seed_metadata("ws-41", "dev-box") == (
        "instance-id: ws-41\nlocal-hostname: dev-box\n"
    )
    assert persist.seed_metadata("ws-41", None) == (
        "instance-id: ws-41\nlocal-hostname: ws-41\n"
    )


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
    # The nameless fallback (#370): the minted id is the hostname.
    assert "local-hostname: ws-persist\n" in log
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


async def test_seed_carries_the_creation_name(tools) -> None:
    """The hostname (#370): a named workspace's seed carries
    local-hostname: <name>, so the guest identifies itself by its
    creation name."""
    settings, record, base = tools
    await persist.ensure_artifacts(
        spec(base, user_data="#!/bin/sh\ntrue\n", name="dev-box"), settings
    )
    assert "local-hostname: dev-box\n" in record.read_text()


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
    with pytest.raises(
        MicrovmError, match="mkisofs on the cidata seed failed"
    ):
        await persist.ensure_artifacts(
            spec(base, user_data="#!/bin/sh\ntrue\n"), settings
        )
    assert not persist.seed_path(settings.state_dir, WID).exists()
    assert not persist.overlay_path(settings.state_dir, WID).exists()
    assert not persist.home_volume_path(settings.state_dir, WID).exists()
    assert not tmp_debris(settings)
    write_mkisofs_stub(mkisofs.parent, record)
    await persist.ensure_artifacts(
        spec(base, user_data="#!/bin/sh\ntrue\n"), settings
    )
    assert persist.seed_path(settings.state_dir, WID).is_file()
    assert persist.overlay_path(settings.state_dir, WID).is_file()
    assert persist.home_volume_path(settings.state_dir, WID).is_file()


# --- Home-volume export/import (#80) ---


def ext4_image(windows: list[bytes]) -> bytes:
    """A stand-in ext4 volume: the magic at 1080, then whole 1 MiB
    windows of caller-chosen bytes (zeros for sparse regions)."""
    body = bytearray(b"".join(windows))
    body[persist.EXT4_MAGIC_OFFSET : persist.EXT4_MAGIC_OFFSET + 2] = (
        persist.EXT4_MAGIC
    )
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


def holes_report_as_allocation() -> bool:
    """Whether st_blocks under-reports a holey file on this test
    filesystem: ext4 and tmpfs answer the bytes actually allocated,
    APFS the file's logical size (a three-MiB file with two MiB of
    holes stats at three MiB), so the hole-saving the import writes
    cannot be observed there. A tmp-dir probe keeps the gate on the
    filesystem's actual answer rather than the platform name."""
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "probe"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.pwrite(fd, b"x" * 4096, 0)
        os.ftruncate(fd, 3 * persist.HOME_WINDOW_B)
        os.close(fd)
        return probe.stat().st_blocks * 512 < 3 * persist.HOME_WINDOW_B


measured_holes = pytest.mark.skipif(
    not holes_report_as_allocation(),
    reason="this filesystem's st_blocks reports the logical size for "
    "holes (APFS), so the import's hole-saving is not observable",
)


@measured_holes
async def test_import_home_volume_keeps_zero_windows_sparse(tools) -> None:
    """All-zero windows become holes: a 3 MiB volume whose two last
    windows are blank costs ~1 MiB of real disk, not 3."""
    settings, _record, _base = tools
    blank = b"\0" * persist.HOME_WINDOW_B
    image = ext4_image(
        [b"data".ljust(persist.HOME_WINDOW_B, b"d"), blank, blank]
    )
    await persist.import_home_volume(
        settings.state_dir, WID, yielding([image])
    )
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
    await persist.import_home_volume(
        settings.state_dir, WID, yielding([image])
    )
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


# --- Home-volume resize and overlay growth (#184) ---


async def make_real_volume(path: Path, mib: int) -> Path:
    """A genuinely formatted ext4 volume (the resize pair's input)."""
    with path.open("wb") as handle:
        handle.truncate(mib * persist.MIB)
    await persist.run_tool(
        ["mkfs.ext4", "-q", "-F", "-L", "msks-home", str(path)],
        "mkfs of the test volume",
    )
    return path


def block_count(path: Path) -> int:
    """The filesystem's block count, straight from dumpe2fs."""
    out = subprocess.run(
        ["dumpe2fs", "-h", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in out.stdout.splitlines():
        if line.startswith("Block count:"):
            return int(line.split(":")[1])
    raise AssertionError("dumpe2fs printed no block count")


@realtools
async def test_resize_home_volume_grows(tmp_path: Path) -> None:
    settings = VmmSettings(state_dir=tmp_path)
    home = await make_real_volume(tmp_path / "home.ext4", 64)
    before = block_count(home)
    assert await persist.resize_home_volume(home, 128, settings) == "grew"
    assert home.stat().st_size == 128 * persist.MIB
    assert block_count(home) > before


@realtools
async def test_resize_home_volume_shrinks(tmp_path: Path) -> None:
    settings = VmmSettings(state_dir=tmp_path)
    home = await make_real_volume(tmp_path / "home.ext4", 128)
    before = block_count(home)
    assert await persist.resize_home_volume(home, 64, settings) == "shrank"
    assert home.stat().st_size == 64 * persist.MIB
    assert block_count(home) < before


async def test_resize_home_volume_names_a_refused_shrink(
    tmp_path: Path,
) -> None:
    """resize2fs's own refusal (an fs too full to shrink) surfaces as
    a named MicrovmError the route maps to a 409."""
    failing = tmp_path / "resize2fs"
    failing.write_text("#!/bin/sh\necho 'new size too small' >&2\nexit 1\n")
    failing.chmod(0o755)
    settings = VmmSettings(
        state_dir=tmp_path,
        resize2fs=str(failing),
        e2fsck=str(write_e2fsck_stub(tmp_path)),
    )
    home = tmp_path / "home.ext4"
    with home.open("wb") as handle:
        handle.truncate(128 * persist.MIB)
    with pytest.raises(MicrovmError, match="resize2fs"):
        await persist.resize_home_volume(home, 64, settings)
    # The file kept its size: the shrink truncates only after the
    # filesystem agreed.
    assert home.stat().st_size == 128 * persist.MIB


async def test_grow_overlay_resizes_the_qcow2(tools) -> None:
    """qemu-img resize carries the grow; the stub records and sizes."""
    settings, _record, _base = tools
    overlay = tmp_overlay(settings)
    assert await persist.grow_overlay(overlay, 20480, settings) is None
    assert overlay.stat().st_size == 20480 * persist.MIB


def tmp_overlay(settings) -> Path:
    """An overlay file at its state-dir path (the grow's target)."""
    overlay = persist.overlay_path(settings.state_dir, WID)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"")
    return overlay


async def test_resize_accepts_a_corrected_volume(tmp_path: Path) -> None:
    """e2fsck's exit-code bitmask: 1 and 2 mean errors corrected, not
    failure — a hard-stopped workspace's volume corrects on the first
    resize and the resize proceeds (#187 review)."""
    e2fsck = tmp_path / "e2fsck"
    e2fsck.write_text("#!/bin/sh\nexit 1\n")
    e2fsck.chmod(0o755)
    resize2fs = tmp_path / "resize2fs"
    resize2fs.write_text("#!/bin/sh\nexit 0\n")
    resize2fs.chmod(0o755)
    settings = VmmSettings(
        state_dir=tmp_path,
        resize2fs=str(resize2fs),
        e2fsck=str(e2fsck),
    )
    home = tmp_path / "home.ext4"
    with home.open("wb") as handle:
        handle.truncate(64 * persist.MIB)
    assert await persist.resize_home_volume(home, 128, settings) == "grew"


async def test_resize_names_an_uncorrectable_volume(tmp_path: Path) -> None:
    """e2fsck exit 4 (uncorrectable) is a real failure: a named
    MicrovmError, not a silent pass."""
    e2fsck = tmp_path / "e2fsck"
    e2fsck.write_text("#!/bin/sh\necho 'uncorrectable' >&2\nexit 4\n")
    e2fsck.chmod(0o755)
    settings = VmmSettings(
        state_dir=tmp_path,
        resize2fs=str(tmp_path / "absent"),
        e2fsck=str(e2fsck),
    )
    home = tmp_path / "home.ext4"
    with home.open("wb") as handle:
        handle.truncate(64 * persist.MIB)
    with pytest.raises(MicrovmError, match="e2fsck"):
        await persist.resize_home_volume(home, 128, settings)


CA_A = "-----BEGIN CERTIFICATE-----\nca-a\n-----END CERTIFICATE-----\n"
CA_B = "-----BEGIN CERTIFICATE-----\nca-b\n-----END CERTIFICATE-----\n"


async def test_a_seed_is_renewed_when_its_ca_marker_drifts(tools) -> None:
    """#485's migration: a seed built for another CA (a per-workspace
    pre-#485 one, or the daemon's previous certificate) is rebuilt
    carrying the daemon's, with an instance-id suffix keyed to it —
    cloud-init re-provisions, the seed script (idempotent by
    design) re-stages the certificate, and the fold re-registers.
    The marker beside the seed says which certificate it carries
    (the iso is unreadable without mounting); a match leaves the
    seed alone."""
    settings, record, base = tools
    spec_a = spec(base, ssh_pubkey="key-one")
    await persist.create_seed(spec_a, settings, 8770, CA_A)
    seed = persist.seed_path(settings.state_dir, WID)
    marker = persist.seed_ca_mark(seed.parent)
    assert marker.read_text() == persist.ca_fingerprint(CA_A)
    first = seed.stat().st_mtime_ns

    # A match: no rebuild, no re-provision.
    await persist.ensure_artifacts(spec_a, settings, 8770, CA_A)
    assert seed.stat().st_mtime_ns == first

    # A drift (the marker missing is the pre-#485 shape): renewed.
    marker.unlink()
    await persist.ensure_artifacts(spec_a, settings, 8770, CA_B)
    assert marker.read_text() == persist.ca_fingerprint(CA_B)
    log = record.read_text()
    assert CA_B in log  # the new cert staged
    # The renewal bumped instance-id: the recorded meta-data carries
    # the fingerprint suffix, so cloud-init re-provisions the
    # already-booted guest.
    suffix = persist.renewal_suffix(
        CA_B, persist.mounts_fingerprint(spec_a.mounts)
    )
    assert f"instance-id: {WID}-{suffix}" in log


def test_the_legacy_ca_names_match_the_canonical_constants() -> None:
    """The sweep's literals stay in step with interceptor.ca's
    canonical file names (persist is a storage leaf — the tie is a
    pinned literal, not an import)."""
    from msks.interceptor import ca

    assert persist.LEGACY_CA_KEY == ca.CA_KEY_FILE
    assert persist.LEGACY_CA_CERT == ca.CA_CERT_FILE


async def test_a_renewal_sweeps_the_legacy_per_workspace_ca(tools) -> None:
    """The pre-#485 pair in the workspace's directory is
    interception-capable against a guest that still trusts it;
    the renewal that supersedes it sweeps it."""
    settings, record, base = tools
    spec_a = spec(base, ssh_pubkey="key-one")
    await persist.create_seed(spec_a, settings, 8770, CA_A)
    seed = persist.seed_path(settings.state_dir, WID)
    seed.parent.mkdir(parents=True, exist_ok=True)
    (seed.parent / "interceptor-ca.key").write_bytes(b"old-key")
    (seed.parent / "interceptor-ca.crt").write_bytes(b"old-cert")
    await persist.ensure_artifacts(spec_a, settings, 8770, CA_B)
    assert not (seed.parent / "interceptor-ca.key").exists()
    assert not (seed.parent / "interceptor-ca.crt").exists()


async def test_ensure_builds_seed_for_a_port_only_workspace(
    tools,
) -> None:
    """A proxy port alone builds a seed (#483): no user_data and no
    identity, but the MSKSWS_* exports ride the same cidata
    channel."""
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base), settings, 8770)
    seed = persist.seed_path(settings.state_dir, WID)
    assert seed.is_file()
    assert "--- user-data ---\n#!/bin/sh" in record.read_text()


def two_mounts():
    """A mount set for the renewal tests: one share at two guest
    paths, one read-only."""
    from msks.spec.mounts import MountSpec

    return (
        MountSpec(host="/srv/proj", guest="/mnt/proj"),
        MountSpec(host="/srv/docs", guest="/srv/docs", mode="ro"),
    )


async def test_mounts_alone_build_a_seed(tools) -> None:
    """A workspace with nothing but mounts gets a seed (#620,
    part #623): the block carries the units, and the marker beside
    the seed names the set it baked — the empty-string value
    included, so a later first mount reads as the drift it is."""
    settings, record, base = tools
    await persist.ensure_artifacts(spec(base, mounts=two_mounts()), settings)
    seed = persist.seed_path(settings.state_dir, WID)
    assert seed.is_file()
    log = record.read_text()
    assert "mnt-proj.mount" in log
    assert "srv-docs.mount" in log
    marker = persist.seed_mounts_mark(seed.parent)
    assert marker.read_text() == persist.mounts_fingerprint(two_mounts())
    # A fresh build keeps the plain instance-id: cloud-init runs
    # once per workspace, not once per start.
    assert f"instance-id: {WID}\n" in log


async def test_a_seed_renews_when_its_mount_set_drifts(tools) -> None:
    """The per-start regeneration (#623): a start whose attachable
    mount set differs from the set the seed baked renews the seed
    — new units in, an instance-id suffix keyed to the change so
    cloud-init re-runs the script (whose prune half removes the
    dropped mounts' units), and the marker names the new set. An
    unchanged set leaves the seed untouched."""
    from msks.spec.mounts import MountSpec

    settings, record, base = tools
    mounts = two_mounts()
    grown = mounts + (MountSpec(host="/srv/extra", guest="/mnt/extra"),)
    row = spec(base, ssh_pubkey="key-one", mounts=mounts)
    await persist.create_seed(row, settings, 8770, None)
    seed = persist.seed_path(settings.state_dir, WID)
    first = seed.stat().st_mtime_ns

    # The same set: no rebuild.
    await persist.ensure_artifacts(row, settings, 8770, None)
    assert seed.stat().st_mtime_ns == first

    # A grown set: renewed, with the new unit and the suffix.
    row_grown = spec(base, ssh_pubkey="key-one", mounts=grown)
    await persist.ensure_artifacts(row_grown, settings, 8770, None)
    assert seed.stat().st_mtime_ns != first
    log = record.read_text()
    assert "mnt-extra.mount" in log
    assert (
        f"instance-id: {WID}-"
        f"{persist.renewal_suffix(None, persist.mounts_fingerprint(grown))}"
        in log
    )
    assert persist.seed_mounts_mark(seed.parent).read_text() == (
        persist.mounts_fingerprint(grown)
    )


async def test_an_emptied_mount_set_renews_with_the_prune(tools) -> None:
    """The all-mounts-removed renewal (#623): the empty set is a
    drift against the baked marker, and the rebuilt seed carries
    the prune-only block — the manifest is empty and the units'
    removal rides the script, not the seed's absence."""
    settings, record, base = tools
    row = spec(base, ssh_pubkey="key-one", mounts=two_mounts())
    await persist.create_seed(row, settings, 8770, None)
    seed = persist.seed_path(settings.state_dir, WID)

    emptied = spec(base, ssh_pubkey="key-one")
    await persist.ensure_artifacts(emptied, settings, 8770, None)
    assert persist.seed_mounts_mark(seed.parent).read_text() == (
        persist.mounts_fingerprint(())
    )
    log = record.read_text()
    # The prune-only block: an empty manifest beside the prune
    # loop, and a new suffix so cloud-init re-runs it.
    assert "<<'MSEOF'\nMSEOF" in log
    assert 'systemctl stop "${u##*/}"' in log
    assert "sh '/var/lib/cloud/scripts/per-boot/50-msks-mounts'" in log


async def test_a_pre_mounts_seed_is_left_alone_until_mounts_arrive(
    tools,
) -> None:
    """Upgrade shape (#623): a seed built before the marker existed
    (every pre-part-3 workspace) never renews for the marker's own
    sake — a mountless workspace keeps its create-once seed. The
    first mount is the drift that bakes the marker."""
    settings, record, base = tools
    row = spec(base, ssh_pubkey="key-one")
    await persist.create_seed(row, settings, 8770, None)
    seed = persist.seed_path(settings.state_dir, WID)
    persist.seed_mounts_mark(seed.parent).unlink()  # the pre-#623 shape
    first = seed.stat().st_mtime_ns

    await persist.ensure_artifacts(row, settings, 8770, None)
    assert seed.stat().st_mtime_ns == first

    row_mounted = spec(base, ssh_pubkey="key-one", mounts=two_mounts())
    await persist.ensure_artifacts(row_mounted, settings, 8770, None)
    assert seed.stat().st_mtime_ns != first
    assert persist.seed_mounts_mark(seed.parent).read_text() == (
        persist.mounts_fingerprint(two_mounts())
    )


async def test_the_timezone_alone_builds_a_seed(tools) -> None:
    """The host's timezone captured at create (#633): a zone name
    alone gives the workspace a seed whose script sets the guest's
    clock — and a bare workspace on a UTC host (timezone None)
    keeps the seedless boot, as the no-user_data test pins."""
    settings, record, base = tools
    await persist.ensure_artifacts(
        spec(base, timezone="Europe/Berlin"), settings
    )
    seed = persist.seed_path(settings.state_dir, WID)
    assert seed.is_file()
    log = record.read_text()
    assert "timedatectl set-timezone 'Europe/Berlin'" in log
    assert "ln -nsf /usr/share/zoneinfo/Europe/Berlin /etc/localtime" in log
