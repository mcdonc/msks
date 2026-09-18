"""Discovery of the nix-built guest VM assets (#5).

``devenv tasks run msks:build-guest`` builds the kernel, initrd, and
ext4 rootfs with nix and copies them next to a JSON manifest under
``.guest/`` at the repository root. This module resolves that manifest
so smoke tests (and, later, the CLI) use the built artifacts without
hand-exported environment variables. Explicitly exported
``MSKSD_TEST_*`` variables always keep precedence over anything
discovered here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(".guest") / "guest-manifest.json"
RUNNER_IMAGE_PATH = Path(".guest") / "runner-image.json"

VMLINUX_ENV = "MSKSD_TEST_VMLINUX"
INITRD_ENV = "MSKSD_TEST_INITRD"
ROOTFS_ENV = "MSKSD_TEST_ROOTFS"
CMDLINE_ENV = "MSKSD_TEST_CMDLINE"
RUNNER_IMAGE_ENV = "MSKSD_TEST_RUNNER_IMAGE"

#: Directory holding the manifest and the artifacts it names.
GUEST_DIR = ".guest"


@dataclass(frozen=True)
class GuestAssets:
    """A fully built guest: where the artifacts are and how to boot them."""

    vmlinux: Path
    initrd: Path | None
    rootfs: Path
    cmdline: str
    kernel_version: str


def _root() -> Path:
    """Repository root: the devenv exports ``DEVENV_ROOT`` for tasks/tests."""
    return Path(os.environ.get("DEVENV_ROOT", "."))


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _artifact(base: Path, value: object) -> Path | None:
    """Resolve a manifest-named artifact below ``base`` that exists.

    Names must be plain filenames within the guest directory: the
    manifest is a local, gitignored build product, but a stray
    ``../`` in a hand-edited one should not resolve outside it.
    """
    name = _as_str(value)
    if name is None or name.startswith("/") or "/" in name:
        return None
    path = base / GUEST_DIR / name
    return path if path.is_file() else None


def _load_manifest(base: Path) -> dict | None:
    """The parsed guest manifest, ``None`` unless it is a schema-1 dict."""
    try:
        raw = json.loads((base / MANIFEST_PATH).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if isinstance(raw, dict) and raw.get("schema") == 1:
        return raw
    return None


def _guest_assets(base: Path, raw: dict) -> GuestAssets | None:
    """Build the asset record, ``None`` unless every required piece exists."""
    vmlinux = _artifact(base, raw.get("vmlinux"))
    rootfs = _artifact(base, raw.get("rootfs"))
    cmdline = _as_str(raw.get("cmdline"))
    version = _as_str(raw.get("kernel_version"))
    if None in (vmlinux, rootfs, cmdline, version):
        return None
    return GuestAssets(
        vmlinux=vmlinux,
        initrd=_artifact(base, raw.get("initrd")),
        rootfs=rootfs,
        cmdline=cmdline,
        kernel_version=version,
    )


def load_guest_assets(root: Path | None = None) -> GuestAssets | None:
    """Return the built guest assets below ``root`` (default ``$DEVENV_ROOT``).

    ``None`` when the manifest is absent, unreadable, malformed, or
    names artifacts that are no longer on disk — callers treat that as
    "the guest was never built" and skip.
    """
    base = root if root is not None else _root()
    raw = _load_manifest(base)
    if raw is None:
        return None
    return _guest_assets(base, raw)


def kvm_available() -> bool:
    """Whether ``/dev/kvm`` exists and this user may read and write it."""
    return os.path.exists("/dev/kvm") and os.access(
        "/dev/kvm", os.R_OK | os.W_OK
    )


def smoke_env_defaults(assets: GuestAssets | None) -> dict[str, str]:
    """``MSKSD_TEST_*`` defaults exposing built assets to the smoke tests.

    Empty unless the assets exist and ``/dev/kvm`` is usable: the smoke
    tests then still skip themselves when the guest was never built or
    the host cannot run VMs.
    """
    if assets is None or not kvm_available():
        return {}
    env = {
        # Absolute paths: a relative rootfs would resolve against the
        # per-test state dir once qemu-img binds it as the overlay's
        # backing file (backing paths are overlay-relative), so the
        # smoke tests must hand the daemon the resolved location.
        VMLINUX_ENV: str(_absolute(assets.vmlinux)),
        ROOTFS_ENV: str(_absolute(assets.rootfs)),
        CMDLINE_ENV: assets.cmdline,
    }
    if assets.initrd is not None:
        env[INITRD_ENV] = str(_absolute(assets.initrd))
    return env


def _absolute(path: Path) -> Path:
    """The manifest artifact as an absolute, symlink-free path."""
    return path.resolve()


def load_runner_image(root: Path | None = None) -> str | None:
    """Image reference of the built vm-runner archive below ``root``, if any.

    Written by ``devenv tasks run msks:build-runner-image`` once the
    container archive is ready to import on the k3s node.
    """
    base = root if root is not None else _root()
    try:
        raw = json.loads(
            (base / RUNNER_IMAGE_PATH).read_text(encoding="utf-8")
        )
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return _as_str(raw.get("image"))
