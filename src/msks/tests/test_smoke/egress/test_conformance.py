"""The conformance checker's egress pass, under root (#258)."""

from msks.conformance import EGRESS_DHCP, check_image

from test_smoke import (
    GUEST_UP_TIMEOUT_S,
    SHUTDOWN_TIMEOUT_S,
    needs_egress,
    needs_local,
)
from test_smoke.core.test_conformance import guest_archive


@needs_egress
@needs_local
async def test_conformance_egress_pass_on_the_built_guest() -> None:
    """The --egress leg against the real net stack: the checker's
    DHCP point passes on the shipped image, beside a green core."""
    rows = await check_image(
        guest_archive(),
        egress=True,
        boot_timeout_s=GUEST_UP_TIMEOUT_S,
        shutdown_timeout_s=SHUTDOWN_TIMEOUT_S,
    )
    egress = next(row for row in rows if row.name == EGRESS_DHCP)
    assert egress.status == "pass", egress.detail
    for row in rows:
        if row.name != EGRESS_DHCP:
            assert row.status == "pass", f"{row.name}: {row.detail}"
