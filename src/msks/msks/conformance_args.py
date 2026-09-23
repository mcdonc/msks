"""The ``image check`` flags (#258), shared by the two entries.

The client CLI and the standalone ``python -m msks.conformance``
parse the same surface; the definitions live in this leaf module
(argparse only — no daemon composition) so the client can import
them at module scope without paying for ``msks.app``.
"""

import argparse


def check_arguments(parser: argparse.ArgumentParser) -> None:
    """The check subcommand's flags."""
    parser.add_argument("archive", help="the container-image tar to check")
    parser.add_argument(
        "--egress",
        action="store_true",
        help="also verify DHCP address acquisition through the "
        "daemon's net stack (requires root)",
    )
    parser.add_argument(
        "--uplink",
        default=None,
        help="uplink interface for --egress (default: the default route)",
    )
    parser.add_argument(
        "--boot-timeout-s",
        type=float,
        default=120.0,
        help="deadline for each boot and seed wait (default: 120)",
    )
    parser.add_argument(
        "--shutdown-timeout-s",
        type=float,
        default=120.0,
        help="deadline for the ACPI power-button shutdown (default: 120)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the throwaway state dir (serial logs) for inspection",
    )
