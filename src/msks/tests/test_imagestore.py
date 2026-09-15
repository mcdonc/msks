"""The workspace image catalog (#40): import, list, resolve, default."""

import asyncio
import gzip
import json
import shutil
import tarfile
import threading
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from msks.app import build_app
from msks.imagestore import (
    ImageError,
    default_image,
    import_archive,
    list_images,
    record_from,
    resolve,
    set_default,
    sweep_crash_leftovers,
    warm_import,
)
from msks.server.api import build_api
from msks.settings import ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm, auth

from msks import guestassets, imagestore


def build_containerdisk(
    path: Path,
    name: str = "debian",
    version: str = "13.6",
    schema: int | None = None,
    members: dict | None = None,
) -> None:
    """Write a minimal-but-valid container-image tar in containerDisk layout."""
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        payload = members or {
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
        }
        if schema is not False:
            manifest = {
                "schema": 2 if schema is None else schema,
                "name": name,
                "version": version,
                "cmdline": "console=ttyS0 root=/dev/vda ro",
                "vsock_shell_port": 1023,
            }
            payload = dict(payload)
            payload["disk/image.json"] = json.dumps(manifest).encode()
        for member, content in payload.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
    layer.seek(0)
    with tarfile.open(path, "w") as outer:
        info = tarfile.TarInfo("layer.tar")
        info.size = layer.getbuffer().nbytes
        outer.addfile(info, layer)
        manifest = json.dumps([{"Layers": ["layer.tar"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        outer.addfile(info, BytesIO(manifest))


def test_import_and_list(tmp_path: Path) -> None:
    archive = tmp_path / "workspace-test-1.0.tar"
    build_containerdisk(archive)
    record = import_archive(archive, tmp_path)
    assert (record.name, record.version) == ("debian", "13.6")
    assert record.kernel.read_bytes() == b"kernel-bytes"
    assert record.rootfs.read_bytes() == b"rootfs-bytes"
    listed = list_images(tmp_path)
    assert [r.ref for r in listed] == ["debian:13.6"]
    # The archive itself is retained as the manageable artifact.
    assert (tmp_path / "images" / f"archive-{record.hash}.tar").is_file()


def test_import_is_idempotent_and_repairing(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar"
    build_containerdisk(archive)
    first = import_archive(archive, tmp_path)
    second = import_archive(archive, tmp_path)
    assert first.hash == second.hash
    # A corrupted cache entry (missing kernel) is invisible until the
    # import re-populates it.
    first.kernel.unlink()
    assert list_images(tmp_path) == []
    repaired = import_archive(archive, tmp_path)
    assert repaired.kernel.is_file()


def test_resolve_forms(tmp_path: Path) -> None:
    a, b, c = (tmp_path / f"i{i}.tar" for i in range(3))
    build_containerdisk(a, version="13.5")
    build_containerdisk(b, version="13.6")
    build_containerdisk(c, name="alpine", version="3.22")
    for archive in (a, b, c):
        import_archive(archive, tmp_path)
    assert resolve("alpine", tmp_path).ref == "alpine:3.22"
    assert resolve("debian", tmp_path).ref == "debian:13.6"  # newest
    assert resolve("debian:13.5", tmp_path).ref == "debian:13.5"
    by_hash = resolve(list_images(tmp_path)[0].hash, tmp_path)
    assert by_hash is not None
    assert resolve("nope", tmp_path) is None
    assert resolve("debian:99", tmp_path) is None


def test_default_selection(tmp_path: Path) -> None:
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    build_containerdisk(a, name="one", version="1")
    build_containerdisk(b, name="two", version="2")
    import_archive(a, tmp_path)
    # Sole entry: the implicit default.
    assert default_image(tmp_path).ref == "one:1"
    two = import_archive(b, tmp_path)
    # Two entries, no pointer: nothing is silently picked.
    assert default_image(tmp_path) is None
    set_default(two.hash, tmp_path)
    assert default_image(tmp_path).ref == "two:2"
    # A stale pointer falls back to nothing (never a wrong image).
    set_default("f" * 64, tmp_path)
    assert default_image(tmp_path) is None


@pytest.mark.parametrize(
    "mangle",
    [
        "no-manifest",
        "empty-layers",
        "missing-image-json",
        "missing-rootfs",
        "bad-schema",
        "bad-json",
    ],
)
def test_import_rejects_malformed(tmp_path: Path, mangle: str) -> None:
    archive = tmp_path / "bad.tar"
    if mangle == "no-manifest":
        with tarfile.open(archive, "w"):
            pass  # empty archive
    elif mangle == "empty-layers":
        with tarfile.open(archive, "w") as outer:
            blob = json.dumps([{"Layers": []}]).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(blob)
            outer.addfile(info, BytesIO(blob))
    elif mangle in ("missing-image-json", "missing-rootfs", "bad-schema"):
        members = {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
        }
        if mangle == "missing-rootfs":
            del members["disk/rootfs.ext4"]
        build_containerdisk(
            archive,
            members=members,
            schema=(
                7
                if mangle == "bad-schema"
                else False
                if mangle == "missing-image-json"
                else None
            ),
        )
    else:  # bad-json: hand-roll an archive with junk image.json
        layer = BytesIO()
        with tarfile.open(fileobj=layer, mode="w") as tar:
            for member, content in {
                "boot/vmlinuz": b"k",
                "boot/initrd.img": b"i",
                "disk/rootfs.ext4": b"r",
                "disk/image.json": b"{not json",
            }.items():
                info = tarfile.TarInfo(f"./{member}")
                info.size = len(content)
                tar.addfile(info, BytesIO(content))
        layer.seek(0)
        with tarfile.open(archive, "w") as outer:
            info = tarfile.TarInfo("layer.tar")
            info.size = layer.getbuffer().nbytes
            outer.addfile(info, layer)
            blob = json.dumps([{"Layers": ["layer.tar"]}]).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(blob)
            outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError):
        import_archive(archive, tmp_path)
    assert list_images(tmp_path) == []


def test_import_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ImageError, match="no such image archive"):
        import_archive(tmp_path / "absent.tar", tmp_path)


def test_gzip_layer_supported(tmp_path: Path) -> None:
    """dockerTools may emit compressed layers; the reader copes."""

    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        for member, content in {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
            "disk/image.json": json.dumps(
                {
                    "schema": 2,
                    "name": "gz",
                    "version": "1",
                    "cmdline": "c",
                    "vsock_shell_port": 1,
                }
            ).encode(),
        }.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
    raw = layer.getvalue()
    archive = tmp_path / "gz.tar"
    with tarfile.open(archive, "w") as outer:
        info = tarfile.TarInfo("layer.tar.gz")
        info.size = len(gzip.compress(raw))
        outer.addfile(info, BytesIO(gzip.compress(raw)))
        blob = json.dumps([{"Layers": ["layer.tar.gz"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    record = import_archive(archive, tmp_path)
    assert record.name == "gz"
    assert record.kernel.read_bytes() == b"k"


def test_import_of_the_real_built_image(tmp_path: Path) -> None:
    """The .guest-built containerDisk, when present, imports as-is."""

    assets = guestassets.load_guest_assets()
    if assets is None:
        pytest.skip("guest assets not built")
    archive = assets.vmlinux.parent / "workspace-debian-13.6.tar"
    if not archive.is_file():
        pytest.skip("built image archive not present")
    record = import_archive(archive, tmp_path)
    assert record.name == "debian"
    assert record.kernel.stat().st_size > 1_000_000
    assert record.rootfs.stat().st_size > 1_000_000_000
    # The retained archive is loadable by stock tooling: the RepoTag
    # must be a valid reference (the ''-escape bug shipped a literal
    # pair of quotes here once).
    with tarfile.open(archive) as tf:
        tag = json.load(tf.extractfile("manifest.json"))[0]["RepoTags"][0]
    assert "'" not in tag and "''" not in tag, tag


async def test_image_endpoints_and_create_by_ref(tmp_path) -> None:
    """The catalog surfaces over the API and fills workspace boots."""

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=0.05
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)

    archive = tmp_path / "in.tar"
    build_containerdisk(archive, name="debian", version="13.6")

    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://t"
        ) as http:
            await _drive_image_flow(http, archive)


async def _drive_image_flow(http, archive) -> None:

    listed = await http.get("/api/v1/images", headers=auth())
    assert listed.status_code == 200
    assert listed.json() == []

    imported = await http.post(
        "/api/v1/images", json={"source": str(archive)}, headers=auth()
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["ref"] == "debian:13.6"

    # First import became the default: bare create resolves.
    made = await http.post("/api/v1/workspaces", json={"id": "ws-img"}, headers=auth())
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["kernel"].endswith("/kernel")
    assert row["rootfs"].endswith("/rootfs.ext4")
    assert row["cmdline"] == "console=ttyS0 root=/dev/vda ro"

    # Explicit ref, and an unknown one.
    made2 = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-img2", "image": "debian:13.6"},
        headers=auth(),
    )
    assert made2.status_code == 201
    missing = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-x", "image": "nope"},
        headers=auth(),
    )
    assert missing.status_code == 404

    listed = await http.get("/api/v1/images", headers=auth())
    assert listed.json()[0]["default"] is True

    bad = await http.post(
        "/api/v1/images", json={"source": "/absent.tar"}, headers=auth()
    )
    assert bad.status_code == 400

    # A second import must not steal the default designation.
    second_archive = archive.parent / "second.tar"
    build_containerdisk(second_archive, name="other", version="1")
    again = await http.post(
        "/api/v1/images", json={"source": str(second_archive)}, headers=auth()
    )
    assert again.status_code == 201
    listed = await http.get("/api/v1/images", headers=auth())
    defaults = [image for image in listed.json() if image["default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "debian"


async def test_create_explicit_artifacts_without_catalog(tmp_path) -> None:
    """Explicit kernel/rootfs with no catalog at all: the old shape."""

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    with TestClient(build_api(app)) as client:
        made = client.post(
            "/api/v1/workspaces",
            json={"id": "ws-plain", "kernel": "/k", "rootfs": "/r"},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert made.status_code == 201, made.text
        row = made.json()
        assert row["initrd"] is None
        assert row["cmdline"] == "console=hvc0 root=/dev/vda rw"
        explicit = client.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-cmd",
                "kernel": "/k",
                "rootfs": "/r",
                "cmdline": "console=ttyS0 custom=1",
            },
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert explicit.json()["cmdline"] == "console=ttyS0 custom=1"


def test_import_rejects_not_a_tar(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"this is not a tar archive at all")
    with pytest.raises(ImageError, match="not a tar archive"):
        import_archive(junk, tmp_path)


def test_import_rejects_directory_manifest(tmp_path: Path) -> None:
    """extractfile on a directory yields None, not KeyError."""
    archive = tmp_path / "d.tar"
    with tarfile.open(archive, "w") as outer:
        outer.addfile(tarfile.TarInfo("manifest.json"), BytesIO(b"")) if False else None
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.DIRTYPE
        outer.addfile(info)
    with pytest.raises(ImageError, match="no manifest.json"):
        import_archive(archive, tmp_path)


def test_import_rejects_missing_layer_member(tmp_path: Path) -> None:
    archive = tmp_path / "ml.tar"
    with tarfile.open(archive, "w") as outer:
        blob = json.dumps([{"Layers": ["layer.tar"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
        # layer.tar deliberately absent
    with pytest.raises(ImageError, match="malformed container image"):
        import_archive(archive, tmp_path)


def test_import_rejects_bad_manifest_field(tmp_path: Path) -> None:
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        image = json.dumps(
            {"schema": 2, "name": "x", "version": "1"}  # no cmdline/port
        ).encode()
        info = tarfile.TarInfo("./disk/image.json")
        info.size = len(image)
        tar.addfile(info, BytesIO(image))
        for member, content in {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
        }.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
    archive = tmp_path / "mf.tar"
    _wrap_layer(archive, layer)
    with pytest.raises(ImageError, match="missing 'cmdline'"):
        import_archive(archive, tmp_path)


def _wrap_layer(archive: Path, layer: BytesIO) -> None:
    layer.seek(0)
    with tarfile.open(archive, "w") as outer:
        info = tarfile.TarInfo("layer.tar")
        info.size = layer.getbuffer().nbytes
        outer.addfile(info, layer)
        blob = json.dumps([{"Layers": ["layer.tar"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))


def test_list_skips_corrupt_cache_entries(tmp_path: Path) -> None:
    images = tmp_path / "images"
    bad = images / ("a" * 64)
    bad.mkdir(parents=True)
    (bad / "image.json").write_text("{not json")
    for member in ("kernel", "initrd", "rootfs.ext4"):
        (bad / member).write_bytes(b"x")
    assert list_images(tmp_path) == []


def test_resolve_well_formed_unknown_hash(tmp_path: Path) -> None:
    assert resolve("e" * 64, tmp_path) is None


async def test_default_image_bootstrap(tmp_path, capsys) -> None:
    """MSKSD_DEFAULT_IMAGE imports once at startup, as the default."""

    archive = tmp_path / "boot.tar"
    build_containerdisk(archive, name="boot", version="1")

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms", default_image=str(archive)),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token="t", event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    api = build_api(app)
    with TestClient(api):
        default = default_image(tmp_path / "vms")
        assert default is not None
        assert default.ref == "boot:1"
        assert "default image boot:1" in capsys.readouterr().out

    # Second startup with the same archive: the warm path skips the
    # re-import entirely (no second "imported" line).
    app2 = build_app(settings)
    with TestClient(build_api(app2)):
        assert default_image(tmp_path / "vms").ref == "boot:1"
        assert capsys.readouterr().out == ""

    # A broken pointer is loud but non-fatal: the API still serves.
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms2", default_image="/absent.tar"),
        server=ServerSettings(
            db_path=tmp_path / "ws2.db", bootstrap_token="t", event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    api = build_api(app)
    with TestClient(api) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert "default image import failed" in capsys.readouterr().out


def test_import_layer_entry_is_directory(tmp_path: Path) -> None:
    archive = tmp_path / "ld.tar"
    with tarfile.open(archive, "w") as outer:
        info = tarfile.TarInfo("layer.tar")
        info.type = tarfile.DIRTYPE
        outer.addfile(info)
        blob = json.dumps([{"Layers": ["layer.tar"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="layer member missing"):
        import_archive(archive, tmp_path)


def test_import_image_json_is_directory(tmp_path: Path) -> None:
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        info = tarfile.TarInfo("./disk/image.json")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    archive = tmp_path / "ijd.tar"
    _wrap_layer(archive, layer)
    with pytest.raises(ImageError, match="no disk/image.json"):
        import_archive(archive, tmp_path)


async def test_bootstrap_second_image_no_default_steal(tmp_path) -> None:
    """A later bootstrap import must not steal the designation."""

    first, second = tmp_path / "a.tar", tmp_path / "b.tar"
    build_containerdisk(first, name="first", version="1")
    build_containerdisk(second, name="second", version="2")
    state = tmp_path / "vms"
    for archive in (first, second):
        settings = Settings(
            vmm=VmmSettings(state_dir=state, default_image=str(archive)),
            server=ServerSettings(
                db_path=tmp_path / "ws.db", bootstrap_token="t", event_poll_s=10.0
            ),
        )
        app = build_app(settings)
        with TestClient(build_api(app)):
            pass

    default = default_image(state)
    assert default.ref == "first:1"


async def test_create_explicit_kernel_keeps_own_initrd(tmp_path) -> None:
    """Explicit artifacts win; the default image fills only cmdline."""

    archive = tmp_path / "d.tar"
    build_containerdisk(archive)
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms", default_image=str(archive)),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=10.0
        ),
    )
    app = build_app(settings)

    app.state.microvm = StubMicrovm()
    with TestClient(build_api(app)) as client:
        made = client.post(
            "/api/v1/workspaces",
            json={"id": "ws-k", "kernel": "/my/kernel", "rootfs": "/my/rootfs"},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert made.status_code == 201, made.text
        row = made.json()
        assert row["kernel"] == "/my/kernel"
        assert row["initrd"] is None
        # No cmdline given and a record exists: the image's cmdline.
        assert row["cmdline"] == "console=ttyS0 root=/dev/vda ro"

        partial = client.post(
            "/api/v1/workspaces",
            json={"id": "ws-half", "kernel": "/k"},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert partial.status_code == 400
        assert "together" in partial.json()["detail"]


def test_import_manifest_not_json(tmp_path: Path) -> None:
    archive = tmp_path / "nj.tar"
    with tarfile.open(archive, "w") as outer:
        blob = b"{definitely not json"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="malformed container image"):
        import_archive(archive, tmp_path)


def test_import_manifest_empty_layers_entry(tmp_path: Path) -> None:
    archive = tmp_path / "el.tar"
    with tarfile.open(archive, "w") as outer:
        blob = json.dumps([{"Layers": []}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="no layers"):
        import_archive(archive, tmp_path)


def test_import_manifest_empty_list(tmp_path: Path) -> None:
    archive = tmp_path / "e.tar"
    with tarfile.open(archive, "w") as outer:
        blob = json.dumps([]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="no layers"):
        import_archive(archive, tmp_path)


async def test_concurrent_same_hash_imports() -> None:
    """Two simultaneous imports of one archive cannot collide."""

    async def run(tmpdir):
        return await asyncio.to_thread(import_archive, tmpdir / "c.tar", tmpdir)

    root = Path("/tmp") / f"msks-conc-{uuid4hex()}"
    root.mkdir(parents=True)
    try:
        build_containerdisk(root / "c.tar", name="cc", version="1")
        results = await asyncio.gather(run(root), run(root), run(root))
        refs = {r.hash for r in results}
        assert len(refs) == 1
        assert len(list_images(root)) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def uuid4hex() -> str:

    return uuid.uuid4().hex[:10]


def test_resolve_numeric_versions(tmp_path: Path) -> None:
    """13.10 is newer than 13.9 (not lexically older)."""
    for version in ("13.9", "13.10"):
        archive = tmp_path / f"v{version}.tar"
        build_containerdisk(archive, version=version)
        import_archive(archive, tmp_path)
    assert resolve("debian", tmp_path).ref == "debian:13.10"


def test_warm_import_skips_complete_cache(tmp_path: Path) -> None:

    archive = tmp_path / "w.tar"
    build_containerdisk(archive, name="warm", version="1")
    first = import_archive(archive, tmp_path)
    # Re-import via the warm path: same identity, no re-extraction
    # (the cache dir's inode survives).
    cache_dir = tmp_path / "images" / first.hash
    marker_stat = cache_dir.stat()
    warm = warm_import(archive, tmp_path)
    assert warm is not None and warm.hash == first.hash
    assert cache_dir.stat().st_ino == marker_stat.st_ino
    assert warm_import(tmp_path / "absent.tar", tmp_path) is None
    # A different archive is a miss, not a stale hit.
    other = tmp_path / "other.tar"
    build_containerdisk(other, name="other", version="9")
    assert warm_import(other, tmp_path) is None


def test_resolve_name_at_hash_pin(tmp_path: Path) -> None:
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    build_containerdisk(a, name="debian", version="1")
    build_containerdisk(b, name="alpine", version="1")
    first = import_archive(a, tmp_path)
    second = import_archive(b, tmp_path)
    pinned = resolve(f"debian@{first.hash}", tmp_path)
    assert pinned.ref == "debian:1"
    # The pin names a different image: a miss, not a wrong answer.
    assert resolve(f"debian@{second.hash}", tmp_path) is None
    with pytest.raises(ImageError, match="malformed image hash"):
        resolve("debian@nothex", tmp_path)


async def test_image_delete_with_reference_guard(tmp_path) -> None:

    archive = tmp_path / "del.tar"
    build_containerdisk(archive, name="gone", version="1")
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms", default_image=str(archive)),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    with TestClient(build_api(app)) as client:
        headers = {"authorization": f"Bearer {TOKEN}"}
        listed = client.get("/api/v1/images", headers=headers).json()
        digest = listed[0]["hash"]
        plain = client.post(
            "/api/v1/workspaces",
            json={"id": "ws-plain", "kernel": "/k", "rootfs": "/r"},
            headers=headers,
        )
        assert plain.status_code == 201
        made = client.post(
            "/api/v1/workspaces", json={"id": "ws-keep"}, headers=headers
        )
        assert made.status_code == 201
        malformed = client.post(
            "/api/v1/workspaces",
            json={"id": "ws-badref", "image": "gone@nothex"},
            headers=headers,
        )
        assert malformed.status_code == 400
        assert "malformed image hash" in malformed.json()["detail"]
        guarded = client.delete(f"/api/v1/images/{digest}", headers=headers)
        assert guarded.status_code == 409
        assert "ws-keep" in guarded.json()["detail"]
        assert (
            client.delete(f"/api/v1/images/{'f' * 64}", headers=headers).status_code
            == 404
        )
        gone = client.delete("/api/v1/workspaces/ws-keep", headers=headers)
        assert gone.status_code == 200
        freed = client.delete(f"/api/v1/images/{digest}", headers=headers)
        assert freed.status_code == 200
        assert client.get("/api/v1/images", headers=headers).json() == []


def test_image_json_carries_kernel_facts(tmp_path: Path) -> None:
    archive = tmp_path / "kv.tar"
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        image = json.dumps(
            {
                "schema": 2,
                "name": "kv",
                "version": "1",
                "cmdline": "c",
                "vsock_shell_port": 1,
                "kernel_version": "6.12.107+deb13-amd64",
                "kernel_format": "bzImage",
            }
        ).encode()
        info = tarfile.TarInfo("./disk/image.json")
        info.size = len(image)
        tar.addfile(info, BytesIO(image))
        for member, content in {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
        }.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
    _wrap_layer(archive, layer)
    record = import_archive(archive, tmp_path)
    assert record.kernel_version == "6.12.107+deb13-amd64"
    assert record.kernel_format == "bzImage"


def test_import_archive_retained_wins_race(tmp_path: Path) -> None:
    """A pre-existing retained archive: the copy rename is skipped."""
    archive = tmp_path / "r.tar"
    build_containerdisk(archive, name="rr", version="1")
    first = import_archive(archive, tmp_path)
    # Simulate a concurrent winner: keep the retained archive, drop
    # only the cache, then re-import.
    shutil.rmtree(tmp_path / "images" / first.hash)
    again = import_archive(archive, tmp_path)
    assert again.hash == first.hash
    assert (tmp_path / "images" / f"archive-{first.hash}.tar").is_file()


async def test_concurrent_import_swap_paths(tmp_path, monkeypatch) -> None:
    """Deterministic cover of the losing swap branches."""

    archive = tmp_path / "s.tar"
    build_containerdisk(archive, name="ss", version="1")

    barrier = threading.Barrier(2, timeout=10)
    real_hash = imagestore.hash_file

    def slow_hash(path):
        digest = real_hash(path)
        # Both importers hash (different private copies), then enter
        # extraction together: the swap window overlaps by force.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return digest

    monkeypatch.setattr(imagestore, "hash_file", slow_hash)

    async def run():
        return await asyncio.to_thread(imagestore.import_archive, archive, tmp_path)

    first, second = await asyncio.gather(run(), run())
    assert first.hash == second.hash
    assert imagestore.list_images(tmp_path)[0].ref == "ss:1"


def test_malformed_manifest_shapes_are_400s(tmp_path: Path) -> None:
    """Round-2 finding 1: non-dict manifest entries, non-dict image.json."""
    # manifest.json whose first entry is a list, not an object.
    archive = tmp_path / "bad-manifest.tar"
    with tarfile.open(archive, "w") as outer:
        info = tarfile.TarInfo("manifest.json")
        blob = b'[["layer.tar"]]'
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="manifest carries no layers"):
        import_archive(archive, tmp_path)

    # image.json that is valid JSON but not an object.
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as disk:
        image = b"[1, 2]"
        info = tarfile.TarInfo("./disk/image.json")
        info.size = len(image)
        disk.addfile(info, BytesIO(image))
        for member, content in {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
        }.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            disk.addfile(info, BytesIO(content))
    archive = tmp_path / "bad-image-json.tar"
    _wrap_layer(archive, layer)
    with pytest.raises(ImageError, match="not a JSON object"):
        import_archive(archive, tmp_path)


def test_corrupt_cache_manifest_degrades(tmp_path: Path) -> None:
    """Round-2 finding 2: a corrupt entry is invisible, not a crash."""
    archive = tmp_path / "c.tar"
    build_containerdisk(archive, name="cc", version="1")
    record = import_archive(archive, tmp_path)
    cache = tmp_path / "images" / record.hash
    (cache / "image.json").write_text("[1, 2]")  # valid JSON, wrong shape
    assert list_images(tmp_path) == []
    # warm_import misses and re-import repairs.
    repaired = import_archive(archive, tmp_path)
    assert repaired.hash == record.hash
    assert list_images(tmp_path)[0].ref == "cc:1"


def test_version_key_never_compares_int_to_str(tmp_path: Path) -> None:
    """Round-2 finding 3: 1.0 vs 1.rc must order, not TypeError."""
    first = tmp_path / "v1.tar"
    second = tmp_path / "v2.tar"
    build_containerdisk(first, name="vv", version="1.0")
    build_containerdisk(second, name="vv", version="1.rc")
    import_archive(first, tmp_path)
    import_archive(second, tmp_path)
    newest = resolve("vv", tmp_path)
    assert newest is not None
    # "rc" outranks "0" lexically: (1,"rc") > (0,0).
    assert newest.version == "1.rc"


def test_sweep_crash_leftovers(tmp_path: Path) -> None:
    """Startup sweep drops interrupted-import debris."""
    root = tmp_path / "images"
    root.mkdir(parents=True)
    (root / ".src-1234-abcd.tar").write_bytes(b"partial")
    (root / ".abc.1234.tmp").mkdir()
    (root / "dd.5678.old").mkdir()
    keep = root / ("a" * 64)
    keep.mkdir()
    (keep / "image.json").write_text("{}")
    sweep_crash_leftovers(tmp_path)
    assert list(root.iterdir()) == [keep]


def test_foreign_archive_has_no_fabricated_kernel_format(tmp_path: Path) -> None:
    """kernel_format is a declared fact, not a default."""
    archive = tmp_path / "foreign.tar"
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as disk:
        image = json.dumps(
            {
                "schema": 2,
                "name": "foreign",
                "version": "9",
                "cmdline": "c",
                "vsock_shell_port": 1023,
            }
        ).encode()
        info = tarfile.TarInfo("./disk/image.json")
        info.size = len(image)
        disk.addfile(info, BytesIO(image))
        for member, content in {
            "boot/vmlinuz": b"k",
            "boot/initrd.img": b"i",
            "disk/rootfs.ext4": b"r",
        }.items():
            info = tarfile.TarInfo(f"./{member}")
            info.size = len(content)
            disk.addfile(info, BytesIO(content))
    _wrap_layer(archive, layer)
    record = import_archive(archive, tmp_path)
    assert record.kernel_version == ""
    assert record.kernel_format == ""


def test_provisioner_round_trip(tmp_path: Path) -> None:
    """capabilities.provisioner (#41) parses, survives listing, and
    absent stays None."""
    archive = tmp_path / "a.tar"
    build_containerdisk(
        archive,
        schema=False,
        members={
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
            "disk/image.json": json.dumps(
                {
                    "schema": 2,
                    "name": "debian",
                    "version": "13.6",
                    "cmdline": "console=ttyS0 root=/dev/vda ro",
                    "vsock_shell_port": 1023,
                    "capabilities": {"provisioner": "cloud-init"},
                }
            ).encode(),
        },
    )
    record = import_archive(archive, tmp_path)
    assert record.provisioner == "cloud-init"
    assert list_images(tmp_path)[0].provisioner == "cloud-init"
    plain = tmp_path / "b.tar"
    build_containerdisk(plain, name="plain")
    assert import_archive(plain, tmp_path).provisioner is None


def test_unknown_provisioner_is_a_named_import_error(tmp_path: Path) -> None:
    """A typo'd provisioner fails the import with the value named —
    it is the field create-time payload checks key off."""
    archive = tmp_path / "a.tar"
    build_containerdisk(
        archive,
        schema=False,
        members={
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
            "disk/image.json": json.dumps(
                {
                    "schema": 2,
                    "name": "debian",
                    "version": "13.6",
                    "cmdline": "console=ttyS0 root=/dev/vda ro",
                    "vsock_shell_port": 1023,
                    "capabilities": {"provisioner": "cloudinit!"},
                }
            ).encode(),
        },
    )
    with pytest.raises(ImageError, match="capabilities.provisioner"):
        import_archive(archive, tmp_path)
    # The refusal lands before anything installs: no cache, no
    # retained archive, only the (swept) private copy's absence.
    assert list((tmp_path / "images").iterdir()) == []


def test_non_object_capabilities_is_a_named_import_error(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar"
    build_containerdisk(
        archive,
        schema=False,
        members={
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
            "disk/image.json": json.dumps(
                {
                    "schema": 2,
                    "name": "debian",
                    "version": "13.6",
                    "cmdline": "console=ttyS0 root=/dev/vda ro",
                    "vsock_shell_port": 1023,
                    "capabilities": ["cloud-init"],
                }
            ).encode(),
        },
    )
    with pytest.raises(ImageError, match="capabilities must be an object"):
        import_archive(archive, tmp_path)


def test_cached_manifest_with_bad_provisioner_is_invisible(tmp_path: Path) -> None:
    """A hand-edited cache entry with a bogus provisioner is an
    invisible image, not a daemon crash; re-import repairs it."""
    archive = tmp_path / "a.tar"
    build_containerdisk(archive)
    record = import_archive(archive, tmp_path)
    manifest_path = tmp_path / "images" / record.hash / "image.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["capabilities"] = {"provisioner": "bogus"}
    manifest_path.write_text(json.dumps(manifest))
    assert list_images(tmp_path) == []
    repaired = import_archive(archive, tmp_path)
    assert repaired.provisioner is None


def test_capabilities_without_provisioner_reads_as_none(tmp_path: Path) -> None:
    """capabilities may exist without a provisioner (future keys):
    the provisioner reads as absent, not as an error."""
    archive = tmp_path / "a.tar"
    build_containerdisk(
        archive,
        schema=False,
        members={
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
            "disk/image.json": json.dumps(
                {
                    "schema": 2,
                    "name": "debian",
                    "version": "13.6",
                    "cmdline": "console=ttyS0 root=/dev/vda ro",
                    "vsock_shell_port": 1023,
                    "capabilities": {"future-key": True},
                }
            ).encode(),
        },
    )
    assert import_archive(archive, tmp_path).provisioner is None


# --- console identity markers (#63) -----------------------------------


def test_record_defaults_to_legacy_console(tmp_path: Path) -> None:
    record = record_from(tmp_path, "h", minimal_manifest())
    assert record.console_protocol == "legacy"
    assert record.console_users == ("root",)


def test_record_reads_console_markers(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    manifest["console_protocol"] = "prelude-v1"
    manifest["console_users"] = ["root", "msks"]
    record = record_from(tmp_path, "h", manifest)
    assert record.console_protocol == "prelude-v1"
    assert record.console_users == ("root", "msks")


def minimal_manifest() -> dict:
    return {
        "name": "debian",
        "version": "13.6",
        "cmdline": "console=ttyS0 root=/dev/vda rw",
        "vsock_shell_port": 1023,
    }
