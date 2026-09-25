"""The ``image check`` surface (#258), shared by the two entries.

The client CLI (typer, #315) and the standalone
``python -m msks.conformance`` (argparse) parse the same surface;
the field names, defaults, and help lines live in this leaf module
(no daemon composition) so the client can import them at module
scope without paying for ``msks.app``, and the two entries can
never drift apart.
"""

import argparse
from dataclasses import dataclass

#: Help lines shared by both parsers: one source, so a wording
#: change lands on both entries at once.
CHECK_ARCHIVE_HELP = "the container-image tar to check"
CHECK_EGRESS_HELP = (
    "also verify DHCP address acquisition through the "
    "daemon's net stack (requires root)"
)
CHECK_UPLINK_HELP = (
    "uplink interface for --egress (default: the default route)"
)
CHECK_BOOT_TIMEOUT_HELP = "deadline for each boot and seed wait (default: 120)"
CHECK_SHUTDOWN_TIMEOUT_HELP = (
    "deadline for the ACPI power-button shutdown (default: 120)"
)
CHECK_KEEP_HELP = "keep the throwaway state dir (serial logs) for inspection"


@dataclass
class CheckOptions:
    """The check surface's values, attribute-shaped for
    :func:`msks.conformance.run_check` — the typer layer builds one
    from its parsed flags, the standalone parser fills the same
    fields."""

    archive: str
    egress: bool = False
    uplink: str | None = None
    boot_timeout_s: float = 120.0
    shutdown_timeout_s: float = 120.0
    keep: bool = False


def check_arguments(parser: argparse.ArgumentParser) -> None:
    """The standalone check entry's flags (the CLI declares the
    same surface with the shared help lines above)."""
    parser.add_argument("archive", help=CHECK_ARCHIVE_HELP)
    parser.add_argument(
        "--egress", action="store_true", help=CHECK_EGRESS_HELP
    )
    parser.add_argument("--uplink", default=None, help=CHECK_UPLINK_HELP)
    parser.add_argument(
        "--boot-timeout-s",
        type=float,
        default=120.0,
        help=CHECK_BOOT_TIMEOUT_HELP,
    )
    parser.add_argument(
        "--shutdown-timeout-s",
        type=float,
        default=120.0,
        help=CHECK_SHUTDOWN_TIMEOUT_HELP,
    )
    parser.add_argument("--keep", action="store_true", help=CHECK_KEEP_HELP)
