"""The GUI images presenter (#963): catalog and manifest fakes
in, row models out — the two groups' cells, the membership
markers, and the architecture guard. The presenter holds no Qt
import, so these tests drive it as plain Python, the same way
the coverage gate does."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from msks.client import images as images_mod
from msks.client.gui import imagerows as gui_images
from msks.client.tui import importer as tui_importer

A_HASH = "a" * 64
B_HASH = "b" * 64
C_HASH = "c" * 64


def catalog_entry(
    name: str = "debian",
    version: str = "13.6",
    hash: str = A_HASH,
    default: bool = False,
    imported: str | None = "2026-01-02T03:04:05",
) -> dict:
    """One daemon catalog row as the listing serves it (#963's
    fakes): the fields the presenter reads."""
    return {
        "name": name,
        "version": version,
        "hash": hash,
        "default": default,
        "imported": imported,
    }


def manifest_entry(
    ref: str = "debian:13.6",
    url: str = "https://example.com/debian.tar.zst",
    hash: str | None = A_HASH,
    description: str | None = "Debian 13 trixie, cloud kernel",
    size: int | None = 104_857_600,
    built_at: str | None = "2026-01-01T12:00:00Z",
    arch: str | None = "x86_64",
) -> dict:
    """One channel manifest row as the fetch serves it (#963's
    fakes): the pair the import action needs beside the
    optional cells."""
    row = {"ref": ref, "url": url}
    for key, value in (
        ("hash", hash),
        ("description", description),
        ("size", size),
        ("built_at", built_at),
        ("arch", arch),
    ):
        if value is not None:
            row[key] = value
    return row


def test_the_catalog_group_builds_row_models() -> None:
    """The catalog group's row models (#963): one per daemon
    row, in the page's reading order — ref ascending (name,
    then version), and one ref's rows newest import first, so
    the row an operator just imported leads its block."""
    models = gui_images.catalog_rows(
        [
            catalog_entry(name="ubuntu", version="24.04"),
            catalog_entry(imported="2026-01-03T00:00:00"),
            catalog_entry(name="alpine", version="3.21"),
            # The same ref twice — a rebuilt archive: the newer
            # import stands first in the block.
            catalog_entry(hash=B_HASH, imported="2026-01-01T00:00:00"),
        ]
    )
    assert [model.ref for model in models] == [
        "alpine:3.21",
        "debian:13.6",
        "debian:13.6",
        "ubuntu:24.04",
    ]
    assert models[1].hash == A_HASH
    assert models[2].hash == B_HASH
    assert all(isinstance(model, gui_images.CatalogRow) for model in models)


def test_one_catalog_model_carries_what_the_widget_renders() -> None:
    """One catalog model's fields (#963): the ref, the default
    designation the marker column shows, the full hash the
    remove and designate actions send, the 12-character prefix
    cell, and the import date cell off the shared leaf — the
    same cell the CLI table prints."""
    model = gui_images.catalog_row(catalog_entry(default=True))
    assert model == gui_images.CatalogRow(
        ref="debian:13.6",
        default=True,
        hash=A_HASH,
        hash_prefix=A_HASH[:12],
        imported=images_mod.imported_cell(catalog_entry()),
    )
    assert model.imported == "2026-01-02 03:04"


def test_the_import_date_reads_the_shared_cell() -> None:
    """The catalog group's import dates (#963) reuse the shared
    leaf the TUI page and the CLI table read: a stamp renders to
    the minute, and a row without one renders its dash."""
    assert (
        gui_images.catalog_row(catalog_entry()).imported == "2026-01-02 03:04"
    )
    assert gui_images.catalog_row(catalog_entry(imported=None)).imported == "-"
    assert (
        gui_images.catalog_row(catalog_entry(imported="junk")).imported == "-"
    )


def test_the_stamp_less_rows_sort_within_their_block() -> None:
    """The catalog order's floor (#963): a row without a usable
    import stamp — none at all, or a moment the clock cannot
    parse — sorts below every dated row of its ref's block, and
    a naive stamp reads as UTC beside the aware ones."""
    # The naive rule pinned outright: the shared leaf's own
    # ``stamp_moment`` deliberately reads a naive stamp as local
    # time (a different job), so a future unification must not
    # flip this half silently.
    assert gui_images.imported_moment(
        {"imported": "2026-01-01T00:00:00"}
    ) == datetime(2026, 1, 1, tzinfo=UTC)
    models = gui_images.catalog_rows(
        [
            catalog_entry(hash=B_HASH, imported=None),
            catalog_entry(imported="2026-01-02T00:00:00+00:00"),
            catalog_entry(hash=C_HASH, imported="not a stamp"),
            catalog_entry(imported="2026-01-01T00:00:00"),
        ]
    )
    assert [model.hash for model in models] == [
        A_HASH,  # the aware Jan 2 stamp
        A_HASH,  # the naive Jan 1 stamp, read as UTC
        B_HASH,  # no stamp at all
        C_HASH,  # a stamp the clock cannot parse
    ]


def test_the_published_group_builds_row_models() -> None:
    """The published group's row models (#963): one per manifest
    row, in the manifest's own order, each carrying the
    description, the size, the hash prefix, the build date, the
    membership marker, and the url the import action POSTs."""
    catalog = [catalog_entry()]
    models = gui_images.published_rows(
        [
            manifest_entry(),
            manifest_entry(
                ref="alpine:3.21",
                url="https://example.com/alpine.tar.zst",
                hash="d" * 64,
                description=None,
                size=None,
                built_at=None,
            ),
        ],
        catalog,
    )
    assert models == [
        gui_images.PublishedRow(
            ref="debian:13.6",
            description="Debian 13 trixie, cloud kernel",
            size="100 MiB",
            hash_prefix=A_HASH[:12],
            built="2026-01-01",
            marker=gui_images.IMPORTED_MARKER,
            url="https://example.com/debian.tar.zst",
        ),
        gui_images.PublishedRow(
            ref="alpine:3.21",
            description="-",
            size="-",
            hash_prefix="d" * 12,
            built="-",
            marker="",
            url="https://example.com/alpine.tar.zst",
        ),
    ]


def test_the_membership_markers_read_the_hash_comparison() -> None:
    """The membership markers (#565, the GUI's read): a channel
    import's catalog hash equal to the manifest row's hash is
    ``[imported]``; the same ref under a different hash — the
    pre-channel plain tar, a locally built archive — is
    ``[local]``; an unrelated row carries no marker. A rebuild
    must not make the marker lie, and a row without a hash
    falls to the ref comparison."""
    catalog = [catalog_entry()]
    assert (
        gui_images.membership(manifest_entry(), catalog)
        == gui_images.IMPORTED_MARKER
    )
    assert (
        gui_images.membership(manifest_entry(hash=B_HASH), catalog)
        == gui_images.LOCAL_MARKER
    )
    assert (
        gui_images.membership(
            manifest_entry(ref="alpine:3.21", hash="e" * 64), catalog
        )
        == ""
    )
    assert (
        gui_images.membership(manifest_entry(hash=None), catalog)
        == gui_images.LOCAL_MARKER
    )
    assert gui_images.IMPORTED_MARKER == "[imported]"
    assert gui_images.LOCAL_MARKER == "[local]"


def test_the_architecture_guard_excludes_foreign_rows() -> None:
    """The published group keeps the rows the daemon's own
    machine can boot (#717): a row declaring the other
    architecture stays off, a row declaring none shows
    everywhere, and no machine word at all steps the guard
    aside."""
    manifest = [
        manifest_entry(ref="amd64:1", arch="x86_64"),
        manifest_entry(ref="arm64:1", arch="aarch64"),
        manifest_entry(ref="any:1", arch=None),
    ]
    kept = gui_images.served_rows(manifest, "x86_64")
    assert [row["ref"] for row in kept] == ["amd64:1", "any:1"]
    assert [row["ref"] for row in gui_images.served_rows(manifest, None)] == [
        "amd64:1",
        "arm64:1",
        "any:1",
    ]


def test_the_guard_reads_the_daemons_machine() -> None:
    """The machine word off the daemon's /health document
    (#717): the uname machine itself, or None when the document
    carries no word at all — nothing served, an empty string, a
    mistyped value."""
    assert gui_images.daemon_machine(None) is None
    assert gui_images.daemon_machine({}) is None
    assert gui_images.daemon_machine({"machine": ""}) is None
    assert gui_images.daemon_machine({"machine": 7}) is None
    assert gui_images.daemon_machine({"machine": "x86_64"}) == "x86_64"


def test_one_rows_arch_declared_or_not() -> None:
    """The per-row half of the guard (#717, the #638 rule): a
    row whose arch is absent, null, or empty declares nothing
    and serves any machine; a declared arch serves its match
    alone — truthy junk serves nothing."""
    assert gui_images.row_serves_arch({"arch": "aarch64"}, None)
    assert gui_images.row_serves_arch({}, "x86_64")
    assert gui_images.row_serves_arch({"arch": ""}, "x86_64")
    assert gui_images.row_serves_arch({"arch": None}, "x86_64")
    assert gui_images.row_serves_arch({"arch": "x86_64"}, "x86_64")
    assert gui_images.row_serves_arch({"arch": "aarch64"}, "x86_64") is False
    assert gui_images.row_serves_arch({"arch": 3}, "x86_64") is False


def test_the_manifest_rows_validate_by_name() -> None:
    """The manifest row's validation (#565): the ref/url pair is
    required, every present field answers its type, an absent
    or null optional field passes, and a mistyped value refuses
    by name — a broken channel, not a crash."""
    for broken in (
        {},
        {"ref": "x:1"},
        {"url": "u"},
        {"ref": "", "url": "u"},
        ["not", "a", "mapping"],
    ):
        with pytest.raises(ValueError, match="manifest row lacks ref/url"):
            gui_images.checked_row(broken)
    for field in ("ref", "url", "description", "hash", "built_at", "arch"):
        row = manifest_entry()
        row[field] = 7
        with pytest.raises(
            ValueError, match=f"{field} must be a string, got int"
        ):
            gui_images.checked_row(row)
    for size in (True, "big"):
        row = manifest_entry(size=1)
        row["size"] = size
        with pytest.raises(
            ValueError,
            match=f"size must be a number, got {type(size).__name__}",
        ):
            gui_images.checked_row(row)
    # The passing shapes: the full row, and the bare pair with
    # every optional field absent.
    assert gui_images.checked_row(manifest_entry()) == manifest_entry()
    assert gui_images.checked_row({"ref": "x:1", "url": "u"}) == {
        "ref": "x:1",
        "url": "u",
    }


def test_the_cells_read_their_dash_floors() -> None:
    """The published cells' floors (#963): the size in whole
    MiB with a dash for none or nonsense, the build date as the
    stamp's date half with a dash for none, the hash prefix at
    12 characters with a dash for none."""
    assert gui_images.size_label(104_857_600) == "100 MiB"
    assert gui_images.size_label(524_288) == "0 MiB"  # half a MiB rounds down
    assert gui_images.size_label(None) == "-"
    assert gui_images.size_label(0) == "-"
    assert gui_images.size_label(-5) == "-"
    assert gui_images.built_label({"built_at": "2026-01-01T12:00:00Z"}) == (
        "2026-01-01"
    )
    assert gui_images.built_label({}) == "-"
    assert gui_images.hash_cell("z" * 64) == "z" * 12
    assert gui_images.hash_cell("short") == "short"
    assert gui_images.hash_cell(None) == "-"
    assert gui_images.hash_cell("") == "-"


def test_the_helpers_match_the_tui_page() -> None:
    """The mirrored helpers keep behavior parity with the TUI
    page (#963): over the same catalog and manifest fakes, both
    clients read the same membership markers, the same
    architecture guard, the same machine word, the same cells,
    and the same reading order — a formatting change on one
    side must fail here rather than drift the two pages
    apart."""
    catalog = [
        catalog_entry(),
        catalog_entry(name="ubuntu", version="24.04", hash=B_HASH),
    ]
    manifest = [
        manifest_entry(),
        manifest_entry(ref="ubuntu:24.04", hash=B_HASH),
        manifest_entry(ref="ubuntu:24.04", hash=C_HASH),
        manifest_entry(ref="alpine:3.21", hash=None),
        # An undeclared arch and the other architecture, so the
        # guard's falsy-arch arm compares against the TUI's own.
        manifest_entry(ref="any:1", arch=None),
        manifest_entry(ref="arm64:1", arch="aarch64"),
    ]
    for row in manifest:
        assert gui_images.membership(row, catalog) == tui_importer.membership(
            row, catalog
        )
    for row in manifest:
        for machine in (None, "x86_64", "aarch64"):
            assert gui_images.row_serves_arch(
                row, machine
            ) == tui_importer.row_serves_arch(row, machine)
    for health in (None, {}, {"machine": "x86_64"}, {"machine": 7}):
        assert gui_images.daemon_machine(
            health
        ) == tui_importer.daemon_machine(health)
    for size in (None, 0, -5, 524_288, 104_857_600):
        assert gui_images.size_label(size) == tui_importer.size_label(size)
    for row in manifest:
        assert gui_images.built_label(row) == tui_importer.built_label(row)
        assert gui_images.hash_cell(row.get("hash")) == tui_importer.hash_cell(
            row.get("hash")
        )
    for stamp in (
        None,
        "junk",
        "2026-01-01T00:00:00",
        "2026-01-02T00:00:00+00:00",
    ):
        row = catalog_entry(imported=stamp)
        assert gui_images.imported_moment(row) == tui_importer.imported_moment(
            row
        )
    assert gui_images.sorted_catalog(catalog) == tui_importer.sorted_catalog(
        catalog
    )


def test_the_validation_matches_the_tui_page() -> None:
    """The mirrored validation keeps parity with the TUI page
    (#963): over the same corpus of broken and passing rows,
    both clients refuse alike — the same messages, so a
    rewording on one side fails here rather than drift the two
    channels apart — and accept the same shapes."""
    broken = [
        {},
        {"ref": "x:1"},
        {"url": "u"},
        {"ref": "", "url": "u"},
        ["not", "a", "mapping"],
        {"ref": "x:1", "url": 5},
        {"ref": 3, "url": "u"},
    ]
    for field in ("description", "hash", "built_at", "arch"):
        row = manifest_entry()
        row[field] = 7
        broken.append(row)
    for size in (True, "big", 1.5):
        row = manifest_entry(size=1)
        row["size"] = size
        broken.append(row)
    for row in broken:
        with pytest.raises(ValueError) as gui_exc:
            gui_images.checked_row(row)
        with pytest.raises(ValueError) as tui_exc:
            tui_importer.manifest_row(row)
        assert str(gui_exc.value) == str(tui_exc.value)
    for row in (
        manifest_entry(),
        {"ref": "x:1", "url": "u"},
        {"ref": "x:1", "url": "u", "description": None, "size": None},
    ):
        assert gui_images.checked_row(row) == tui_importer.manifest_row(row)


def test_the_module_imports_nothing_from_pyside6() -> None:
    """The presenter stays Qt-free (#963): the widget module
    owns every Qt import, and this module's plain data keeps the
    coverage gate — and a base install's parse path, through the
    package init's pin — off the binding."""
    tree = ast.parse(Path(gui_images.__file__).read_text())
    roots = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(root.split(".")[0] == "PySide6" for root in roots)
