"""``msks-setup-macos-host`` (#676): the macOS host setup — the
tool that takes a stock Apple-Silicon Mac to a working msksd.

The Linux setup tools (``msks-setup-debian-host``, ``-fedora-``)
prepare a host msksd runs on directly; on macOS the daemon runs
inside its own Lima VM, so this tool's phases compose the pieces
the other tools never see:

1. **The host check** — Apple Silicon (the guest images are
   aarch64 and nested virtualization needs the Apple Virtualization
   framework), and a Lima at the template's floor (2.2.1, the
   version the template's own expansions were verified against).
   A Lima the Mac does not carry installs through brew (#699) —
   the stock-Mac step this tool runs itself; a Mac without brew
   names the Homebrew line and stops.
2. **The msksd VM** — created from the template that ships in the
   wheel (``server/assets/msksd-vm.yaml``, #671: vz, nested
   virtualization, the invoking user's home mounted under the
   reserved ``/mnt/msks`` prefix) with the daemon's port forward
   appended — the one piece the template leaves to this tool,
   because the port is a deployment choice. A run without
   ``--port`` talks through the forward the existing VM already
   carries (#699: the forward is fixed at creation, so the VM's
   own answer is this deployment's port); an explicit ``--port``
   that disagrees with it stays the named recreation refusal.
3. **The in-VM bootstrap** — the daemon's Debian tool set
   (egress plumbing, volume trio, seed tooling) and its kernel
   routing setting, the pinned cloud-hypervisor static release,
   the wheel installed into /opt/msks
   (the PyPI release matching this tool's version, or ``--wheel``
   for a build in hand), ``/etc/msksd.yaml`` written (the daemon
   on the forwarded port, host-share mode ``prefix`` — the
   reserved-prefix geometry the template mounted, the invoking
   user's home the default allowed root), and a systemd unit that
   starts msksd at boot.
4. **The verify pass** — the daemon's TLS listener answers
   through the forward; the CA and the bootstrap token come back
   out, the CA written under ``~/.config/msks/``.
5. **The client config and the login agent** — ``msks.yaml`` itself
   (the client's own config tree): a daemon alias pointing a bare
   ``msks`` at the forward (``url``, ``token_file``, ``cafile`` —
   the token written beside the CA), so every CLI invocation
   reaches the daemon with no exports in the shell profile; and a
   launchd agent that runs ``limactl start msksd`` at login —
   kickstarted when installed, so the VM starts through the
   agent immediately too — so the daemon answers after a reboot.
   The printed exports remain
   as a copy-paste fallback, and a config this tool never wrote is
   kept untouched — the run names the file and prints the alias
   block to paste into it; a file this tool wrote is refreshed on
   re-run (its header says so, operator edits included).

``--check`` runs the verify pass alone — the macOS doctor row
set: Lima, the VM, the nested-virtualization marker, virtiofsd
inside the VM, the listener, the client config, the login agent
— and mutates nothing: a missing Lima refuses with the
run-the-tool remedy, never an install.
``--uninstall`` stops and deletes the VM and its login agent
(Lima and the client config stay, and the summary names
them). Every phase is idempotent: re-run any time; a rerun
preserves the token, the CA, and the daemon's state.
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib.resources import files
from pathlib import Path

from ..client.config import (
    default_config_path,
    parse_config_doc,
    render_template,
)
from ..spec.version import __version__
from .tls import CA_CERT

TOOL = "msks-setup-macos-host"

#: The Lima instance name. The course VM (#649) is ``msks``; the
#: daemon VM keeps its own name so the two never collide.
VM_NAME = "msksd"

#: The daemon's port inside the VM (its own default) and on the
#: Mac — the forward binds both loopbacks (the daemon binds the
#: guest's 127.0.0.1, Lima forwards the host's 127.0.0.1; the
#: course VM verified the shape live). The host port is a
#: ``--port`` choice: anything else on the Mac already answering
#: there — another VM's forward, a local daemon — makes the
#: verify pass name the collision and the operator picks
#: another. A run without ``--port`` adopts the forward the
#: existing VM carries (#699); a fresh creation renders this
#: default.
VM_PORT = 8660
PORT = 8660

#: The template's Lima floor (#671) — the version whose mount
#: expansions (``~`` host-side, ``{{.User}}`` guest-side) were
#: verified. Older Limas render the mountPoint literally.
LIMA_FLOOR = (2, 2, 1)

#: Where the wheel installs inside the VM and the paths the unit
#: and the config read. The venv is uv-managed Python: the wheel
#: targets the project's Python floor (3.14) and Debian trixie
#: ships 3.13, so the bootstrap provisions the standalone 3.14
#: through uv rather than fighting the distro's interpreter.
VM_PREFIX = "/opt/msks"

#: The daemon's own tool set — the packages a Debian deployment
#: host installs (hostsetup's map): the egress plumbing (nft,
#: ip, conntrack), the volume trio, the seed's iso tooling, the
#: client workflows' pair, and the console's multiplexer. The
#: daemon execs them by name; a missing one is a workspace-start
#: refusal naming it.
VM_PACKAGES = (
    "python3-venv virtiofsd nftables iproute2 conntrack "
    "e2fsprogs qemu-utils genisoimage openssh-client rsync tmux"
)

#: The VMM the daemon boots workspaces with: upstream's static
#: aarch64 release (the tool is Apple-Silicon-only, one arch),
#: pinned by sha256 the way the repro suites pin theirs — Debian
#: carries cloud-hypervisor in no suite.
CH_URL = (
    "https://github.com/cloud-hypervisor/cloud-hypervisor/"
    "releases/download/v54.0/cloud-hypervisor-static-aarch64"
)
CH_SHA256 = "e8a6d9786ea678e739f1a386682846e93742dcb9c7b6e3e0c62e3bda6cff0ef3"

#: The uv release the bootstrap installs from PyPI — pinned like
#: every fetched artifact; uv provisions the VM's Python 3.14
#: from its own standalone builds.
UV_PIN = "0.9.7"
VM_CONF = "/etc/msksd.yaml"
VM_UNIT = "/etc/systemd/system/msksd.service"
VM_STATE = "/var/lib/msksd"

#: Where the CA lands on the Mac — beside the client config the
#: preset's CAFILE names.
MAC_CA = Path("~/.config/msks/msksd-vm-ca.pem").expanduser()

#: Where the bootstrap token lands, beside the CA: the managed
#: config's ``token_file`` names it (the config tree stays free
#: of inline credentials — a file is the client's own shape).
MAC_TOKEN = Path("~/.config/msks/msksd-vm.token").expanduser()

#: The daemon alias the managed client config defines — the name
#: a bare ``msks`` addresses through ``active_daemon``.
MANAGED_ALIAS = "msksd-vm"
#: The managed config's first line: the marker a rerun reads to
#: know the file is this tool's to refresh.
MANAGED_HEADER = "# Written by msks-setup-macos-host"

#: The login agent (launchd): ``RunAtLoad`` runs ``limactl start``
#: at login, so the VM — and with it the daemon — answers after a
#: reboot with no manual step. The plist lives in the user's own
#: LaunchAgents: a user agent, no root, matching the VM it owns.
AGENT_LABEL = "com.msks.msksd-vm"
AGENT_PLIST = Path(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist").expanduser()
AGENT_LOG = Path("~/Library/Logs/msksd-vm-agent.log").expanduser()


class MacSetupError(Exception):
    """A named refusal: the message prints with the tool's name."""


def say(message: str) -> None:
    """One progress line on stderr, the hostsetup tools' shape."""
    print(f"{TOOL}: {message}", file=sys.stderr)


def run(command: list[str], *, capture: bool = False) -> str:
    """One subprocess; a nonzero exit is a named refusal carrying
    the command's tail. A binary the host does not carry raises
    FileNotFoundError unchanged — the caller decides what it
    means: lima_version_output reads it as the stock-Mac answer,
    delete_vm maps it to the install remedy."""
    done = subprocess.run(
        command,
        capture_output=capture,
        text=True,
    )
    if done.returncode != 0:
        tail = ""
        if capture and done.stderr:
            tail = ": " + done.stderr.strip().splitlines()[-1]
        raise MacSetupError(f"`{' '.join(command[:3])}...` failed{tail}")
    return done.stdout if capture else ""


def vm_shell(script: str) -> str:
    """Run one bash snippet inside the VM as root, stdin to
    stdout — the one in-VM seam every phase drives. A nonzero exit
    is a named refusal carrying the stderr tail."""
    done = subprocess.run(
        ["limactl", "shell", VM_NAME, "--", "sudo", "bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1] if done.stderr else ""
        raise MacSetupError(
            f"in-VM command failed{': ' + tail if tail else ''}"
        )
    return done.stdout


# --- phase 1: the host check -----------------------------------------


def check_host(install: bool = True) -> dict:
    """The Mac itself: Apple Silicon (the images are aarch64,
    nested virtualization is the Apple Virtualization
    framework's) and a Lima at the template's floor — installed
    through brew when missing (#699), unless the caller passed
    install=False (the check pass, which mutates nothing). A Mac
    without brew names the Homebrew line; an old Lima names the
    upgrade."""
    if sys.platform != "darwin":
        raise MacSetupError(
            "this tool runs on macOS (the Linux hosts take "
            "msks-setup-debian-host)"
        )
    if os.uname().machine != "arm64":
        raise MacSetupError(
            "this tool needs Apple Silicon (the guest images are aarch64; "
            "nested virtualization is the Apple Virtualization framework's)"
        )
    check_chip_class()
    return {"lima": require_lima(install)}


def check_chip_class() -> None:
    """The chip can nest: Apple's Virtualization framework grew
    nested virtualization on the M3 class — an M1 or M2 boots the
    daemon VM but its workspaces can never start (#676's gate:
    warn here, never provision a VM whose workspaces cannot
    run). Newer, unrecognized chips pass (the floor is what the
    framework refuses, not what this list knows)."""
    brand = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if brand.startswith(("Apple M1", "Apple M2")):
        raise MacSetupError(
            f"this Mac's chip ({brand}) does not provide nested "
            "virtualization; the daemon VM would boot but its "
            "workspaces cannot start"
        )


def require_lima(install: bool = True) -> str:
    """The installed Lima's version word, at the template's floor
    or newer. A Lima the Mac does not carry takes
    :func:`lima_after_install`'s path (#699) — brew, unless the
    caller passed install=False (the check pass, which mutates
    nothing). A limactl that answers nonzero refuses by name
    through run() (a broken install is not healed by another);
    a Lima below the floor names the upgrade remedy."""
    word = lima_version_output()
    if not word:
        word = lima_after_install(install)
    if lima_version_tuple(word) < LIMA_FLOOR:
        floor = ".".join(str(p) for p in LIMA_FLOOR)
        raise MacSetupError(
            f"Lima {word} is older than the template's floor {floor} "
            "(the mount expansions render wrong before it); upgrade: "
            "brew upgrade lima"
        )
    return word


def lima_after_install(install: bool) -> str:
    """The word after the missing-Lima step. The check pass
    (install=False) refuses with the run-the-tool remedy — it
    mutates nothing; the setup pass brew-installs Lima and
    re-reads, and a still-empty word (brew's bin dir joined PATH
    only in fresh shells) is the fresh-shell refusal."""
    if not install:
        raise MacSetupError(
            "limactl is not on PATH; run the tool (it installs "
            "Lima itself through brew), then --check again"
        )
    install_lima()
    word = lima_version_output()
    if not word:
        raise MacSetupError(
            "brew install lima finished but limactl is still not on "
            "this shell's PATH; open a fresh shell (brew's bin dir "
            f"joins PATH there) and re-run {TOOL}"
        )
    return word


def install_lima() -> None:
    """Install Lima through brew (#699): the stock-Mac step this
    tool runs itself. A Mac without brew names the Homebrew line
    and stops — this tool installs through brew, never around
    it."""
    if shutil.which("brew") is None:
        raise MacSetupError(
            "brew is not on PATH; install Homebrew first "
            "(https://brew.sh), then re-run this tool — it installs "
            "Lima itself from there"
        )
    say("installing Lima through brew (brew install lima)")
    run(["brew", "install", "lima"])


def lima_version_output() -> str:
    """The version word from ``limactl --version``, or the empty
    word when limactl is nowhere on PATH (the caller installs or
    refuses by name — a stock Mac's answer). A limactl that is
    present but answers nonzero refuses by name through run(),
    carrying the command's tail."""
    try:
        out = run(["limactl", "--version"], capture=True)
    except FileNotFoundError:
        return ""
    return out.strip().split()[-1] if out.strip() else ""


def lima_version_tuple(word: str) -> tuple:
    """The version word's numeric parts (``2.2.1`` → (2, 2, 1));
    non-numeric fragments drop, an empty word compares below
    every floor."""
    return tuple(int(p) for p in word.lstrip("v").split(".") if p.isdigit())


# --- phase 2: the msksd VM -------------------------------------------


def instance_yaml(host_port: int) -> str:
    """The instance config: the shipped template with the daemon's
    port forward appended — the one template piece this tool owns
    (a deployment choice, not image geometry). The forward binds
    both loopbacks: the daemon serves the guest's 127.0.0.1 and
    the client dials the Mac's."""
    template = (
        files("msks.server").joinpath("assets/msksd-vm.yaml").read_text()
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


def create_vm(name: str, port: int) -> int:
    """The first-sight creation: the rendered instance config
    (template plus this tool's forward) starts the VM through
    limactl from a temp path — the image downloads on first run
    — and the creation port returns for the phases after."""
    say(f"creating the {name} VM (the image downloads on first run)")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as handle:
        handle.write(instance_yaml(port))
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


def ensure_vm(name: str = VM_NAME, host_port: int | None = None) -> int:
    """The VM exists and runs, and the run's host port returns:
    created from the rendered instance config on first sight (the
    explicit port when one was named, the default otherwise),
    started when stopped, left alone when running. The port for
    an existing VM is :func:`adopted_port`'s policy: an explicit
    ``--port`` that disagrees with the forward (fixed at creation)
    refuses naming the recreation remedy; with no ``--port``, the
    run adopts the forward the VM carries — the deployment
    already chose it at creation (#699)."""
    entry = vm_entry(name)
    if entry is None:
        return create_vm(name, resolved_port(host_port, None))
    if entry.get("status") != "Running":
        say(f"starting the stopped {name} VM")
        run(["limactl", "start", name])
    return adopted_port(name, host_port)


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


def vm_version() -> str:
    """The wheel release the bootstrap installs: the PyPI release
    matching this tool's own version — the Mac's client and the
    VM's daemon ship from one build."""
    return __version__


def bootstrap_script(wheel: str | None, home: str) -> str:
    """The in-VM bootstrap: a uv-managed Python 3.14 venv (the
    wheel targets the project's floor; Debian trixie ships 3.13,
    so uv provisions the standalone interpreter the repo itself
    pins), the wheel (a release from PyPI, or the file this tool
    copied in for a build in hand), the config (the forwarded
    port, host-share mode prefix — the reserved-prefix geometry
    the template mounted — and the invoking user's home the
    default allowed root), the unit, and the start. Idempotent:
    the token and the state survive a rerun; the venv reinstall
    is a no-op at the same version. Coherence guards between the
    steps (#706): the installed daemon's version must equal the
    tool's, and it must parse the config this same script writes
    — the #704 crash loop refuses here, with the version pair
    named, ahead of the unit restart."""
    install = f"/tmp/{Path(wheel).name}" if wheel else f"msks=={vm_version()}"
    return f"""set -eux
apt-get update -qq
apt-get install -y -qq {VM_PACKAGES} >/dev/null
# The egress stack's kernel setting (#101): the daemon verifies
# it at startup and refuses an egress workspace on 0.
echo net.ipv4.ip_forward=1 >/etc/sysctl.d/99-msksd-egress.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null
# The VMM: pinned, verified, and left alone once present.
ch_url={CH_URL}
ch_sha={CH_SHA256}
if ! [ -x /usr/local/bin/cloud-hypervisor ]; then
  curl -sL -o /usr/local/bin/cloud-hypervisor "$ch_url"
  echo "$ch_sha  /usr/local/bin/cloud-hypervisor" | sha256sum -c - >/dev/null
  chmod +x /usr/local/bin/cloud-hypervisor
fi
python3 -m venv /opt/uv-bootstrap
/opt/uv-bootstrap/bin/pip install -q uv=={UV_PIN}
/opt/uv-bootstrap/bin/uv venv -q --python 3.14 {VM_PREFIX}
uv_bin=/opt/uv-bootstrap/bin/uv
$uv_bin pip install -q --python {VM_PREFIX}/bin/python {install}
# The coherence guard, version half (#706, #704): everything
# after this line assumes the installed daemon is this tool's
# own build. A dev tree whose pyproject.toml still names an
# already-released number installs that release from PyPI under
# the same word — the version pair here catches a resolution
# that landed anything else; the same-numbered stale release is
# the config half's catch below.
ver_probe="import importlib.metadata as m; print(m.version('msks'))"
installed=$({VM_PREFIX}/bin/python -c "$ver_probe")
if [ "$installed" != "{vm_version()}" ]; then
  # set +x first (#712): under the script's xtrace the exit's own
  # trace would land as stderr's last line, and vm_shell's refusal
  # tail would carry "+ exit 1" instead of the message above it.
  set +x
  echo "the bootstrap installed msks $installed, not this" \
    "tool's {vm_version()} — pass --wheel with a build of this tree" >&2
  exit 1
fi
mkdir -p {VM_STATE}
token_file={VM_STATE}/bootstrap-token
if [ ! -s "$token_file" ]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" >"$token_file"
  chmod 600 "$token_file"
fi
token=$(cat "$token_file")
cat >{VM_CONF} <<CONF
# Written by {TOOL} (#676); re-run the tool to refresh this file.
# The bootstrap token and the state dir survive reruns. The file
# is flat — every key top-level, the config loader's shape.
host: 127.0.0.1
port: {VM_PORT}
socket: ""
bootstrap_token: "$token"
state_dir: {VM_STATE}
# Workspaces are networked by default; the daemon's egress stack
# arms at startup when the setting says so.
egress_enabled: true
# The reserved-prefix geometry the VM's template mounted (#671):
# share roots live under /mnt/msks, host paths translate by
# composition, and the invoking user's home is the default
# allowed root (#672's policy gate).
mount_host_mode: prefix
mount_host_roots: ["{home}"]
CONF
# The coherence guard, config half (#706, #704): the config
# this tool writes must parse under the daemon this bootstrap
# installed — the #704 shape (a same-numbered stale release that
# rejects keys newer than itself) refused here, with the version
# pair named, ahead of the unit restart and its crash loop.
parse_probe="from msks.config import load_settings; "
parse_probe+="load_settings('{VM_CONF}')"
if ! {VM_PREFIX}/bin/python -c "$parse_probe" 2>/tmp/msksd-config.err; then
  # set +x first (#712): same as the version half — the message,
  # not the exit's trace, is stderr's last line.
  set +x
  echo "the daemon this bootstrap installed ($installed) rejects" \
    " the config this tool ({vm_version()}) writes (a stale" \
    " release can carry the same number): pass --wheel with a" \
    " build of this tree; $(tail -1 /tmp/msksd-config.err)" >&2
  exit 1
fi
cat >{VM_UNIT} <<'UNIT'
# Written by {TOOL} (#676); re-run the tool to refresh this file.
[Unit]
Description=msksd (the msks daemon VM's service)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart={VM_PREFIX}/bin/msksd serve --config {VM_CONF}
Restart=on-failure
StateDirectory=msksd
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable msksd.service
# A rewrite of the config must reach the running daemon: enable
# alone leaves a live service on the old file (the CA and the
# state then land where the old config said).
systemctl restart msksd.service
"""


def bootstrap_vm(wheel: str | None, home: str) -> None:
    """Copy the wheel in when one is named, then run the
    bootstrap script inside the VM."""
    if wheel is not None:
        say(f"copying {wheel} into the VM")
        run(["limactl", "copy", os.path.abspath(wheel), f"{VM_NAME}:/tmp/"])
    vm_shell(bootstrap_script(wheel, home))


# --- phase 4: the verify pass and the preset --------------------------


def verify_daemon(port: int, timeout_s: float = 90.0) -> None:
    """The daemon answers through the forward: a TLS connect to
    the listener is enough (any HTTP answer — a 401 included —
    proves the forward, the listener, and the daemon's TLS at
    once). Bounded: a first boot's pip install takes a while."""
    # The probe does not verify: the daemon's CA is self-signed,
    # and the preset step (which fetches the CA) is what arms the
    # client's verification — any TLS answer here proves the
    # stack, whatever the chain says.
    context = ssl._create_unverified_context()
    deadline = time.monotonic() + timeout_s
    last = "no attempt was made"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"https://127.0.0.1:{port}/", timeout=5, context=context
            )
            return
        except Exception as exc:  # any answer proves the stack
            if getattr(exc, "code", None) is not None:
                return
            last = str(exc)
        time.sleep(2.0)
    raise MacSetupError(
        f"the daemon never answered https://127.0.0.1:{port}/ within "
        f"{timeout_s:.0f}s ({last}); either the VM's journal names the "
        f"cause (limactl shell {VM_NAME} -- sudo journalctl -u msksd -e) "
        "or another listener answers this host port (pass --port)"
    )


def verify_chain(port: int, ca_pem: str) -> None:
    """The listener that answers is THIS daemon: the served
    certificate chains to the VM's CA. A mismatch names the
    collision — something else on the Mac already answers this
    port (another VM's forward, a local daemon), and the remedy
    is another ``--port``. A CA that will not parse (a torn read
    inside the daemon's write window) refuses with the re-run
    remedy, never a raw traceback."""
    try:
        context = ssl.create_default_context(cadata=ca_pem)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1"):
                return
    except ssl.SSLCertVerificationError as exc:
        # The chain does not hold: the listener answering this
        # port serves another daemon's certificate.
        raise MacSetupError(
            f"something other than the msksd VM's daemon answers "
            f"127.0.0.1:{port} ({exc}); pass --port to use another "
            "host port"
        ) from None
    except ssl.SSLError as exc:
        raise MacSetupError(
            f"the daemon's CA did not parse ({exc}); re-run the tool"
        ) from None
    except OSError as exc:
        raise MacSetupError(
            f"something other than the msksd VM's daemon answers "
            f"127.0.0.1:{port} ({exc}); pass --port to use another "
            "host port"
        ) from None


def client_preset(port: int) -> str:
    """The CA and the token out of the VM, the client's three
    files written (the CA, the token beside it, the config naming
    both), the preset block returned for printing — the exports a
    shell can paste when it prefers them to the config. The CA
    fetch retries briefly: the restart this run's bootstrap just
    issued returns before the daemon's first startup writes the
    file."""
    ca = fetch_vm_file(f"{VM_STATE}/{CA_CERT}").strip()
    if not ca.startswith("-----BEGIN CERTIFICATE-----"):
        raise MacSetupError(
            "the daemon's CA did not come back from the VM (the "
            "daemon may still be generating it; re-run)"
        )
    MAC_CA.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(MAC_CA, ca + "\n")
    verify_chain(port, ca)
    token = fetch_vm_file(f"{VM_STATE}/bootstrap-token").strip()
    write_private(MAC_TOKEN, token + "\n")
    status = ensure_client_config(port)
    say(status)
    exports = (
        f"export MSKSC_URL=https://127.0.0.1:{port}\n"
        f"export MSKSC_TOKEN={token}\n"
        f"export MSKSC_CAFILE={MAC_CA}"
    )
    if status.startswith("kept"):
        # The file this tool never wrote: the paste-able half rides
        # beside the exports — the entry block for the config (no
        # top-level key, so a verbatim paste cannot duplicate one
        # and corrupt the file), where the exports are the shell's
        # fallback.
        return (
            exports
            + "\n\n# add this entry under the daemons: mapping in "
            + str(Path(default_config_path()))
            + "\n# (write the daemons: line too if the file carries "
            "none), then\n# set active_daemon: "
            + MANAGED_ALIAS
            + " there:\n"
            + alias_entry(port)
        )
    return exports


# --- phase 5: the client config and the login agent -------------------


def write_private(path: Path, body: str) -> None:
    """Write a private file the house way: the parent 0700 when
    created, the file 0600 — the token is a credential, and the
    CA and the config join it for uniformity. A step the
    filesystem refuses (the mkdir, the open, the write) is a
    named refusal, never a raw traceback."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        raise MacSetupError(
            f"could not write {path} ({exc}); fix the path or its "
            "permissions, then re-run"
        ) from None


def client_config_body(port: int) -> str:
    """The managed client config: the daemon alias a bare ``msks``
    addresses — the forwarded port, the token file, the CA — with
    ``active_daemon`` naming it, so every CLI invocation reaches
    the daemon with no exports in the shell profile."""
    return (
        f"{MANAGED_HEADER} (#689); re-run the tool\n"
        "# to refresh this file — a rerun rewrites what this tool\n"
        "# wrote, edits included.\n" + alias_stanza(port)
    )


def alias_stanza(port: int) -> str:
    """The daemon alias block alone: the managed config's body —
    for the file this tool writes."""
    return (
        "daemons:\n" + alias_entry(port) + f"active_daemon: {MANAGED_ALIAS}\n"
    )


def alias_entry(port: int) -> str:
    """The alias's own entry block, indented to sit under a
    ``daemons:`` mapping — the paste-able piece for an
    operator's own ``msks.yaml``. It carries no top-level key
    (the client's loader refuses duplicates, so a verbatim paste
    can never corrupt the file) and no managed header (which
    would hand this tool ownership of it on the next rerun)."""
    return (
        f"  {MANAGED_ALIAS}:\n"
        f"    url: https://127.0.0.1:{port}\n"
        f"    token_file: {MAC_TOKEN}\n"
        f"    cafile: {MAC_CA}\n"
    )


def ensure_client_config(port: int) -> str:
    """Write or refresh the managed client config, idempotently.

    A missing file is written; a file the read cannot reach is a
    named refusal (never the missing arm's overwrite); an
    existing file takes :func:`refresh_managed_config`'s policy,
    whose status this returns for the caller to say."""
    path = Path(default_config_path())
    body = client_config_body(port)
    try:
        text = path.read_text()
    except FileNotFoundError:
        write_private(path, body)
        return f"wrote the client config ({path})"
    except OSError as exc:
        raise MacSetupError(
            f"the client config ({path}) cannot be read ({exc}); fix "
            "the file or remove it, then re-run"
        ) from None
    return refresh_managed_config(text, body, path)


def refresh_managed_config(text: str, body: str, path: Path) -> str:
    """The four-way policy over an existing config file's text:
    the current body is a no-op; the client's own first-run
    template (all comments, nothing set) is replaced without
    ceremony — the client regenerates it on demand any time; a
    file this tool wrote (its header says so) is refreshed —
    re-running rewrites what the tool wrote, operator edits to it
    included; a file it never wrote is kept untouched, and the
    status names that choice — the preset this run prints carries
    the alias block to paste into such a file."""
    if text == body:
        return f"the client config is current ({path})"
    if text == render_template():
        write_private(path, body)
        return f"wrote the client config over the empty template ({path})"
    if text.startswith(MANAGED_HEADER):
        write_private(path, body)
        return f"refreshed the client config ({path})"
    return (
        f"kept the config at {path} (this tool never wrote it) — add "
        f"the {MANAGED_ALIAS} daemon entry to it by hand, or remove "
        "the file and re-run"
    )


def client_config_row(port: int) -> str:
    """The check row: a bare ``msks`` reaches THIS daemon — the
    active alias names this forward with the VM's token and CA.
    Anything else (no file, a file that will not parse, an active
    daemon pointing elsewhere) is a named refusal with the
    re-run remedy."""
    path = Path(default_config_path())
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise MacSetupError(
            f"the client config ({path}) does not exist; re-run {TOOL} "
            "or write the daemon alias into it by hand"
        ) from None
    except OSError as exc:
        raise MacSetupError(
            f"the client config ({path}) cannot be read ({exc}); fix "
            "the file or remove it, then re-run"
        ) from None
    entry = active_daemon_entry(text, path)
    if not entry_points_at(entry, port):
        raise MacSetupError(
            f"the client config's active daemon does not point a bare "
            f"msks at this daemon (https://127.0.0.1:{port} with the "
            f"VM's token and CA); re-run {TOOL} or edit {path} by hand"
        )
    return f"a bare msks reaches the daemon ({path})"


def active_daemon_entry(text: str, path: Path) -> dict:
    """The active alias's settings from the config file's text,
    parsed by the client's own loader — a file that will not load
    is a named refusal, and an active name no alias defines
    answers an empty entry (the pointing check refuses it)."""
    try:
        doc = parse_config_doc(text, str(path))
    except ValueError as exc:
        raise MacSetupError(
            f"the client config does not load ({exc})"
        ) from None
    return doc.get("daemons", {}).get(doc.get("active_daemon"), {})


def entry_points_at(entry: dict, port: int) -> bool:
    """Whether an alias's settings name this forward with the VM's
    token and CA — the three facts a bare ``msks`` dials."""
    return (
        entry.get("url") == f"https://127.0.0.1:{port}"
        and entry.get("token_file") == str(MAC_TOKEN)
        and entry.get("cafile") == str(MAC_CA)
    )


def limactl_path() -> str:
    """The absolute limactl path the agent names — launchd's own
    environment carries none of the brew prefixes, so the agent
    cannot look the binary up by name at run time."""
    found = shutil.which("limactl")
    if found is None:
        raise MacSetupError(
            "limactl is not on PATH; re-run the tool (it installs "
            "Lima itself through brew) or run brew install lima"
        )
    return found


def agent_plist(limactl: str) -> bytes:
    """The agent's plist: ``RunAtLoad`` brings the VM up at login
    (``limactl start`` on an already-running VM is a quick no-op,
    so a login into a live session costs one command). Both
    streams land in one log under ~/Library/Logs — the file a
    silent login failure reads."""
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": [limactl, "start", VM_NAME],
            "RunAtLoad": True,
            "StandardOutPath": str(AGENT_LOG),
            "StandardErrorPath": str(AGENT_LOG),
        }
    )


def unload_agent() -> None:
    """Boot the agent out of launchd, tolerant of nothing being
    loaded — the state after a first install and after an
    uninstall alike."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
        capture_output=True,
    )


def agent_loaded() -> bool:
    """Whether launchd carries the agent: ``launchctl print`` on
    the label answers 0 only for a loaded service."""
    done = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
        capture_output=True,
    )
    return done.returncode == 0


def ensure_agent() -> None:
    """Install and load the login agent, idempotently: the plist
    is rewritten every run (the agent is updated, never
    duplicated — one label, one file), the old service boots out,
    and the new one bootstraps in loaded. The kickstart that
    follows runs the agent's job immediately — the same
    ``limactl start`` every login runs — so the VM starts through
    the agent now, and a green install has already proven the
    login path instead of discovering a broken one at the next
    login."""
    limactl = limactl_path()
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(agent_plist(limactl))
    unload_agent()
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST)])
    run(
        [
            "launchctl",
            "kickstart",
            f"gui/{os.getuid()}/{AGENT_LABEL}",
        ]
    )
    say(f"the login agent starts the {VM_NAME} VM now and at every login")


def remove_agent() -> None:
    """The uninstall mirror: boot the agent out and remove its
    plist — idempotent, an absent agent uninstalls cleanly."""
    unload_agent()
    AGENT_PLIST.unlink(missing_ok=True)


def nested_marker() -> str:
    """The nested-virtualization marker inside the VM, in #664's
    doctor-row shape (doctor.py's kvm_nv_mode): the kvm mode
    parameter names ``nested``, the kernel command line sets
    ``kvm-arm.mode=nested``, or KVM's init line in the kernel log
    names NV engaged (the kernel's own spellings: ``VHE+NV`` on
    the Asahi tree, ``nVHE`` and the ``nv:`` handler counts on
    Debian's) — the running mode's own record. Empty when
    none answers (the kvm module loads on demand; a freshly
    booted VM may carry none of the three yet)."""
    probe = (
        "cat /sys/module/kvm/parameters/mode 2>/dev/null; "
        "grep -o 'kvm-arm.mode=nested' /proc/cmdline 2>/dev/null; "
        "dmesg | grep -m1 -E 'VHE\\+NV|nVHE|nv: [0-9]+ coarse' "
        "2>/dev/null || true"
    )
    for line in vm_shell(probe).splitlines():
        if line.strip():
            return line.strip()
    return ""


def virtiofsd_state() -> str:
    """Whether virtiofsd is installed inside the VM — the shares
    feature's daemon, installed on demand by a re-run."""
    return vm_shell(
        "dpkg -s virtiofsd >/dev/null 2>&1 && echo present || echo missing"
    ).strip()


def fetch_vm_file(path: str, timeout_s: float = 20.0) -> str:
    """One file out of the VM, retried while the daemon's first
    startup may not have written it yet (the restart returns
    before the CA lands)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = vm_shell(f"cat {path} 2>/dev/null || true")
        if last.strip():
            return last
        time.sleep(2.0)
    return last


def daemon_version(port: int) -> str:
    """The VM's daemon version word, from the public ``/health``
    over the forward (#706) — the same unverified TLS connect
    the listener row already proved chains to this daemon's CA,
    so the probe answers or the row refuses naming why. The
    word rides the skew row beside the tool's own version."""
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/api/v1/health",
            timeout=5,
            context=context,
        ) as answer:
            doc = json.loads(answer.read())
    except Exception as exc:
        raise MacSetupError(
            f"the daemon's /health did not answer through the "
            f"forward ({exc}); re-run {TOOL} to refresh the VM's "
            "daemon"
        ) from None
    word = doc.get("version") if isinstance(doc, dict) else None
    if not isinstance(word, str) or not word:
        raise MacSetupError(
            "the daemon's /health answer carried no version word"
        )
    return word


def skew_row(port: int) -> str:
    """The version-pair row (#706): green naming both words when
    the VM's daemon runs this tool's own version; a drift is the
    named rerun refusal — the pair ships from one wheel, and a
    skew otherwise surfaces one feature at a time."""
    theirs = daemon_version(port)
    if theirs != vm_version():
        raise MacSetupError(
            f"the VM's daemon runs {theirs}, this tool {vm_version()}; "
            f"re-run {TOOL} to update the VM's daemon"
        )
    return f"the VM's daemon and this tool both run {vm_version()}"


def check_rows(port: int, install: bool = True) -> list[str]:
    """The macOS doctor rows: Lima at the floor, the VM running,
    the nested-virtualization marker inside it, virtiofsd
    installed there, and the daemon's listener answering through
    the forward — chained to THIS daemon's CA, so a port
    collision (another VM's forward, a local daemon) refuses
    instead of reporting another daemon green — and the VM's
    daemon at this tool's own version (the skew row, #706: the
    pair ships from one wheel, and the row stays red until a
    rerun lands the tool's version in the VM).
    virtiofsd's
    absence is a refusal, not a degraded pass: shares are this
    VM's headline feature. The Lima row installs through brew on
    the mutating default; the check pass passes install=False —
    it refuses on a missing Lima instead of installing it."""
    lima = require_lima(install)
    rows = [f"lima {lima} at the template's floor"]
    entry = vm_entry(VM_NAME)
    if entry is None:
        raise MacSetupError(f"the {VM_NAME} VM does not exist; run {TOOL}")
    if entry.get("status") != "Running":
        raise MacSetupError(
            f"the {VM_NAME} VM is not running; limactl start {VM_NAME}"
        )
    rows.append(f"the {VM_NAME} VM is running")
    nested = nested_marker()
    if nested:
        rows.append(f"nested virtualization is on ({nested})")
    else:
        rows.append(
            "nested virtualization marker not found (the kvm module "
            "loads on demand; start a workspace and re-check)"
        )
    if virtiofsd_state() != "present":
        raise MacSetupError(
            "virtiofsd is not installed inside the VM; re-run the "
            "tool to install it (shares spawn it per attach)"
        )
    rows.append("virtiofsd present inside the VM")
    verify_daemon(port, timeout_s=15.0)
    ca = fetch_vm_file(f"{VM_STATE}/{CA_CERT}").strip()
    verify_chain(port, ca)
    rows.append(
        f"the daemon answers https://127.0.0.1:{port}/ through the "
        "forward, chained to the VM's CA"
    )
    rows.append(skew_row(port))
    rows.extend(client_side_rows(port))
    return rows


def client_side_rows(port: int) -> list[str]:
    """The client-side rows: the config a bare ``msks`` reads and
    the login agent that brings the VM up after a reboot — the
    two #689 rows, one refusal each with the re-run remedy."""
    rows = [client_config_row(port)]
    if not agent_loaded():
        raise MacSetupError(
            f"the login agent ({AGENT_LABEL}) is not loaded; re-run {TOOL}"
        )
    rows.append(
        f"the login agent brings the {VM_NAME} VM up at login ({AGENT_LABEL})"
    )
    return rows


# --- the entry --------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The surface: the wheel override for a build in hand, the
    check-only pass, and the uninstall mirror."""
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
        "--check",
        action="store_true",
        help="verify only: the doctor rows, nothing mutated",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="stop and delete the msksd VM and its login agent "
        "(Lima and the client config stay, and the summary "
        "names them)",
    )
    return parser.parse_args(argv)


def run_setup(args: argparse.Namespace) -> str:
    """The phases in order; the preset block returns for the
    caller to print. The verify pass runs before the client
    files and the login agent so a green run is what they
    provision — the preset's token and CA are real, and the agent
    earns its login slot. The port the phases share is the one
    ``ensure_vm`` resolves: the explicit ``--port``, or the
    forward the existing VM already carries (#699)."""
    check_host()
    home = str(Path.home())
    port = ensure_vm(host_port=args.port)
    bootstrap_vm(args.wheel, home)
    verify_daemon(port)
    preset = client_preset(port)
    ensure_agent()
    return preset


def main(argv: list[str] | None = None) -> int:
    """The console entry: 0 on success, 1 on a named refusal; the
    preset and the check rows print on stdout, progress and
    refusals on stderr."""
    args = parse_args(argv)
    try:
        if args.uninstall:
            say(f"removing the login agent and the {VM_NAME} VM")
            remove_agent()
            delete_vm(VM_NAME)
            print(
                f"{TOOL}: the {VM_NAME} VM and its login agent are gone. "
                "Lima itself, the client config, and the CA and token "
                "under ~/.config/msks/ stay; remove them by hand when "
                "you want them gone."
            )
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
