"""Tests for msks.guestassets — discovery of the nix-built guest
assets (#5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from msks.guestassets import GuestAssets

from msks import guestassets


def guest_dir(root: Path) -> Path:
    """The default guest state dir below ``root`` (no env override)."""
    return root / ".devenv" / "state" / "guest"


def write_manifest(
    root: Path, *, guest: Path | None = None, **overrides: object
) -> None:
    """Write a valid manifest into ``root``'s guest state dir, then
    patch fields (``guest`` targets a different dir — the
    relocation tests)."""
    guest = guest if guest is not None else guest_dir(root)
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
    (guest / "guest-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_load_returns_assets(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets == GuestAssets(
        vmlinux=guest_dir(tmp_path) / "vmlinux",
        initrd=guest_dir(tmp_path) / "initrd",
        rootfs=guest_dir(tmp_path) / "rootfs.ext4",
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
    guest = guest_dir(tmp_path)
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").write_text("not json", encoding="utf-8")
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_unreadable_manifest(tmp_path: Path) -> None:
    # A directory in place of the file: reading it raises OSError
    # (EISDIR) for every user — root included, unlike a chmod 000 file.
    guest = guest_dir(tmp_path)
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").mkdir()
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_manifest_is_not_a_mapping(tmp_path: Path) -> None:
    guest = guest_dir(tmp_path)
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
    (guest_dir(tmp_path) / "rootfs.ext4").unlink()
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
    assert assets.rootfs == guest_dir(tmp_path) / "rootfs.ext4"


def test_load_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path)
    monkeypatch.delenv("DEVENV_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert guestassets.load_guest_assets() is not None


# --- the MSKS_GUEST_DIR relocation (#156) -----------------------------


def test_guest_dir_default(tmp_path: Path) -> None:
    assert guestassets.guest_dir(tmp_path) == guest_dir(tmp_path)


def test_guest_dir_env_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, "/elsewhere/guest")
    assert guestassets.guest_dir(tmp_path) == Path("/elsewhere/guest")


def test_guest_dir_env_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, "other-guest")
    assert guestassets.guest_dir(tmp_path) == tmp_path / "other-guest"


def test_load_honors_guest_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSKS_GUEST_DIR relocates everything the loader reads."""
    moved = tmp_path / "relocated"
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, str(moved))
    write_manifest(tmp_path, guest=moved)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets is not None
    assert assets.rootfs == moved / "rootfs.ext4"


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


# --- the image-baked agent toolchain (#266) ----------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_the_image_ships_the_pi_extension() -> None:
    """The model-discovery extension rides the guest image (#266):
    the file exists beside the image build, reads the MSKSWS_*
    pair the seed exports (never a vendor-shaped name), resolves
    its credential per request from the seeded token file, and
    carries the klangk behavior — provider registration, the
    embed/rerank filter, the quiet no-op when the environment
    names no proxy."""
    ext = (REPO_ROOT / "nix" / "guest-pi-extension.ts").read_text()
    assert "process.env.MSKSWS_BASE_URL" in ext
    assert "process.env.MSKSWS_API_KEY" in ext
    assert "OPENAI_API_KEY" not in ext
    assert 'apiKey: "!cat /etc/msks/llm.token"' in ext
    assert 'pi.registerProvider("msks"' in ext
    assert '"embed"' in ext and '"rerank"' in ext


def test_the_image_bakes_the_agent_toolchain() -> None:
    """The toolchain pins and their staging (#266): the build fetches
    the pinned Node tarball and the pinned pi package by digest,
    builds pi offline against its shrinkwrap, and stages both into
    the overlay's /usr/local with the extension planted for root
    and in the skeleton every seed-provisioned account copies."""
    build = (REPO_ROOT / "nix" / "guest-debian.nix").read_text()
    assert (
        "https://nodejs.org/dist/v22.23.3/"
        "node-v22.23.3-linux-x64.tar.gz" in build.replace('"\n      + "', "")
    )
    assert "pi-coding-agent-0.87.1.tgz" in build
    # A real npmDepsHash, not the placeholder the two-step prefetch
    # starts from.
    assert "AAAAAAAAAAAAAAAAAAAAAAAA" not in build
    assert "$out/etc/skel/.pi/agent/extensions/llm-models.ts" in build
    assert "$out/root/.pi/agent/extensions/llm-models.ts" in build
    assert "$out/usr/local/bin/pi" in build
