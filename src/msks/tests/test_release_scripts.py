"""Tests over the release-workflow helpers (#564):
``scripts/release_notes.py`` and ``scripts/image_meta.py`` —
the pure logic the workflow's shell drives.
"""

import importlib.util
import json
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_notes = load("release_notes")
image_meta = load("image_meta")

CHANGELOG = """\
# Changelog

## \\[Unreleased]

### Added

- nothing yet

## \\[v1.2.3] - 2026-10-18

### Fixed

- the fix body, first line

second paragraph.

## \\[v1.2.2] - 2026-10-01

### Added

- older
"""


def test_extract_reads_the_escaped_section_with_its_date() -> None:
    """The changelog escapes the opening bracket (#3142); the
    match strips the escape, prefixes on the bracketed version,
    and takes the body up to the next heading."""
    section = release_notes.extract("v1.2.3", CHANGELOG)
    assert "### Fixed" in section
    assert "the fix body, first line" in section
    assert "second paragraph." in section
    assert "older" not in section
    assert "nothing yet" not in section


def test_extract_matches_an_undated_heading() -> None:
    text = "## [v0.9.0]\n\nbody only\n\n## [other]\n"
    assert "body only" in release_notes.extract("v0.9.0", text)


def test_a_missing_section_refuses() -> None:
    """A tag without its changelog entry stops the workflow: the
    human-authored section is the reason the file exists."""
    with pytest.raises(ValueError, match="no '## \\[v9.9.9]' section"):
        release_notes.extract("v9.9.9", CHANGELOG)


def test_the_cli_writes_the_section(tmp_path: Path, capsys) -> None:
    path = tmp_path / "changes.md"
    path.write_text(CHANGELOG)
    assert release_notes.main(["v1.2.3", str(path)]) == 0
    out = capsys.readouterr().out
    assert "the fix body" in out
    assert release_notes.main(["v9.9.9", str(path)]) == 1
    assert "cut the release in the changelog first" in capsys.readouterr().err


def build_guest_dir(
    root: Path,
    name: str = "debian",
    version: str = "13.6",
    arch: str | None = None,
) -> Path:
    """A guest dir shaped like msks-build-guest's output: the
    archive-name marker and a containerDisk archive whose layer
    carries a description-bearing image.json (an arch-bearing one
    when *arch* names the guest's)."""
    guest = root / "guest"
    guest.mkdir()
    archive_name = f"workspace-{name}-{version}.tar"
    (guest / "image-archive-name").write_text(archive_name)
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        image = {
            "schema": 2,
            "name": name,
            "version": version,
            "cmdline": "console=ttyS0",
            "kernel_version": "6.12.107",
            "description": f"{name.title()} {version}, kernel 6.12.107",
        }
        if arch is not None:
            image["arch"] = arch
        blob = json.dumps(image).encode()
        info = tarfile.TarInfo("./disk/image.json")
        info.size = len(blob)
        tar.addfile(info, BytesIO(blob))
    layer.seek(0)
    with tarfile.open(guest / archive_name, "w") as outer:
        info = tarfile.TarInfo("layer.tar")
        info.size = layer.getbuffer().nbytes
        outer.addfile(info, layer)
        manifest = json.dumps([{"Layers": ["layer.tar"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        outer.addfile(info, BytesIO(manifest))
    return guest


def test_image_meta_reads_the_archive(tmp_path: Path) -> None:
    """The channel row's facts come from the archive itself — the
    same self-describing read the import performs."""
    meta = image_meta.meta(build_guest_dir(tmp_path))
    assert meta == {
        "ref": "debian:13.6",
        "description": "Debian 13.6, kernel 6.12.107",
        "kernel_version": "6.12.107",
        "arch": "",
        "archive": "workspace-debian-13.6.tar",
    }


def test_image_meta_reads_the_declared_arch(tmp_path: Path) -> None:
    """The arch the manifest declares rides the channel row (#637):
    an aarch64 build's row names its arch, an older image without
    the field answers the empty string."""
    meta = image_meta.meta(build_guest_dir(tmp_path, arch="aarch64"))
    assert meta["arch"] == "aarch64"


def test_image_meta_answers_the_cli_shape(tmp_path, capsys) -> None:
    guest = build_guest_dir(tmp_path, name="nixos", version="26.05")
    assert image_meta.main([str(guest)]) == 0
    assert json.loads(capsys.readouterr().out)["ref"] == "nixos:26.05"


def test_image_meta_names_a_missing_archive(tmp_path: Path) -> None:
    empty = tmp_path / "guest"
    empty.mkdir()
    (empty / "image-archive-name").write_text("workspace-x-1.tar")
    with pytest.raises(SystemExit, match="no such archive"):
        image_meta.archive_in(empty)


def test_the_scripts_run_as_processes(tmp_path: Path) -> None:
    """The workflow drives both scripts as processes (#564 round-2
    review): the __main__ guard is what makes the changelog gate
    refuse and the meta reader answer — pinned by running them the
    way the workflow does."""
    import subprocess

    changelog = tmp_path / "changes.md"
    changelog.write_text(CHANGELOG)
    ok = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "release_notes.py"),
            "v1.2.3",
            str(changelog),
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert "the fix body" in ok.stdout
    refused = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "release_notes.py"),
            "v9.9.9",
            str(changelog),
        ],
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 1
    assert "cut the release in the changelog first" in refused.stderr

    guest = build_guest_dir(tmp_path)
    meta = subprocess.run(
        [sys.executable, str(SCRIPTS / "image_meta.py"), str(guest)],
        capture_output=True,
        text=True,
    )
    assert meta.returncode == 0
    assert json.loads(meta.stdout)["ref"] == "debian:13.6"
