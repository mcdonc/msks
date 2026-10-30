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
   A missing Lima names the brew line that installs it.
2. **The msksd VM** — created from the template that ships in the
   wheel (``server/assets/msksd-vm.yaml``, #671: vz, nested
   virtualization, the invoking user's home mounted under the
   reserved ``/mnt/msks`` prefix) with the daemon's port forward
   appended — the one piece the template leaves to this tool,
   because the port is a deployment choice.
3. **The in-VM bootstrap** — the wheel installed into /opt/msks
   (the PyPI release matching this tool's version, or ``--wheel``
   for a build in hand), ``/etc/msksd.yaml`` written (the daemon
   on the forwarded port, host-share mode ``prefix`` — the
   reserved-prefix geometry the template mounted, the invoking
   user's home the default allowed root), and a systemd unit that
   starts msksd at boot.
4. **The verify pass** — the daemon's TLS listener answers
   through the forward; the CA and the bootstrap token come back
   out, and the client preset prints: the three exports a Mac
   shell needs to reach this daemon (``MSKSC_URL``,
   ``MSKSC_TOKEN``, ``MSKSC_CAFILE`` — the CA written under
   ``~/.config/msks/``).

``--check`` runs the verify pass alone — the macOS doctor row
set: Lima, the VM, the nested-virtualization marker, virtiofsd
inside the VM, the listener. ``--uninstall`` stops and deletes
the VM (Lima and the installed wheel stay, and the summary names
them). Every phase is idempotent: re-run any time; a rerun
preserves the token, the CA, and the daemon's state.
"""

import argparse
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib.resources import files
from pathlib import Path

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
#: verify pass name the collision and the operator picks another.
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


class MacSetupError(Exception):
    """A named refusal: the message prints with the tool's name."""


def say(message: str) -> None:
    """One progress line on stderr, the hostsetup tools' shape."""
    print(f"{TOOL}: {message}", file=sys.stderr)


def run(command: list[str], *, capture: bool = False) -> str:
    """One subprocess; a nonzero exit is a named refusal carrying
    the command's tail."""
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


def check_host() -> dict:
    """The Mac itself: Apple Silicon (the images are aarch64,
    nested virtualization is the Apple Virtualization
    framework's) and a Lima at the template's floor. A missing
    Lima names the brew line; an old Lima names the upgrade."""
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
    return {"lima": require_lima()}


def require_lima() -> str:
    """The installed Lima's version word, refused below the
    template's floor with the upgrade remedy."""
    word = lima_version_output()
    if lima_version_tuple(word) < LIMA_FLOOR:
        floor = ".".join(str(p) for p in LIMA_FLOOR)
        raise MacSetupError(
            f"Lima {word} is older than the template's floor {floor} "
            "(the mount expansions render wrong before it); upgrade: "
            "brew upgrade lima"
        )
    return word


def lima_version_output() -> str:
    """The version word from ``limactl --version``, or the named
    refusal with the brew line when limactl is nowhere on
    PATH."""
    try:
        out = run(["limactl", "--version"], capture=True)
    except MacSetupError:
        raise MacSetupError(
            "limactl is not on PATH; install Lima first: brew install lima"
        ) from None
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


def ensure_vm(name: str = VM_NAME, host_port: int = PORT) -> None:
    """The VM exists and runs: created from the rendered instance
    config on first sight, started when stopped, left alone when
    running. vz mounts are fixed at creation — a recreation is
    the remedy for template changes, and the shares doc names it."""
    listing = run(["limactl", "list", "--json"], capture=True)
    if f'"name":"{name}"' not in listing.replace(" ", ""):
        say(f"creating the {name} VM (the image downloads on first run)")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False
        ) as handle:
            handle.write(instance_yaml(host_port))
            path = handle.name
        try:
            run(["limactl", "start", "--name", name, path])
        finally:
            os.unlink(path)
        return
    if '"status":"Running"' not in listing.replace(" ", ""):
        say(f"starting the stopped {name} VM")
        run(["limactl", "start", name])


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
    is a no-op at the same version."""
    install = f"/tmp/{Path(wheel).name}" if wheel else f"msks=={vm_version()}"
    return f"""set -eux
apt-get update -qq
apt-get install -y -qq python3-venv virtiofsd >/dev/null
python3 -m venv /opt/uv-bootstrap
/opt/uv-bootstrap/bin/pip install -q uv=={UV_PIN}
/opt/uv-bootstrap/bin/uv venv -q --python 3.14 {VM_PREFIX}
uv_bin=/opt/uv-bootstrap/bin/uv
$uv_bin pip install -q --python {VM_PREFIX}/bin/python {install}
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
# The reserved-prefix geometry the VM's template mounted (#671):
# share roots live under /mnt/msks, host paths translate by
# composition, and the invoking user's home is the default
# allowed root (#672's policy gate).
mount_host_mode: prefix
mount_host_roots: ["{home}"]
CONF
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
        f"{timeout_s:.0f}s ({last}); the VM's journal names the cause: "
        f"limactl shell {VM_NAME} -- sudo journalctl -u msksd -e"
    )


def verify_chain(port: int, ca_pem: str) -> None:
    """The listener that answers is THIS daemon: the served
    certificate chains to the VM's CA. A mismatch names the
    collision — something else on the Mac already answers this
    port (another VM's forward, a local daemon), and the remedy
    is another ``--port``."""
    context = ssl.create_default_context(cadata=ca_pem)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1"):
                return
    except (OSError, ssl.SSLError) as exc:
        raise MacSetupError(
            f"something other than the msksd VM's daemon answers "
            f"127.0.0.1:{port} ({exc}); pass --port to use another "
            "host port"
        ) from None


def client_preset(port: int) -> str:
    """The CA and the token out of the VM, the CA written beside
    the client config, the preset block returned for printing.
    The CA fetch retries briefly: the restart this run's
    bootstrap just issued returns before the daemon's first
    startup writes the file."""
    ca = fetch_vm_file(f"{VM_STATE}/{CA_CERT}").strip()
    if not ca.startswith("-----BEGIN CERTIFICATE-----"):
        raise MacSetupError(
            "the daemon's CA did not come back from the VM (the "
            "daemon may still be generating it; re-run)"
        )
    MAC_CA.parent.mkdir(parents=True, exist_ok=True)
    MAC_CA.write_text(ca + "\n")
    verify_chain(port, ca)
    token = fetch_vm_file(f"{VM_STATE}/bootstrap-token").strip()
    return (
        f"export MSKSC_URL=https://127.0.0.1:{port}\n"
        f"export MSKSC_TOKEN={token}\n"
        f"export MSKSC_CAFILE={MAC_CA}"
    )


def nested_marker() -> str:
    """The nested-virtualization marker inside the VM: the
    arm64 kvm mode parameter (``nv``) or the kernel log's NV
    init line — the same probe shape #664's doctor row reads,
    empty when neither answers."""
    marker = vm_shell(
        "cat /sys/module/kvm/parameters/kvm-arm.mode 2>/dev/null "
        "|| dmesg | grep -m1 -i 'kvm.*nested' || true"
    ).strip()
    return marker if marker else ""


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


def check_rows(port: int) -> list[str]:
    """The macOS doctor rows: Lima at the floor, the VM running,
    the nested-virtualization marker inside it, virtiofsd
    installed there, and the daemon's listener answering through
    the forward. Each row names its finding; the first refusal
    stops the pass."""
    lima = require_lima()
    rows = [f"lima {lima} at the template's floor"]
    listing = run(["limactl", "list", "--json"], capture=True).replace(" ", "")
    if f'"name":"{VM_NAME}"' not in listing:
        raise MacSetupError(f"the {VM_NAME} VM does not exist; run {TOOL}")
    if '"status":"Running"' not in listing:
        raise MacSetupError(
            f"the {VM_NAME} VM is not running; limactl start {VM_NAME}"
        )
    rows.append(f"the {VM_NAME} VM is running")
    nested = nested_marker()
    if nested:
        rows.append(f"nested virtualization is on ({nested})")
    else:
        rows.append(
            "nested virtualization marker not found (workspaces "
            "need it; the VM may still be probing)"
        )
    if virtiofsd_state() == "present":
        rows.append("virtiofsd present inside the VM")
    else:
        rows.append("virtiofsd missing inside the VM (a re-run installs it)")
    verify_daemon(port, timeout_s=15.0)
    rows.append(
        f"the daemon answers https://127.0.0.1:{port}/ through the forward"
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
        default=PORT,
        help=f"the daemon's port, both loopbacks (default: {PORT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only: the doctor rows, nothing mutated",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="stop and delete the msksd VM (Lima and the client "
        "config stay, and the summary names them)",
    )
    return parser.parse_args(argv)


def run_setup(args: argparse.Namespace) -> str:
    """The phases in order; the preset block returns for the
    caller to print. The verify pass runs last so the preset's
    token and CA are real."""
    check_host()
    home = str(Path.home())
    ensure_vm(host_port=args.port)
    bootstrap_vm(args.wheel, home)
    verify_daemon(args.port)
    return client_preset(args.port)


def main(argv: list[str] | None = None) -> int:
    """The console entry: 0 on success, 1 on a named refusal; the
    preset prints on stdout, everything else on stderr."""
    args = parse_args(argv)
    try:
        if args.uninstall:
            say(f"deleting the {VM_NAME} VM (Lima and the client config stay)")
            run(["limactl", "delete", "-f", VM_NAME])
            print(
                f"{TOOL}: the {VM_NAME} VM is gone. Lima itself, the "
                "client config, and the CA under ~/.config/msks stay; "
                "remove them by hand when you want them gone."
            )
            return 0
        if args.check:
            for row in check_rows(args.port):
                print(f"  ok  {row}")
            return 0
        preset = run_setup(args)
        print(preset)
        return 0
    except MacSetupError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 1
