"""State-disk capacity reporting (#184): budget, cost, pressure."""

from pathlib import Path

import pytest
from msks.settings import VmmSettings

from msks import storage


def seed_state_dir(root: Path) -> Path:
    """A state dir with one workspace's artifacts and one image."""
    vm_dir = root / "vms" / "ws1"
    vm_dir.mkdir(parents=True)
    overlay = vm_dir / "root.qcow2"
    overlay.write_bytes(b"x" * (2 * 1024 * 1024))
    (vm_dir / "seed.img").write_bytes(b"y" * (64 * 1024))
    volumes = root / "volumes"
    volumes.mkdir()
    (volumes / "ws1.ext4").write_bytes(b"z" * (1024 * 1024))
    image_dir = root / "images" / ("a" * 64)
    image_dir.mkdir(parents=True)
    (image_dir / "kernel").write_bytes(b"k" * (3 * 1024 * 1024))
    return root


class FakeImage:
    """The imagestore record shape the report reads."""

    def __init__(self, digest: str, name: str, version: str) -> None:
        self.hash = digest
        self.name = name
        self.version = version


def test_state_usage_reports_the_filesystem(tmp_path: Path) -> None:
    usage = storage.state_usage(tmp_path)
    assert usage is not None
    assert usage["total"] > 0
    assert usage["free"] > 0
    assert 0 <= usage["used"] <= usage["total"]


def test_state_usage_unprobeable_path_answers_none(tmp_path: Path) -> None:
    assert storage.state_usage(tmp_path / "absent") is None


def test_pressure_floor_outranks_the_percentage() -> None:
    usage = {
        "total": 100 * storage.MIB,
        "used": 50 * storage.MIB,
        "free": 50 * storage.MIB,
    }
    # At the floor exactly: critical, not warn (50% used).
    assert storage.pressure_for(usage, 90, 50) == "critical"
    # One byte above the floor and below the warn line: ok.
    roomy = {**usage, "free": usage["free"] + 1, "used": usage["used"] - 1}
    assert storage.pressure_for(roomy, 90, 50) == "ok"


def test_pressure_warn_at_the_percentage() -> None:
    usage = {
        "total": 100 * storage.MIB,
        "used": 91 * storage.MIB,
        "free": 9 * storage.MIB,
    }
    assert storage.pressure_for(usage, 90, 1) == "warn"
    below = {
        "total": 100 * storage.MIB,
        "used": 89 * storage.MIB,
        "free": 11 * storage.MIB,
    }
    assert storage.pressure_for(below, 90, 1) == "ok"


def test_pressure_unknown_when_unprobeable() -> None:
    assert storage.pressure_for(None, 90, 512) == "unknown"
    assert storage.pressure_for({"total": 0, "used": 0, "free": 0}, 90, 1) == (
        "unknown"
    )


def test_file_cost_counts_disk_blocks(tmp_path: Path) -> None:
    # The cost is exactly lstat's st_blocks scaled to bytes — the
    # allocation the filesystem reports, whatever its semantics
    # (ext4 counts blocks; a compressed btrfs counts compressed
    # extents), and a missing file costs nothing.
    dense = tmp_path / "dense.img"
    dense.write_bytes(b"d" * (1024 * 1024))
    assert storage.file_cost(dense) == dense.lstat().st_blocks * 512
    assert storage.file_cost(tmp_path / "absent") == 0


def test_tree_cost_covers_nested_files(tmp_path: Path) -> None:
    entry = tmp_path / "images" / ("a" * 64)
    entry.mkdir(parents=True)
    kernel = entry / "kernel"
    kernel.write_bytes(b"k" * 65536)
    assert storage.tree_cost(entry) == storage.file_cost(kernel)
    assert storage.tree_cost(kernel) == storage.file_cost(kernel)


def test_create_refusal_unprobeable_path_proceeds() -> None:
    # A state dir statvfs cannot reach answers "unknown" pressure —
    # never a refusal (the daemon still serves existing workspaces).
    vmm = VmmSettings(state_dir=Path("/nonexistent-msks-state"))
    assert storage.create_refusal(vmm) is None


def test_create_refusal_at_critical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        storage,
        "state_usage",
        lambda path: {
            "total": 40 * storage.MIB,
            "used": 39 * storage.MIB,
            "free": 100 * 1024 * 1024,
        },
    )
    vmm = VmmSettings(state_dir=tmp_path, storage_floor_mib=512)
    refusal = storage.create_refusal(vmm)
    assert refusal is not None
    assert "100 MiB free" in refusal
    assert "MSKSD_STORAGE_FLOOR_MIB" in refusal
    assert "msks storage" in refusal


def test_create_refusal_clear_at_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        storage,
        "state_usage",
        lambda path: {
            "total": 40 * 1024 * storage.MIB,
            "used": 10 * 1024 * storage.MIB,
            "free": 30 * 1024 * storage.MIB,
        },
    )
    vmm = VmmSettings(state_dir=tmp_path, storage_floor_mib=512)
    assert storage.create_refusal(vmm) is None


def test_storage_report_assembles_all_three_blocks(tmp_path: Path) -> None:
    seed_state_dir(tmp_path)
    rows = [{"id": "ws1", "root_mib": 10240, "home_mib": 2048}]
    images = [FakeImage("a" * 64, "debian", "13")]
    report = storage.storage_report(tmp_path, 90, 512, rows, images)
    assert report["state"]["pressure"] in ("ok", "warn", "critical", "unknown")
    assert report["state"]["floor_mib"] == 512
    assert report["state"]["warn_pct"] == 90
    ws = report["workspaces"][0]
    assert ws["id"] == "ws1"
    assert ws["root_mib"] == 10240
    assert ws["home_mib"] == 2048
    # The root cost is the vm directory's whole tree (overlay plus
    # seed beside it); the home cost is the volume file. Costs are
    # the filesystem's own allocation reports, so the assertions
    # compare against those, not absolute MiB (a host tmp fs may
    # compress or dedupe them).
    overlay = tmp_path / "vms" / "ws1" / "root.qcow2"
    seed = tmp_path / "vms" / "ws1" / "seed.img"
    volume = tmp_path / "volumes" / "ws1.ext4"
    assert ws["root_bytes"] == (
        storage.file_cost(overlay) + storage.file_cost(seed)
    )
    assert ws["home_bytes"] == storage.file_cost(volume)
    image = report["images"][0]
    assert image["name"] == "debian"
    assert image["version"] == "13"
    assert image["bytes"] == storage.tree_cost(
        tmp_path / "images" / ("a" * 64)
    )
    # A workspace with no artifacts on disk costs nothing, not an
    # error — its row still reports.
    rows.append({"id": "ghost", "root_mib": 10240, "home_mib": 2048})
    report = storage.storage_report(tmp_path, 90, 512, rows, [])
    assert report["workspaces"][1]["root_bytes"] == 0
    assert report["workspaces"][1]["home_bytes"] == 0


def test_image_cost_counts_the_retained_archive(tmp_path: Path) -> None:
    """One catalog entry's cost is the whole thing ``msks image rm``
    removes: the cache directory plus the retained archive beside
    it (#184 review — the reclaim decision sees the freed number)."""
    images = tmp_path / "images"
    cache = images / ("a" * 64)
    cache.mkdir(parents=True)
    (cache / "kernel").write_bytes(b"k" * 65536)
    archive = images / f"archive-{'a' * 64}.tar"
    archive.write_bytes(b"t" * 65536)
    image = FakeImage("a" * 64, "debian", "13")
    assert storage.image_cost(tmp_path, image) == (
        storage.file_cost(cache / "kernel") + storage.file_cost(archive)
    )


def test_create_refusal_is_local_driver_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The k8s backend keeps artifacts on per-workspace claims; a
    critical local filesystem never refuses its creates."""
    monkeypatch.setattr(
        storage,
        "state_usage",
        lambda path: {"total": 0, "used": 0, "free": 0},
    )
    vmm = VmmSettings(state_dir=tmp_path, driver="k8s")
    assert storage.create_refusal(vmm) is None
