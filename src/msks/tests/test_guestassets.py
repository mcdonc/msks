"""Tests for msks.guestassets — discovery of the nix-built guest assets (#5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from msks.guestassets import GuestAssets

from msks import guestassets


def write_manifest(root: Path, **overrides: object) -> None:
    """Write a valid manifest into ``root/.guest/``, then patch fields."""
    guest = root / ".guest"
    guest.mkdir(parents=True, exist_ok=True)
    for name in ("vmlinux", "initrd", "rootfs.ext4"):
        (guest / name).write_bytes(b"artifact")
    manifest = {
        "schema": 1,
        "kernel_version": "6.18.50",
        "kernel_format": "bzImage",
        "cmdline": "console=ttyS0 root=/dev/vda ro",
        "vmlinux": "vmlinux",
        "initrd": "initrd",
        "rootfs": "rootfs.ext4",
    }
    manifest.update(overrides)
    (guest / "guest-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_load_returns_assets(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets == GuestAssets(
        vmlinux=tmp_path / ".guest" / "vmlinux",
        initrd=tmp_path / ".guest" / "initrd",
        rootfs=tmp_path / ".guest" / "rootfs.ext4",
        cmdline="console=ttyS0 root=/dev/vda ro",
        kernel_version="6.18.50",
    )


def test_load_without_initrd(tmp_path: Path) -> None:
    write_manifest(tmp_path, initrd=None)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets is not None
    assert assets.initrd is None


def test_load_missing_manifest(tmp_path: Path) -> None:
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_invalid_json(tmp_path: Path) -> None:
    guest = tmp_path / ".guest"
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").write_text("not json", encoding="utf-8")
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_unreadable_manifest(tmp_path: Path) -> None:
    # A directory in place of the file: reading it raises OSError
    # (EISDIR) for every user — root included, unlike a chmod 000 file.
    guest = tmp_path / ".guest"
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").mkdir()
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_manifest_is_not_a_mapping(tmp_path: Path) -> None:
    guest = tmp_path / ".guest"
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").write_text("[1, 2]", encoding="utf-8")
    assert guestassets.load_guest_assets(tmp_path) is None


@pytest.mark.parametrize("schema", [2, None, "1"])
def test_load_unknown_schema(tmp_path: Path, schema: object) -> None:
    write_manifest(tmp_path, schema=schema)
    assert guestassets.load_guest_assets(tmp_path) is None


@pytest.mark.parametrize("name", ["../vmlinux", "sub/vmlinux", "/etc/passwd"])
def test_load_rejects_artifact_names_outside_guest_dir(
    tmp_path: Path, name: str
) -> None:
    write_manifest(tmp_path, vmlinux=name)
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_missing_artifact_file(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    (tmp_path / ".guest" / "rootfs.ext4").unlink()
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_non_string_fields(tmp_path: Path) -> None:
    write_manifest(tmp_path, cmdline=7, kernel_version=False)
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_defaults_to_devenv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path)
    monkeypatch.setenv("DEVENV_ROOT", str(tmp_path))
    assets = guestassets.load_guest_assets()
    assert assets is not None
    assert assets.rootfs == tmp_path / ".guest" / "rootfs.ext4"


def test_load_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_manifest(tmp_path)
    monkeypatch.delenv("DEVENV_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert guestassets.load_guest_assets() is not None


def test_kvm_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: True)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: True)
    assert guestassets.kvm_available()


def test_kvm_available_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: False)
    assert not guestassets.kvm_available()


def test_kvm_available_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: True)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: False)
    assert not guestassets.kvm_available()


def _assets(**kwargs: object) -> GuestAssets:
    fields = {
        "vmlinux": Path("/a/vmlinux"),
        "initrd": Path("/a/initrd"),
        "rootfs": Path("/a/rootfs.ext4"),
        "cmdline": "console=ttyS0",
        "kernel_version": "6.18.50",
    }
    fields.update(kwargs)
    return GuestAssets(**fields)  # type: ignore[arg-type]


def _force_kvm(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: usable)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: usable)


def test_smoke_env_with_assets_and_kvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_kvm(monkeypatch, True)
    env = guestassets.smoke_env_defaults(_assets())
    assert env == {
        "MSKSD_TEST_VMLINUX": "/a/vmlinux",
        "MSKSD_TEST_INITRD": "/a/initrd",
        "MSKSD_TEST_ROOTFS": "/a/rootfs.ext4",
        "MSKSD_TEST_CMDLINE": "console=ttyS0",
    }


def test_smoke_env_without_initrd(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, True)
    env = guestassets.smoke_env_defaults(_assets(initrd=None))
    assert "MSKSD_TEST_INITRD" not in env


def test_smoke_env_without_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, True)
    assert guestassets.smoke_env_defaults(None) == {}


def test_smoke_env_without_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, False)
    assert guestassets.smoke_env_defaults(_assets()) == {}


def test_load_runner_image(tmp_path: Path) -> None:
    guest = tmp_path / ".guest"
    guest.mkdir()
    (guest / "runner-image.json").write_text(
        json.dumps({"image": "msks-vm-runner:dev"}), encoding="utf-8"
    )
    assert guestassets.load_runner_image(tmp_path) == "msks-vm-runner:dev"


@pytest.mark.parametrize("body", ["", "[]", '{"image": ""}'])
def test_load_runner_image_invalid(tmp_path: Path, body: str) -> None:
    guest = tmp_path / ".guest"
    guest.mkdir()
    (guest / "runner-image.json").write_text(body, encoding="utf-8")
    assert guestassets.load_runner_image(tmp_path) is None


def test_load_runner_image_defaults_to_devenv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = tmp_path / ".guest"
    guest.mkdir()
    (guest / "runner-image.json").write_text(
        json.dumps({"image": "msks-vm-runner:dev"}), encoding="utf-8"
    )
    monkeypatch.setenv("DEVENV_ROOT", str(tmp_path))
    assert guestassets.load_runner_image() == "msks-vm-runner:dev"
