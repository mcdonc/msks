"""Phase 2: the msksd VM.

Created from the template that ships in the wheel with the
daemon's port forward appended; the shape edits a re-run asks
for, the disk shrink it refuses, and the adoption of an existing
instance's own port.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path

from .base import PORT, TOOL, VM_NAME, VM_PORT, MacSetupError, run, say

# --- phase 2: the msksd VM -------------------------------------------


def instance_yaml(host_port: int, shape: dict | None = None) -> str:
    """The instance config: the shipped template with the daemon's
    port forward appended — the one template piece this tool owns
    (a deployment choice, not image geometry) — and the flagged
    sizing rendered over the template's own three lines (#721),
    so a fresh creation carries the operator's CPUs, memory, and
    disk instead of the defaults. The forward binds
    both loopbacks: the daemon serves the guest's 127.0.0.1 and
    the client dials the Mac's."""
    template = (
        files("msks.server").joinpath("assets/msksd-vm.yaml").read_text()
    )
    for key, value in (shape or {}).items():
        unit = "" if key == "cpus" else "GiB"
        template = re.sub(
            rf"^{key}: .+$",
            f"{key}: {value}{unit}",
            template,
            count=1,
            flags=re.MULTILINE,
        )
    forward = (
        "\n# The daemon's TLS listener (#676): the forward this tool\n"
        "# appends — both loopbacks, the course VM's verified shape.\n"
        "portForwards:\n"
        f"- guestPort: {VM_PORT}\n"
        '  guestIP: "127.0.0.1"\n'
        f"  hostPort: {host_port}\n"
        '  hostIP: "127.0.0.1"\n'
    )
    return template + forward


def vm_entry(name: str) -> dict | None:
    """This tool's entry from ``limactl list --json`` — the
    listing parsed, the one VM named, so a status read never
    borrows another VM's (the course VM runs beside this one on
    dev Macs)."""
    listing = run(["limactl", "list", "--json"], capture=True)
    for entry in listing_entries(listing):
        if entry.get("name") == name:
            return entry
    return None


def listing_entries(listing: str) -> list:
    """The entries a ``limactl list --json`` listing carries: one
    object per line is limactl's shape; a whole array on one line
    parses to a list all the same."""
    entries = []
    for line in listing.splitlines():
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except ValueError:
            continue
        entries.extend(decoded if isinstance(decoded, list) else [decoded])
    return entries


def forward_host_port(name: str) -> int | None:
    """The host port the VM's own config forwards for the daemon
    — read from the instance config Lima wrote at creation, so a
    mid-life ``--port`` change is a named refusal (the forward is
    fixed at creation), never a silent wrong port."""
    config = Path.home() / ".lima" / name / "lima.yaml"
    try:
        text = config.read_text()
    except OSError:
        return None
    match = re.search(
        rf"guestPort: {VM_PORT}\n.*?hostPort: (\d+)", text, re.DOTALL
    )
    return int(match.group(1)) if match else None


def resolved_port(explicit: int | None, forward: int | None) -> int:
    """The host port a run talks through: an explicit ``--port``
    stands (a disagreement with the existing forward is the
    caller's named refusal); with none, the VM's own forward —
    fixed at creation — is this deployment's port (#699); a VM
    without a readable forward (none exists yet, or Lima never
    wrote the config) falls back to the tool's default."""
    if explicit is not None:
        return explicit
    return forward if forward is not None else PORT


def instance_shape(name: str) -> dict | None:
    """The VM's current sizing — CPUs, memory and disk in GiB —
    from the instance config Lima wrote at creation (the same
    file the port adoption reads), or None when it carries none
    of the three lines: a rerun's sizing flags then refuse by
    name rather than guess at the grow-only rule."""
    config = Path.home() / ".lima" / name / "lima.yaml"
    try:
        text = config.read_text()
    except OSError:
        return None
    shape = {}
    for key, pattern in (
        ("cpus", r"^cpus: (\d+)"),
        ("memory", r"^memory: (\d+)GiB"),
        ("disk", r"^disk: (\d+)GiB"),
    ):
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            return None
        shape[key] = int(match.group(1))
    return shape


def planned_changes(
    shape: dict, current: dict | None, name: str = VM_NAME
) -> dict:
    """The flagged values that differ from the VM's current sizing
    (#721): equal values drop — the rerun's no-op — and the disk
    only grows (:func:`refuse_disk_shrink` names both sizes; Lima
    resizes the data disk upward at the next start, and a shrink
    would refuse inside Lima with less to say). The caller stands
    down before the read when the run carries no flags."""
    if current is None:
        config = Path.home() / ".lima" / name / "lima.yaml"
        raise MacSetupError(
            f"the {name} VM's current sizing could not be read "
            f"({config}); the sizing flags need it — re-run without "
            "them, or inspect the file"
        )
    changes = {}
    for key, value in shape.items():
        refuse_disk_shrink(key, value, current)
        if value != current[key]:
            changes[key] = value
    return changes


def refuse_disk_shrink(key: str, value: int, current: dict) -> None:
    """The grow-only rule (#721): the disk's value may grow, and
    a smaller one refuses naming both sizes with the reclaim
    remedy beside it."""
    if key == "disk" and value < current["disk"]:
        raise MacSetupError(
            f"--disk {value} GiB would shrink the VM's "
            f"{current['disk']} GiB disk; the disk only grows — "
            "reclaim space with `msks storage` instead"
        )


def say_shape(name: str, changes: dict) -> None:
    """The restart's own line: what changes, and what the restart
    does to running workspaces — they stop with the VM, and each
    starts again through the client (#721 review: nothing
    auto-returns a workspace; the line says so)."""
    words = ", ".join(
        f"{key} {value}" + ("" if key == "cpus" else " GiB")
        for key, value in changes.items()
    )
    say(
        f"applying {words} to the {name} VM; the VM restarts — "
        "running workspaces stop with it, and each starts again "
        "with `msks start`"
    )


def edit_command(name: str, changes: dict) -> list[str]:
    """The limactl edit that carries the changed values — only
    the changed flags, in the flags' own order."""
    edit = ["limactl", "edit", name]
    for key, value in changes.items():
        edit += [f"--{key}", str(value)]
    return edit


def apply_shape(name: str, shape: dict | None, running: bool) -> bool:
    """Bring an existing VM to the flagged sizing (#721): changed
    values stop the VM (a stopped one skips the stop), edit the
    instance, and start it again. A failure between the stop and
    the start names the recovery. Equal values change nothing (the
    rerun no-op), and no flags at all stand down before the
    instance config is even read."""
    if not shape:
        return False
    changes = planned_changes(shape, instance_shape(name), name)
    if not changes:
        return False
    say_shape(name, changes)
    if running:
        run(["limactl", "stop", name])
    try:
        run(edit_command(name, changes))
        run(["limactl", "start", name])
    except MacSetupError as exc:
        raise MacSetupError(
            f"{exc}; the {name} VM sits stopped mid-resize — "
            f"limactl start {name} brings it back, or re-run the "
            "tool without the sizing flags"
        ) from None
    return True


def create_vm(name: str, port: int, shape: dict | None = None) -> int:
    """The first-sight creation: the rendered instance config
    (template, this tool's forward, and any flagged sizing, #721)
    starts the VM through limactl from a temp path — the image
    downloads on first run — and the creation port returns for
    the phases after."""
    say(f"creating the {name} VM (the image downloads on first run)")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as handle:
        handle.write(instance_yaml(port, shape))
        path = handle.name
    try:
        run(["limactl", "start", "--name", name, path])
    finally:
        os.unlink(path)
    return port


def adopted_port(name: str, host_port: int | None) -> int:
    """The port an existing VM's run talks through (#699): the
    explicit ``--port`` when it agrees with the fixed-at-creation
    forward (a disagreement is the named recreation refusal,
    never a silent wrong port), the VM's own forward when no
    ``--port`` was named — the deployment chose it at creation —
    and the tool's default when the forward reads nothing (the
    verify pass then catches a wrong port)."""
    forward = forward_host_port(name)
    if forward is None:
        return resolved_port(host_port, None)
    if host_port is None:
        if forward != PORT:
            say(
                f"the existing {name} VM forwards host port {forward}; "
                "the run continues on it"
            )
        return forward
    if forward != host_port:
        raise MacSetupError(
            f"the {name} VM forwards host port {forward} (fixed at "
            f"creation); --port {host_port} wants a recreation: "
            f"limactl delete -f {name} && re-run {TOOL}"
        )
    return host_port


def ensure_vm(
    name: str = VM_NAME,
    host_port: int | None = None,
    shape: dict | None = None,
) -> int:
    """The VM exists and runs, and the run's host port returns:
    created from the rendered instance config on first sight (the
    explicit port when one was named, the default otherwise),
    brought to the flagged sizing when the values differ (#721 —
    a changed value restarts the VM with them), started when
    stopped, left alone when running. The port for
    an existing VM resolves before the sizing touches anything
    (#721 review: :func:`adopted_port` is a pure read, and its
    recreation refusal costs no restart) — an explicit
    ``--port`` that disagrees with the forward (fixed at creation)
    refuses naming the recreation remedy; with no ``--port``, the
    run adopts the forward the VM carries — the deployment
    already chose it at creation (#699)."""
    entry = vm_entry(name)
    if entry is None:
        return create_vm(name, resolved_port(host_port, None), shape)
    port = adopted_port(name, host_port)
    running = entry.get("status") == "Running"
    changed = apply_shape(name, shape, running)
    if not changed and not running:
        say(f"starting the stopped {name} VM")
        run(["limactl", "start", name])
    return port


def delete_vm(name: str) -> None:
    """Delete the VM through limactl (#701): a limactl that is
    nowhere on PATH — a stock Mac, or a Lima removed by hand —
    is the named install-again remedy, never the raw
    FileNotFoundError the subprocess raises (a present limactl
    that answers nonzero already refuses by name through
    run())."""
    try:
        run(["limactl", "delete", "-f", name])
    except FileNotFoundError:
        raise MacSetupError(
            "limactl is not on PATH; install Lima (brew install "
            "lima), then --uninstall again — the VM's files live "
            f"under ~/.lima/{name} for removal by hand"
        ) from None


# --- phase 3: the in-VM bootstrap ------------------------------------
