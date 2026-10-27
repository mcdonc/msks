"""Read a built guest dir's publishable image facts (#564).

The release workflow builds a guest flavor (``msks-build-guest
<flavor>``) and needs the channel manifest's human fields — the
ref, the description the build baked into ``image.json``, the
kernel version — plus the archive's own name. The archive is the
authoritative source (self-describing since #40): this reads the
containerDisk layer's ``disk/image.json`` straight out of it and
answers one JSON document; no sidecar can drift from the bytes
that ship.
"""

import argparse
import json
import sys
import tarfile
from pathlib import Path

TOOL = "image-meta"

#: The manifest member the container-image layout guarantees, and
#: the layer members the containerDisk convention fixes.
IMAGE_JSON = "disk/image.json"


def archive_in(guest_dir: Path) -> Path:
    """The guest dir's built archive — the one
    ``image-archive-name`` names (the build emits the exact file
    name so nothing globs)."""
    name = (guest_dir / "image-archive-name").read_text().strip()
    archive = guest_dir / name
    if not archive.is_file():
        raise SystemExit(f"{TOOL}: no such archive: {archive}")
    return archive


def image_json(archive: Path) -> dict:
    """The ``disk/image.json`` inside the archive's containerDisk
    layer — the same read :mod:`msks.imagestore` performs at
    import, on the same layout."""
    with tarfile.open(archive) as outer:
        member = outer.extractfile("manifest.json")
        if member is None:
            raise SystemExit(f"{TOOL}: no manifest.json in {archive}")
        manifest = json.loads(member.read())
        layer_member = manifest[0]["Layers"][0]
        with tarfile.open(fileobj=outer.extractfile(layer_member)) as layer:
            member = layer.extractfile(f"./{IMAGE_JSON}")
            if member is None:
                member = layer.extractfile(IMAGE_JSON)
            if member is None:
                raise SystemExit(
                    f"{TOOL}: no {IMAGE_JSON} in the layer of {archive}"
                )
            return json.loads(member.read())


def meta(guest_dir: Path) -> dict:
    """The channel row's facts for one built flavor: ref,
    description, kernel, arch, and the archive's file name."""
    archive = archive_in(guest_dir)
    manifest = image_json(archive)
    return {
        "ref": f"{manifest['name']}:{manifest['version']}",
        "description": manifest.get("description", ""),
        "kernel_version": manifest.get("kernel_version", ""),
        "arch": manifest.get("arch", ""),
        "archive": archive.name,
    }


def archive_or_dir(exc: Exception, args: argparse.Namespace) -> str:
    """The path a broken-archive failure names: the archive when
    the read got that far, the guest dir otherwise."""
    return getattr(exc, "filename", str(args.guest_dir)) or str(args.guest_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Print a built guest dir's publishable image "
        "facts as JSON (#564).",
    )
    parser.add_argument(
        "guest_dir",
        type=Path,
        help="the build's output dir (.devenv/state/guest, or the "
        "nixos one) — the dir carrying image-archive-name",
    )
    args = parser.parse_args(argv)
    try:
        facts = meta(args.guest_dir)
    except (
        OSError,
        tarfile.TarError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as exc:
        print(
            f"{TOOL}: {archive_or_dir(exc, args)} is not a built "
            f"guest archive: {exc}",
            file=sys.stderr,
        )
        return 1
    json.dump(facts, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
