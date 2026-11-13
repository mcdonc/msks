"""The entry: argument parsing and the run.

The shape arguments a create takes, the run that composes the
phases in order, the uninstall closing line, and ``main`` the
console script calls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import ensure_agent, remove_agent
from .base import PORT, SHAPE_FLOORS, TOOL, VM_NAME, MacSetupError, say
from .bootstrap import bootstrap_vm, refuse_dev_build
from .hostcheck import check_host
from .links import link_client_mac, remove_client_link
from .skew import check_rows
from .verify import client_preset, verify_daemon
from .vm import adopted_port, delete_vm, ensure_vm

# --- the entry --------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The surface: the wheel override for a build in hand, the
    check-only pass, the uninstall mirror, and the VM sizing
    flags (#721)."""
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description=(
            "Prepare a macOS host for msksd (#676): install the "
            "Lima VM, bootstrap the daemon inside it, and print "
            "the client preset."
        ),
    )
    parser.add_argument(
        "--wheel",
        metavar="PATH",
        help="install this wheel into the VM instead of the PyPI "
        "release matching this tool's version (a build in hand)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="the daemon's host port, both loopbacks; a run "
        "without it adopts the existing VM's forward (a fresh "
        f"creation takes {PORT})",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=None,
        metavar="N",
        help="the VM's CPUs, at creation and on reruns (the floor "
        f"is {SHAPE_FLOORS['cpus']}; without the flag the "
        "template's own value stands)",
    )
    parser.add_argument(
        "--memory",
        type=int,
        default=None,
        metavar="GIB",
        help="the VM's memory in GiB, at creation and on reruns "
        f"(the floor is {SHAPE_FLOORS['memory']}; without the "
        "flag the template's own value stands)",
    )
    parser.add_argument(
        "--disk",
        type=int,
        default=None,
        metavar="GIB",
        help="the VM's disk in GiB — it only grows: a larger value "
        "resizes the VM's disk, a smaller one refuses; at creation "
        f"and on reruns (the floor is {SHAPE_FLOORS['disk']}; "
        "changed values restart the VM)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only: the doctor rows, nothing mutated",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="stop and delete the msksd VM, its login agent, and "
        "the brew-prefix client links (Lima and the client config "
        "stay, and the summary names them)",
    )
    return parser.parse_args(argv)


def shape_from_args(args: argparse.Namespace) -> dict | None:
    """The sizing flags the operator named (#721), in the
    template's own line order, or None when the run carries none
    — the template's values then stand."""
    shape = {
        key: getattr(args, key)
        for key in ("cpus", "memory", "disk")
        if getattr(args, key) is not None
    }
    return shape or None


def validate_shape(shape: dict | None) -> dict | None:
    """The sizing floors (#721): a value below its floor refuses
    before any phase runs — the host check, the install, the VM —
    so a mistyped flag never spends a boot on a VM that cannot
    hold the daemon and a workspace."""
    for key, value in (shape or {}).items():
        floor = SHAPE_FLOORS[key]
        if value < floor:
            unit = "CPUs" if key == "cpus" else "GiB"
            raise MacSetupError(
                f"--{key} {value} is below the floor of {floor} {unit} "
                "(the VM must hold the daemon and a workspace)"
            )
    return shape


def run_setup(args: argparse.Namespace) -> str:
    """The phases in order; the preset block returns for the
    caller to print. The verify pass runs before the client
    files and the login agent so a green run is what they
    provision — the preset's token and CA are real, and the agent
    earns its login slot. The port the phases share is the one
    ``ensure_vm`` resolves: the explicit ``--port``, or the
    forward the existing VM already carries (#699); the sizing
    flags (#721) apply inside it — a changed value restarts the
    VM, equal values change nothing. A dev build
    without a wheel refuses after the platform gate and before
    the VM creation (#704) — nothing the run builds is wasted —
    and the sizing floors refuse before every phase, the host
    check included (#721 review: a mistyped flag never spends a
    brew install first)."""
    shape = validate_shape(shape_from_args(args))
    check_host()
    refuse_dev_build(args.wheel)
    home = str(Path.home())
    port = ensure_vm(host_port=args.port, shape=shape)
    bootstrap_vm(args.wheel, home)
    verify_daemon(port)
    preset = client_preset(port)
    ensure_agent()
    say(link_client_mac())
    return preset


def uninstall_closing_line(link_note: str) -> str:
    """The uninstall arm's closing print: the removal sentence
    gates on the link's note (#719 review) — a sweep that found
    no link says so in the parenthetical instead of claiming
    one gone."""
    if "gone" in link_note:
        return (
            f"{TOOL}: the {VM_NAME} VM, its login agent, and the "
            f"brew-prefix client links are gone. "
            "Lima itself, the client config, and the CA and token "
            "under ~/.config/msks/ stay; remove them by hand when "
            "you want them gone."
        )
    return (
        f"{TOOL}: the {VM_NAME} VM and its login agent are gone "
        f"({link_note}). "
        "Lima itself, the client config, and the CA and token "
        "under ~/.config/msks/ stay; remove them by hand when "
        "you want them gone."
    )


def refuse_readonly_shape(args: argparse.Namespace) -> None:
    """The read-only passes take no sizing flags (#721): --check
    and --uninstall mutate nothing, so a flag that would change
    the VM refuses before any phase runs."""
    if not (args.check or args.uninstall):
        return
    if shape_from_args(args) is None:
        return
    raise MacSetupError(
        "--check and --uninstall mutate nothing; the sizing "
        "flags belong to the setup pass"
    )


def main(argv: list[str] | None = None) -> int:
    """The console entry: 0 on success, 1 on a named refusal; the
    preset and the check rows print on stdout, progress and
    refusals on stderr."""
    args = parse_args(argv)
    try:
        refuse_readonly_shape(args)
        if args.uninstall:
            say(f"removing the login agent and the {VM_NAME} VM")
            remove_agent()
            delete_vm(VM_NAME)
            print(uninstall_closing_line(remove_client_link()))
            return 0
        if args.check:
            check_host(install=False)
            for row in check_rows(
                adopted_port(VM_NAME, args.port), install=False
            ):
                print(f"  ok  {row}")
            return 0
        preset = run_setup(args)
        print(preset)
        return 0
    except MacSetupError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 1
