"""The conformance checker against the real built guest (#258)."""

from pathlib import Path

import pytest
from msks.conformance import (
    ACPI_SHUTDOWN,
    ARCHIVE,
    BOOT,
    CONSOLE,
    HOME_LABEL,
    ROOT_RW,
    USER_DATA,
    check_image,
)

from msks import guestassets
from test_smoke import (
    GUEST_UP_TIMEOUT_S,
    SHUTDOWN_TIMEOUT_S,
    needs_local,
)


def guest_archive() -> Path:
    """The built image archive the checker consumes — whichever
    flavor the surrounding suite's guest assets carry."""
    assets = guestassets.load_guest_assets()
    if assets is None or assets.vmlinux is None:
        pytest.skip("guest assets not built")
    archives = sorted(assets.vmlinux.parent.glob("workspace-*.tar"))
    if not archives:
        pytest.skip("built image archive not present")
    return archives[-1]


@needs_local
async def test_conformance_passes_on_the_built_guest() -> None:
    """The checker's own medicine: every contract point the shipped
    image claims, it passes — boot, prelude console, overlay, home
    volume, seed, ACPI shutdown."""
    rows = await check_image(
        guest_archive(),
        boot_timeout_s=GUEST_UP_TIMEOUT_S,
        shutdown_timeout_s=SHUTDOWN_TIMEOUT_S,
    )
    by_name = {row.name: row for row in rows}
    assert set(by_name) == {
        ARCHIVE,
        BOOT,
        CONSOLE,
        ROOT_RW,
        HOME_LABEL,
        USER_DATA,
        ACPI_SHUTDOWN,
    }
    for row in rows:
        assert row.status == "pass", f"{row.name}: {row.detail}"
