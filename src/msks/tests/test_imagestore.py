"""The workspace image catalog (#40): import, list, resolve, default."""

import json
import tarfile
import time
from io import BytesIO
from pathlib import Path

import pytest
from msks.imagestore import (
    ImageError,
    default_image,
    import_archive,
    list_images,
    resolve,
    set_default,
)


def build_containerdisk(
    path: Path,
    name: str = "debian",
    version: str = "13.6",
    schema: int | None = None,
    members: dict | None = None,
) -> None:
    """Write a minimal-but-valid OCI archive in containerDisk layout."""
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
    import gzip

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
    from msks import guestassets

    assets = guestassets.load_guest_assets()
    if assets is None:
        pytest.skip("guest assets not built")
    archive = assets.vmlinux.parent / "workspace-debian-13.6.tar"
    if not archive.is_file():
        pytest.skip("built image archive not present")
    start = time.monotonic()
    record = import_archive(archive, tmp_path)
    elapsed = time.monotonic() - start
    assert record.name == "debian"
    assert (record.kernel.stat().st_size) > 1_000_000
    assert record.rootfs.stat().st_size > 1_000_000_000
    assert elapsed < 120, "import should be I/O-bound, not pathological"


async def test_image_endpoints_and_create_by_ref(tmp_path) -> None:
    """The catalog surfaces over the API and fills workspace boots."""

    from httpx import ASGITransport, AsyncClient
    from msks.app import build_app
    from msks.server.api import build_api
    from msks.settings import ServerSettings, Settings, VmmSettings
    from test_api import TOKEN, StubMicrovm

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
    from test_api import auth

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
    from fastapi.testclient import TestClient
    from msks.app import build_app
    from msks.server.api import build_api
    from msks.settings import ServerSettings, Settings, VmmSettings
    from test_api import TOKEN, StubMicrovm

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
    with pytest.raises(ImageError, match="malformed OCI archive"):
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
    from fastapi.testclient import TestClient
    from msks.app import build_app
    from msks.server.api import build_api
    from msks.settings import ServerSettings, Settings, VmmSettings

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
    from fastapi.testclient import TestClient
    from msks.app import build_app
    from msks.server.api import build_api
    from msks.settings import ServerSettings, Settings, VmmSettings

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
    from msks.imagestore import default_image

    default = default_image(state)
    assert default.ref == "first:1"


async def test_create_explicit_kernel_keeps_own_initrd(tmp_path) -> None:
    """Explicit artifacts win; the default image fills only cmdline."""
    from fastapi.testclient import TestClient
    from msks.app import build_app
    from msks.server.api import build_api
    from msks.settings import ServerSettings, Settings, VmmSettings
    from test_api import TOKEN

    archive = tmp_path / "d.tar"
    build_containerdisk(archive)
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms", default_image=str(archive)),
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    from test_api import TOKEN, StubMicrovm

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


def test_import_manifest_not_json(tmp_path: Path) -> None:
    archive = tmp_path / "nj.tar"
    with tarfile.open(archive, "w") as outer:
        blob = b"{definitely not json"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        outer.addfile(info, BytesIO(blob))
    with pytest.raises(ImageError, match="malformed OCI archive"):
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
