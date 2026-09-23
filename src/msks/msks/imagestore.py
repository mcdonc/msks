"""The workspace image catalog (#40).

Images are container-image tars (`podman load` compatible) in
the containerDisk convention: one layer
whose root carries ``boot/vmlinuz``, ``boot/initrd.img``,
``disk/rootfs.ext4``, and ``disk/image.json`` (schema 2). The store
lives under ``<state_dir>/images``:

- ``<sha256>/`` — the per-hash boot-file cache: the unpacked kernel,
  initrd, rootfs, the image manifest, and an ``imported`` stamp
  (when the archive last came in, #186). Workspace launches read
  only these; nothing unpacks on the launch path.
- ``archive-<sha256>.tar`` — the imported archive itself (the
  artifact users copy around and export).
- ``default`` — a one-line file naming the default image's hash.

The catalog is the filesystem: directories are self-describing
(``image.json`` inside), hash-keyed, and crash-safe — an interrupted
import leaves a cache dir without a complete file set, which
``list_images`` ignores until the import re-runs.
"""

import contextlib
import hashlib
import json
import os
import shutil
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

#: The cache file that carries an entry's import time (#186): one
#: ISO-8601 line, rewritten by every import (a re-import refreshes
#: the stamp along with the cache it swaps in).
IMPORTED_STAMP = "imported"

#: The floor of import-time ordering: entries whose time is
#: unreadable sort deterministically — first, before every
#: stamped entry.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

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
    kernel_version: str
    kernel_format: str
    kernel: Path
    initrd: Path
    rootfs: Path
    #: When the entry last came in (#186): the stamp the import
    #: writes, or the cache's filesystem time for entries that
    #: predate stamps. Sorts same-reference entries oldest-first.
    imported: datetime | None = None
    # The image's declared first-boot provisioner (#41): None (the
    # field is absent) or one of PROVISIONERS.
    provisioner: str | None = None
    #: The guest console protocol (#63): "prelude-v1" images negotiate
    #: the user and window size in-band; "legacy" images (and every
    #: image whose manifest predates the field) speak raw bytes.
    console_protocol: str = "legacy"
    #: The users the image's console will serve; the daemon validates
    #: --user against this list before anything reaches the guest.
    console_users: tuple[str, ...] = ("root",)

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.version}"


def images_dir(state_dir: Path) -> Path:
    return state_dir / "images"


def is_url(source: str) -> bool:
    """Whether an import source is a URL rather than a host path
    (#258). A scheme separator is the marker: workspace paths never
    carry one."""
    return "://" in source


def stream_to(response, dest: Path, max_bytes: int, url: str) -> int:
    """Write the open response's body to ``dest``, enforcing the
    ceiling while it streams; returns the byte count. A failure at
    any point removes the partial file — the caller never sees a
    half-written staging name."""
    try:
        size = 0
        with dest.open("wb") as sink:
            for chunk in response.iter_bytes(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise ImageError(
                        f"image download passed the {max_bytes}-byte "
                        f"ceiling: {url}"
                    )
                sink.write(chunk)
        if size == 0:
            raise ImageError(f"image download is empty: {url}")
        return size
    except BaseException:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise


def require_https(url: str) -> None:
    """Refuse a source whose scheme is not https, by name."""
    scheme = urlparse(url).scheme
    if scheme == "https":
        return
    named = scheme or "no scheme"
    raise ImageError(f"image source must be https://, got {named}: {url}")


def check_response(response, url: str, max_bytes: int) -> None:
    """Refuse, by name, a download that is not a plain https 200 —
    a bad status, a redirect chain that left https behind, or a
    declared length over the ceiling (checked before a byte of the
    body is read)."""
    if response.status_code != 200:
        code = response.status_code
        raise ImageError(f"image download answered {code}: {url}")
    if str(response.url).startswith("http://"):
        raise ImageError(f"image download redirected away from https: {url}")
    declared = 0
    with contextlib.suppress(ValueError):
        declared = int(response.headers.get("content-length") or 0)
    if declared > max_bytes:
        raise ImageError(
            f"image download declares {declared} bytes, over the "
            f"{max_bytes}-byte ceiling: {url}"
        )


def fetch_archive(
    url: str,
    state_dir: Path,
    *,
    timeout_s: float,
    max_bytes: int,
    transport=None,
) -> Path:
    """Download an ``https://`` image archive into the catalog's
    staging area (#258).

    Returns the staged copy's path — dot-prefixed, so a crash's
    leftovers are swept at the next startup — for the caller to
    import and unlink. The download verifies TLS against the
    system roots and refuses, by name: non-https schemes (a
    redirect downgrade included), non-200 statuses, empty bodies,
    and bodies over ``max_bytes``.
    """
    require_https(url)
    root = images_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f".dl-{os.getpid()}-{uuid.uuid4().hex[:8]}.tar"
    try:
        with httpx.Client(
            verify=True,
            follow_redirects=True,
            timeout=timeout_s,
            transport=transport,
        ) as client:
            with client.stream("GET", url) as response:
                check_response(response, url, max_bytes)
                stream_to(response, dest, max_bytes, url)
        return dest
    except httpx.HTTPError as exc:
        raise ImageError(f"image download failed: {exc}") from exc
    except OSError as exc:
        raise ImageError(f"cannot write {dest}: {exc}") from exc
    finally:
        if not dest.is_file():
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first_layer_name(archive: tarfile.TarFile) -> str:
    """The first layer path from a container image's manifest."""
    manifest_file = archive.extractfile("manifest.json")
    if manifest_file is None:
        raise ImageError("no manifest.json: not a container image")
    try:
        layers = json.load(manifest_file)
    except json.JSONDecodeError as exc:
        raise ImageError(f"malformed container image: {exc}") from exc
    return layer_of(layers)


def layer_of(layers) -> str:
    if (
        isinstance(layers, list)
        and layers
        and isinstance(layers[0], dict)
        and layers[0].get("Layers")
    ):
        return layers[0]["Layers"][0]
    raise ImageError("image manifest carries no layers")


@contextlib.contextmanager
def open_layer(path: Path) -> Iterator[tarfile.TarFile]:
    """Open the containerDisk layer of a container-image tar.

    The layer tar lives *inside* the outer archive, so both must stay
    open while reading; the outer archive (and with it the file
    handle on the staged source) closes deterministically on exit.
    """
    try:
        archive = tarfile.open(path)
    except (tarfile.TarError, OSError) as exc:
        raise ImageError(f"not a tar archive: {path} ({exc})") from exc
    try:
        try:
            layer_name = first_layer_name(archive)
            layer_file = archive.extractfile(layer_name)
            if layer_file is None:
                raise ImageError("layer member missing from archive")
            layer = tarfile.open(fileobj=layer_file)
        except (KeyError, tarfile.TarError) as exc:
            raise ImageError(f"malformed container image: {exc}") from exc
        yield layer
    finally:
        archive.close()


def validate_manifest(layer: tarfile.TarFile) -> dict:
    member = extract(layer, BOOT_MEMBERS["manifest"])
    if member is None:
        raise ImageError(
            "no disk/image.json in the layer: not a containerDisk"
        )
    try:
        raw = json.load(member)
    except json.JSONDecodeError as exc:
        raise ImageError(f"image.json is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ImageError("image.json is not a JSON object")
    if raw.get("schema") != 2:
        raise ImageError(
            f"image.json schema {raw.get('schema')!r}, expected 2"
        )
    require_fields(raw)
    # Before any staging/renaming: a bad provisioner must fail the
    # import with nothing installed, not leave an invisible cache
    # behind the 400.
    provisioner_of(raw)
    return raw


def require_fields(raw: dict) -> None:
    """Raise unless every schema-2 field is present."""
    missing = [
        field
        for field in ("name", "version", "cmdline", "vsock_shell_port")
        if field not in raw
    ]
    if missing:
        raise ImageError(f"image.json missing {missing[0]!r}")


#: The provisioners an image may declare (#41): which consumer eats
#: the workspace's cidata seed disk. cloud-init is the one consumer
#: the contract supports; the shipped image and any distro cloud
#: image ship it, so a #! script and a cloud-config document both
#: run.
PROVISIONERS = ("cloud-init",)


def provisioner_of(manifest: dict) -> str | None:
    """The declared provisioner from ``capabilities`` (None when
    absent). A present-but-unknown value is a named import error —
    create-time payload checks key off this field."""
    capabilities = manifest.get("capabilities")
    if capabilities is None:
        return None
    if not isinstance(capabilities, dict):
        raise ImageError("image.json capabilities must be an object")
    provisioner = capabilities.get("provisioner")
    if provisioner is None:
        return None
    if provisioner not in PROVISIONERS:
        raise ImageError(
            f"image.json capabilities.provisioner {provisioner!r} is unknown; "
            f"expected one of {PROVISIONERS}"
        )
    return provisioner


def extract(layer: tarfile.TarFile, member_name: str):
    """A member's file object, or None when absent or non-regular."""
    try:
        handle = layer.extractfile(f"./{member_name}")
        if handle is None:
            handle = layer.extractfile(member_name)
    except KeyError:
        handle = None
    return handle


def member(layer: tarfile.TarFile, dest: Path, member_name: str) -> None:
    """Extract one member to dest; ImageError when missing."""
    handle = extract(layer, member_name)
    if handle is None:
        raise ImageError(f"containerDisk member missing: {member_name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(handle, out)


def import_archive(path: Path, state_dir: Path) -> ImageRecord:
    """Register one archive: hash it, unpack the boot files, index.

    Idempotent in identity; re-importing the same archive refreshes
    the cache in place and returns the existing identity. The work
    happens on a private copy (hash-then-unpack TOCTOU: the digest
    must key exactly the bytes that were hashed) under a per-attempt
    staging name (concurrent imports cannot collide), and the swap
    into place is rename-aside (an interrupted re-import can never
    destroy the previously-good cache).
    """
    if not path.is_file():
        raise ImageError(f"no such image archive: {path}")
    root = images_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    attempt = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    source_copy = root / f".src-{attempt}.tar"
    try:
        shutil.copy2(path, source_copy)
        digest = hash_file(source_copy)
        cache = root / digest
        staging = root / f".{digest}.{attempt}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        with open_layer(source_copy) as layer:
            manifest = validate_manifest(layer)
            staging.mkdir(parents=True)
            member(layer, staging / "kernel", BOOT_MEMBERS["kernel"])
            member(layer, staging / "initrd", BOOT_MEMBERS["initrd"])
            member(layer, staging / "rootfs.ext4", BOOT_MEMBERS["rootfs"])
            (staging / "image.json").write_text(json.dumps(manifest))
            now = datetime.now(UTC)
            (staging / IMPORTED_STAMP).write_text(now.isoformat() + "\n")
            # Concurrent imports of the same archive race here; each
            # swap is idempotent because every attempt's content is
            # identical (same digest).
            aside = root / f".{digest}.{attempt}.old"
            try:
                cache.rename(aside)
            except FileNotFoundError:
                pass  # a concurrent import already swapped it away
            try:
                staging.rename(cache)
            except OSError:  # pragma: no cover
                # A concurrent identical import won the swap; discard
                # our duplicate. Only reachable under a tight race —
                # test_concurrent_import_swap_paths walks it.
                shutil.rmtree(staging, ignore_errors=True)  # pragma: no cover
            shutil.rmtree(aside, ignore_errors=True)
        # The private copy IS the retained archive; POSIX rename
        # replaces a concurrent winner's identical file atomically.
        source_copy.rename(root / f"archive-{digest}.tar")
        return record_from(cache, digest, manifest, imported=now)
    finally:
        # The private copy is either renamed into place or discarded.
        with contextlib.suppress(FileNotFoundError):
            source_copy.unlink()


def warm_import(path: Path, state_dir: Path) -> ImageRecord | None:
    """The already-imported record when the archive is unchanged.

    A cheap (hash-only) fast path for repeated daemon starts: one
    pass over the ~1.5G archive replaces a multi-second re-extract
    of the boot-file cache.
    """
    if not path.is_file():
        return None
    digest = hash_file(path)
    record = load_record(images_dir(state_dir) / digest)
    if record is not None and record.hash == digest:
        return record
    return None


def record_from(
    cache: Path,
    digest: str,
    manifest: dict,
    imported: datetime | None = None,
) -> ImageRecord:
    return ImageRecord(
        hash=digest,
        name=manifest["name"],
        version=str(manifest["version"]),
        cmdline=manifest["cmdline"],
        vsock_shell_port=int(manifest["vsock_shell_port"]),
        kernel_version=str(manifest.get("kernel_version", "")),
        kernel_format=str(manifest.get("kernel_format", "")),
        console_protocol=str(manifest.get("console_protocol", "legacy")),
        console_users=tuple(
            str(user) for user in manifest.get("console_users", ("root",))
        ),
        kernel=cache / "kernel",
        initrd=cache / "initrd",
        rootfs=cache / "rootfs.ext4",
        provisioner=provisioner_of(manifest),
        imported=imported,
    )


def imported_at(cache: Path) -> datetime | None:
    """When the cache last came in (#186): the stamp the import
    writes, or the directory's own modification time for entries
    that predate stamps (a re-import swaps in a fresh directory,
    so its mtime is the import time). A stamp without an offset is
    read as UTC — a hand-written naive stamp must not crash the
    listing's aware ordering."""
    with contextlib.suppress(OSError, ValueError):
        parsed = datetime.fromisoformat(
            (cache / IMPORTED_STAMP).read_text().strip()
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    with contextlib.suppress(OSError):
        return datetime.fromtimestamp(cache.stat().st_mtime, tz=UTC)
    # A cache removed between the manifest read and the stat —
    # a rename race the suite cannot arrange.
    return None  # pragma: no cover


def load_record(cache: Path) -> ImageRecord | None:
    manifest_path = cache / "image.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
        record = record_from(
            cache, cache.name, manifest, imported=imported_at(cache)
        )
    except json.JSONDecodeError, TypeError, KeyError, ValueError, ImageError:
        # A corrupt entry is an invisible image, not a daemon crash;
        # re-importing the archive repairs it.
        return None
    for member in ("kernel", "initrd", "rootfs.ext4"):
        if not (cache / member).is_file():
            return None
    return record


def sweep_crash_leftovers(state_dir: Path) -> None:
    """Drop staging/temp files an interrupted import left behind.

    Runs at daemon startup, before any import can be in flight, so
    no live staging is ever removed.
    """
    root = images_dir(state_dir)
    if not root.is_dir():
        return

    def is_debris(name: str) -> bool:
        # Staging copies (.src-<pid>-<uuid>.tar), swap temporaries
        # (.*.tmp), and rename-aside caches (.*.old).
        return name.startswith(".") or name.endswith((".tmp", ".old"))

    for entry in root.iterdir():
        if not is_debris(entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def cache_entries(root: Path) -> list[Path]:
    """Hash-keyed directories only (staging dirs are dot-prefixed)."""
    if not root.is_dir():
        return []
    return [
        entry
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def list_images(state_dir: Path) -> list[ImageRecord]:
    """Every complete cache entry, sorted by reference — then by
    import time, so entries sharing a reference order oldest-first
    (#186) and never fall back to directory-listing order."""
    root = images_dir(state_dir)
    records = [
        record
        for entry in cache_entries(root)
        if (record := load_record(entry)) is not None
    ]
    return sorted(
        records, key=lambda r: (r.name, r.version, r.imported or EPOCH)
    )


def resolve_hash(ref: str, images: list) -> ImageRecord | None:
    by_hash = {image.hash: image for image in images}
    return by_hash.get(ref)


def resolve_name_version(ref: str, images: list) -> ImageRecord | None:
    name, _, version = ref.partition(":")
    for image in images:
        if image.name == name and image.version == version:
            return image
    return None


def version_key(version: str) -> tuple:
    """Numeric version ordering: 13.10 sorts after 13.9."""
    parts = []
    for piece in version.replace("-", ".").split("."):
        # Tagged so segments never compare int-to-str (13.9 vs 13.rc).
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def resolve_newest(name: str, images: list) -> ImageRecord | None:
    candidates = [image for image in images if image.name == name]
    return max(candidates, key=lambda i: version_key(i.version), default=None)


def is_hash_shape(ref: str) -> bool:
    """64 lowercase hex characters."""
    return len(ref) == 64 and all(c in "0123456789abcdef" for c in ref)


def resolve(ref: str, state_dir: Path) -> ImageRecord | None:
    """A catalog reference: hash, name:version, name, or name@hash.

    A bare name resolves to its newest version (numeric ordering);
    name@hash pins both identity and content. A malformed hash in an
    @-reference is a named error, not a silent miss.
    """
    images = list_images(state_dir)
    if is_hash_shape(ref):
        return resolve_hash(ref, images)
    if "@" in ref:
        return resolve_pinned(ref, images)
    if ":" in ref:
        return resolve_name_version(ref, images)
    return resolve_newest(ref, images)


def resolve_pinned(ref: str, images: list) -> ImageRecord | None:
    """name@hash: pin both identity and content."""
    name, _, digest = ref.partition("@")
    if not is_hash_shape(digest):
        raise ImageError(f"malformed image hash in {ref!r}")
    record = resolve_hash(digest, images)
    if record is not None and record.name != name:
        return None  # the pin names a different image
    return record


def remove(digest: str, state_dir: Path) -> None:
    """Drop a catalog entry: the cache and the retained archive."""
    root = images_dir(state_dir)
    shutil.rmtree(root / digest, ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        (root / f"archive-{digest}.tar").unlink()


def set_default(digest: str, state_dir: Path) -> None:
    root = images_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "default").write_text(digest + "\n")


def default_image(state_dir: Path) -> ImageRecord | None:
    """The designated default, or the sole catalog entry, or None."""
    root = images_dir(state_dir)
    pointer = root / "default"
    if pointer.is_file():
        record = load_record(root / pointer.read_text().strip())
        if record is not None:
            return record
    images = list_images(state_dir)
    if len(images) == 1:
        return images[0]
    return None
