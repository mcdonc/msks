"""The workspace image catalog (#40).

Images are OCI archives in the containerDisk convention: one layer
whose root carries ``boot/vmlinuz``, ``boot/initrd.img``,
``disk/rootfs.ext4``, and ``disk/image.json`` (schema 2). The store
lives under ``<state_dir>/images``:

- ``<sha256>/`` — the per-hash boot-file cache: the unpacked kernel,
  initrd, rootfs, and the image manifest. Workspace launches read
  only these; nothing unpacks on the launch path.
- ``archive-<sha256>.tar`` — the imported archive itself (the
  artifact users copy around and export).
- ``default`` — a one-line file naming the default image's hash.

The catalog is the filesystem: directories are self-describing
(``image.json`` inside), hash-keyed, and crash-safe — an interrupted
import leaves a cache dir without a complete file set, which
``list_images`` ignores until the import re-runs.
"""

import hashlib
import json
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

BOOT_MEMBERS = {
    "kernel": "boot/vmlinuz",
    "initrd": "boot/initrd.img",
    "rootfs": "disk/rootfs.ext4",
    "manifest": "disk/image.json",
}


class ImageError(Exception):
    """A catalog operation failed; the message is operator-readable."""


@dataclass(frozen=True)
class ImageRecord:
    """One catalog entry; every path is absolute and launch-ready."""

    hash: str
    name: str
    version: str
    cmdline: str
    vsock_shell_port: int
    kernel: Path
    initrd: Path
    rootfs: Path

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.version}"


def _images_dir(state_dir: Path) -> Path:
    return state_dir / "images"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_layer_name(archive: tarfile.TarFile) -> str:
    """The first layer path from an OCI archive's manifest."""
    manifest_file = archive.extractfile("manifest.json")
    if manifest_file is None:
        raise ImageError("no manifest.json: not an OCI archive")
    try:
        layers = json.load(manifest_file)
    except json.JSONDecodeError as exc:
        raise ImageError(f"malformed OCI archive: {exc}") from exc
    return _layer_of(layers)


def _layer_of(layers) -> str:
    if isinstance(layers, list) and layers:
        names = layers[0].get("Layers")
        if names:
            return names[0]
    raise ImageError("OCI manifest carries no layers")


def _read_archive(path: Path) -> tuple[dict, tarfile.TarFile]:
    """Open the containerDisk layer of an OCI archive."""
    try:
        archive = tarfile.open(path)
    except (tarfile.TarError, OSError) as exc:
        raise ImageError(f"not a tar archive: {path} ({exc})") from exc
    try:
        layer_name = _first_layer_name(archive)
        layer_file = archive.extractfile(layer_name)
        if layer_file is None:
            raise ImageError("layer member missing from archive")
        return {}, tarfile.open(fileobj=layer_file)
    except (KeyError, tarfile.TarError) as exc:
        raise ImageError(f"malformed OCI archive: {exc}") from exc


def _validate_manifest(layer: tarfile.TarFile) -> dict:
    member = _extract(layer, BOOT_MEMBERS["manifest"])
    if member is None:
        raise ImageError("no disk/image.json in the layer: not a containerDisk")
    try:
        raw = json.load(member)
    except json.JSONDecodeError as exc:
        raise ImageError(f"image.json is not JSON: {exc}") from exc
    if raw.get("schema") != 2:
        raise ImageError(f"image.json schema {raw.get('schema')!r}, expected 2")
    _require_fields(raw)
    return raw


def _require_fields(raw: dict) -> None:
    """Raise unless every schema-2 field is present."""
    missing = [
        field
        for field in ("name", "version", "cmdline", "vsock_shell_port")
        if field not in raw
    ]
    if missing:
        raise ImageError(f"image.json missing {missing[0]!r}")


def _extract(layer: tarfile.TarFile, member_name: str):
    """A member's file object, or None when absent or non-regular."""
    try:
        handle = layer.extractfile(f"./{member_name}")
        if handle is None:
            handle = layer.extractfile(member_name)
    except KeyError:
        handle = None
    return handle


def _member(layer: tarfile.TarFile, dest: Path, member_name: str) -> None:
    """Extract one member to dest; ImageError when missing."""
    handle = _extract(layer, member_name)
    if handle is None:
        raise ImageError(f"containerDisk member missing: {member_name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(handle, out)


def import_archive(path: Path, state_dir: Path) -> ImageRecord:
    """Register one archive: hash it, unpack the boot files, index.

    Idempotent: re-importing the same archive refreshes the cache in
    place and returns the existing identity.
    """
    if not path.is_file():
        raise ImageError(f"no such image archive: {path}")
    digest = _hash_file(path)
    cache = _images_dir(state_dir) / digest
    _, layer = _read_archive(path)
    try:
        manifest = _validate_manifest(layer)
        # Extract into a sibling and swap: an interrupted import must
        # never leave a half-populated cache that looks complete.
        staging = cache.with_name(f".{digest}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        _member(layer, staging / "kernel", BOOT_MEMBERS["kernel"])
        _member(layer, staging / "initrd", BOOT_MEMBERS["initrd"])
        _member(layer, staging / "rootfs.ext4", BOOT_MEMBERS["rootfs"])
        (staging / "image.json").write_text(json.dumps(manifest))
        if cache.exists():
            shutil.rmtree(cache)
        staging.rename(cache)
    finally:
        layer.close()
    archive_dest = _images_dir(state_dir) / f"archive-{digest}.tar"
    if not archive_dest.exists():
        shutil.copy2(path, archive_dest)
    return _record_from(cache, digest, manifest)


def _record_from(cache: Path, digest: str, manifest: dict) -> ImageRecord:
    return ImageRecord(
        hash=digest,
        name=manifest["name"],
        version=str(manifest["version"]),
        cmdline=manifest["cmdline"],
        vsock_shell_port=int(manifest["vsock_shell_port"]),
        kernel=cache / "kernel",
        initrd=cache / "initrd",
        rootfs=cache / "rootfs.ext4",
    )


def _load_record(cache: Path) -> ImageRecord | None:
    manifest_path = cache / "image.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None
    for member in ("kernel", "initrd", "rootfs.ext4"):
        if not (cache / member).is_file():
            return None
    return _record_from(cache, cache.name, manifest)


def _cache_entries(root: Path) -> list[Path]:
    """Hash-keyed directories only (staging dirs are dot-prefixed)."""
    if not root.is_dir():
        return []
    return [
        entry
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def list_images(state_dir: Path) -> list[ImageRecord]:
    """Every complete cache entry, sorted by reference."""
    root = _images_dir(state_dir)
    records = [
        record
        for entry in _cache_entries(root)
        if (record := _load_record(entry)) is not None
    ]
    return sorted(records, key=lambda r: (r.name, r.version))


def _resolve_hash(ref: str, images: list) -> ImageRecord | None:
    by_hash = {image.hash: image for image in images}
    return by_hash.get(ref)


def _resolve_name_version(ref: str, images: list) -> ImageRecord | None:
    name, _, version = ref.partition(":")
    for image in images:
        if image.name == name and image.version == version:
            return image
    return None


def _resolve_newest(name: str, images: list) -> ImageRecord | None:
    candidates = [image for image in images if image.name == name]
    return max(candidates, key=lambda i: i.version, default=None)


def resolve(ref: str, state_dir: Path) -> ImageRecord | None:
    """A catalog reference: hash, name, or name:version.

    A bare name resolves to its newest version (versions sort
    lexically; importers are expected to use sortable versions).
    """
    images = list_images(state_dir)
    if len(ref) == 64 and all(c in "0123456789abcdef" for c in ref):
        return _resolve_hash(ref, images)
    if ":" in ref:
        return _resolve_name_version(ref, images)
    return _resolve_newest(ref, images)


def set_default(digest: str, state_dir: Path) -> None:
    root = _images_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "default").write_text(digest + "\n")


def default_image(state_dir: Path) -> ImageRecord | None:
    """The designated default, or the sole catalog entry, or None."""
    root = _images_dir(state_dir)
    pointer = root / "default"
    if pointer.is_file():
        record = _load_record(root / pointer.read_text().strip())
        if record is not None:
            return record
    images = list_images(state_dir)
    if len(images) == 1:
        return images[0]
    return None
