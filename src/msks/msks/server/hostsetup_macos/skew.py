"""The skew and state rows.

The nested-virt and virtiofsd markers read out of the VM, the
daemon's own version beside the client's, and the check rows a
run's summary prints.
"""

from __future__ import annotations

import json
import ssl
import urllib.request

from ...spec.version import ordering
from ..tls import CA_CERT
from .agent import AGENT_LABEL, agent_loaded
from .base import (
    TOOL,
    VM_NAME,
    VM_STATE,
    MacSetupError,
    fetch_vm_file,
    vm_shell,
)
from .bootstrap import vm_version
from .clientconfig import client_config_row
from .hostcheck import require_lima
from .links import client_link_row
from .verify import verify_chain, verify_daemon
from .vm import vm_entry


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


def check_skew_remedy(theirs: str) -> str:
    """The remedy arm for the skew row's refusal (#715): the
    remedy follows the direction — a daemon newer than this tool
    would be downgraded by the plain rerun (the wedge the
    #704/#712 arc kept re-meeting), while an equal-base or older
    dev build is one the plain rerun upgrades; a dev-marked
    leader names the tree no release matches, and a dev-marked
    tool names the wheel path the plain rerun refuses."""
    if ".dev" in theirs and ordering(theirs) > ordering(vm_version()):
        return (
            "the VM's daemon runs a development build no release "
            "matches — update this tool from the same tree or wait "
            "for the release it becomes"
        )
    if ordering(theirs) > ordering(vm_version()):
        return "update this tool (pip install -U msks)"
    if ".dev" in vm_version():
        return f"build a wheel (uv build) and re-run {TOOL} with --wheel"
    return f"re-run {TOOL} to update the VM's daemon"


def skew_row(port: int) -> str:
    """The version-pair row (#706): green naming both words when
    the VM's daemon runs this tool's own version; a drift is the
    named refusal — the pair ships from one wheel, and a skew
    otherwise surfaces one feature at a time. The remedy follows
    the direction (#715)."""
    theirs = daemon_version(port)
    if theirs != vm_version():
        raise MacSetupError(
            f"the VM's daemon runs {theirs}, this tool {vm_version()}; "
            f"{check_skew_remedy(theirs)}"
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
    rows.append(client_link_row())
    return rows


# --- phase 6: the client links (#719, #734) ------------------------------


#: The client's console commands the link phase lands (#719,
#: #734): a bare ``msks`` for every shell, and
#: ``msks-term-popup`` beside it — the consent-terminal
#: launcher an ``open``-opened window resolves on its fresh
#: login PATH, where nothing from the spawning shell carries.
