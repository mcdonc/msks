"""``msks-setup-debian-host`` (#546): the Debian deployment-host
setup, moved from scripts/setup-debian-host.sh into the wheel —
the host-side pieces msksd needs to run first-level the way the
NixOS deployment host (nix/module.nix) runs it. The daemon itself
(the venv or package install, msksd.yaml, the state dir) stays
whatever deployment brought it; this tool owns the host only:

1. The capability wrapper, the Debian equivalent of the NixOS
   host's security.wrappers.msks-caps (#231): a root-owned copy of
   util-linux setpriv at ``/usr/local/bin/msks-caps`` carrying
   file capabilities CAP_NET_ADMIN and CAP_NET_BIND_SERVICE —
   net_admin for per-VM taps and nftables chains, net_bind_service
   for the egress stack's DHCP 67 and DNS 53 (#101). The wrapper
   keeps setpriv's own CLI, so the dev daemon's exec line runs
   unchanged (``DEV_CAPS=/usr/local/bin/msks-caps`` relocates it).
   The caps arrive ambient, so they flow to the daemon and every
   tool it execs. The grant surface is the wrapper's execute
   permission: any local user able to run it holds the two
   capabilities through it — the same grant the NixOS dev host's
   wrapper makes.
2. The routing kernel settings egress verifies or needs
   (docs/networking.md): net.ipv4.ip_forward=1 as a boot-time
   sysctl.d setting — the daemon reads the sysctl at startup and
   refuses every egress workspace with the cause named when it
   reads 0 — and the netfilter/tun modules at boot, mirroring
   nix/module.nix's boot.kernelModules plus the REJECT and NFQUEUE
   pieces the consent chain's rulesets reference.

The plumbing tools the daemon execs (ip, nft, conntrack) install
through apt when missing. /dev/kvm access arrives through the kvm
group on the daemon's user, the same supplementary-group grant the
NixOS unit makes — the summary prints the command when the group
is present. Idempotent: re-run any time. Re-run after a util-linux
upgrade — the setpriv copy under /usr/local keeps running the old
binary until refreshed.
"""

import grp
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL = "msks-setup-debian-host"

# Root's login PATH carries /usr/sbin on Debian; a sudo invocation
# can arrive with a stripped PATH, and setcap/nft/conntrack/modprobe
# live there.
DEBIAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

WRAPPER = "/usr/local/bin/msks-caps"
CAPS = "cap_net_admin,cap_net_bind_service"
SYSCTL_FILE = "/etc/sysctl.d/msks-egress.conf"
MODULES_FILE = "/etc/modules-load.d/msks.conf"
OS_RELEASE = "/etc/os-release"
IP_FORWARD_PROC = "/proc/sys/net/ipv4/ip_forward"

SYSCTL_TEXT = """\
# msksd workspace egress (docs/networking.md): route packets between
# per-VM taps and the host uplink. The daemon reads this key at
# startup — net.ipv4.ip_forward=1 — and refuses every egress
# workspace with the cause named while it reads 0.
net.ipv4.ip_forward = 1
"""

MODULES_HEADER = """\
# msksd egress prerequisites (msks-setup-debian-host): taps, the
# nftables machinery, NAT, conntrack, the deny RST, and the
# interactive consent queue.
"""

# nix/module.nix's boot.kernelModules, plus the reject and NFQUEUE
# pieces its list leaves to autoload (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`).
MODULES = (
    "tun",
    "nf_tables",
    "nft_chain_nat",
    "nft_masq",
    "nft_ct",
    "nft_reject",
    "nf_reject_ipv4",
    "nf_nat",
    "nf_conntrack",
    "nfnetlink_queue",
)

# command → package for every tool the wrapper path or the daemon's
# egress rulesets exec by bare name.
TOOL_PACKAGES = {
    "setpriv": "util-linux",
    "setcap": "libcap2-bin",
    "ip": "iproute2",
    "nft": "nftables",
    "conntrack": "conntrack",
    "modprobe": "kmod",
    # the routing phase's load step; procps ships on full Debian
    # and is absent from slim images (#546's container run found it)
    "sysctl": "procps",
}


class HostSetupError(Exception):
    """A named refusal: what is needed and why, before (or at) the
    step that cannot proceed."""


def sh(
    cmd: list[str], timeout: float = 10.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run one command, capturing its answer — doctor's run shape,
    exceptions included: a timeout or a missing binary answers
    (-1, "", reason) so every caller reports one shape of failure."""
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return -1, "", f"{cmd[0]}: {exc}"
    return done.returncode, done.stdout, done.stderr


def say(message: str) -> None:
    print(f"{TOOL}: {message}")


def am_root() -> bool:
    return os.geteuid() == 0


def os_release_id() -> str:
    """The os-release ID (``debian`` on the targeted hosts), or ""
    when the file is missing or carries no ID."""
    try:
        lines = Path(OS_RELEASE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("ID="):
            return line[3:].strip().strip('"')
    return ""


def write_file(path: str, text: str) -> None:
    """Install one root-owned config file (mode 0644). The parent
    is created when absent — a minimal Debian (a slim image, a
    container) ships without /etc/sysctl.d until procps lands."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    os.chown(target, 0, 0)
    target.chmod(0o644)


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def which(tool: str) -> str | None:
    return shutil.which(tool)


def invoked_as() -> str:
    """The entrypoint's own absolute path, for the sudo hint: an
    install root's PATH does not carry (a venv's bin, above all),
    so the bare name cannot work there — the hint names the path
    that does."""
    return str(Path(sys.argv[0]).resolve())


def check_root_and_platform() -> None:
    """The two refusals that precede every side effect."""
    if not am_root():
        raise HostSetupError(f"run me as root (sudo {invoked_as()})")
    release = os_release_id()
    if release != "debian":
        raise HostSetupError(
            f"this tool targets Debian hosts "
            f"(os-release says '{release or 'unknown'}')"
        )


def missing_packages() -> list[str]:
    """The packages whose tools are absent, in check order."""
    missing = []
    for tool, package in TOOL_PACKAGES.items():
        if which(tool) is None:
            missing.append(package)
    return missing


def apt_env() -> dict[str, str]:
    """The script's guarantee, kept: debconf never reaches for the
    terminal (a postinst under an interactive frontend can block on
    stdin, which sh does not redirect)."""
    return {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def install_packages(packages: list[str]) -> None:
    say(f"installing {' '.join(packages)}")
    rc, _out, err = sh(["apt-get", "-qq", "update"], timeout=120)
    if rc != 0:
        raise HostSetupError(f"apt-get update failed: {err}")
    rc, _out, err = sh(
        ["apt-get", "install", "-y", *packages], timeout=600, env=apt_env()
    )
    if rc != 0:
        raise HostSetupError(f"apt install failed: {err}")


def install_wrapper() -> None:
    """A fresh copy each run (the util-linux-upgrade note in the
    module docstring); setcap lands after the copy, because a copy
    does not carry capability xattrs."""
    setpriv = which("setpriv")
    if setpriv is None:
        raise HostSetupError("setpriv not found after package install")
    rc, _o, err = sh(
        [
            "install",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0755",
            setpriv,
            WRAPPER,
        ]
    )
    if rc != 0:
        raise HostSetupError(f"installing {WRAPPER} failed: {err}")
    verify_wrapper_caps()


def verify_wrapper_caps() -> None:
    """setcap, then read the caps back — a filesystem without
    capability xattrs accepts the setcap and loses it. getcap
    prints the caps in its own order (verified against a trixie
    container: cap_net_bind_service sorts before cap_net_admin),
    so the readback compares the granted set, not the string."""
    rc, _o, _e = sh(["setcap", f"{CAPS}+ep", WRAPPER])
    if rc != 0:
        raise HostSetupError(
            f"setcap on {WRAPPER} failed — the filesystem must "
            "support capability xattrs"
        )
    rc, out, _e = sh(["getcap", WRAPPER])
    granted = granted_caps(out)
    if rc != 0 or granted != set(CAPS.split(",")):
        raise HostSetupError(
            f"getcap does not read {CAPS}=ep back from {WRAPPER} "
            f"(it reported '{out.strip()}')"
        )


def granted_caps(getcap_output: str) -> set[str]:
    """The cap names an ``=ep`` token in getcap's output carries,
    as a set (empty when the output carries none)."""
    for token in getcap_output.split():
        if token.endswith("=ep"):
            return set(token[:-3].split(","))
    return set()


def wrapper_ambient_proof() -> str:
    """The functional proof: the daemon's exact exec line, with the
    ambient set inspected in the exec'd process. CapAmb must read
    nonzero (net_bind_service | net_admin) — a wrapper whose capset
    fails closes with setpriv's own error before this check."""
    rc, out, err = sh(
        [
            WRAPPER,
            "--inh-caps=+net_admin,+net_bind_service",
            "--ambient-caps=+net_admin,+net_bind_service",
            "--",
            "sh",
            "-c",
            "sed -n 's/^CapAmb:[[:space:]]*//p' /proc/self/status",
        ]
    )
    ambient = out.strip()
    if rc != 0 or ambient in ("", "0000000000000000"):
        detail = f"CapAmb reads '{ambient}'" + (
            f"; {err.strip()}" if err.strip() else ""
        )
        raise HostSetupError(
            f"wrapper exec left no ambient capabilities ({detail})"
        )
    return ambient


def land_sysctl() -> None:
    """The forwarding sysctl, boot-time and now; the daemon refuses
    egress workspaces naming this key while it reads 0."""
    write_file(SYSCTL_FILE, SYSCTL_TEXT)
    rc, _out, err = sh(["sysctl", "--load", SYSCTL_FILE])
    if read_file(IP_FORWARD_PROC).strip() != "1":
        detail = f"sysctl rc={rc}" + (
            f", {err.strip()}" if err.strip() else ""
        )
        raise HostSetupError(
            f"{IP_FORWARD_PROC} still reads 0 after loading "
            f"{SYSCTL_FILE} ({detail})"
        )


# A module's state after land_modules: it loaded, the kernel's
# module tree does not carry it, or the load itself failed.
LOADED = "loaded"
ABSENT = "absent"
LOAD_FAILED = "load-failed"

# The three the consent chain's rulesets reference (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`):
# interactive egress consent cannot work without them, and the
# daemon refuses consent-egress workspaces (#550).
CONSENT_MODULES = ("nft_reject", "nf_reject_ipv4", "nfnetlink_queue")


def land_modules() -> dict[str, str]:
    """The modules file, boot-time (nix/module.nix's deterministic
    posture) and modprobe'd now. Returns each module's state for
    the summary's consent line: a name the kernel tree lacks gets
    the named observation and remediation (#550), not the plain
    load-failure warning."""
    write_file(MODULES_FILE, MODULES_HEADER + "\n".join(MODULES) + "\n")
    return report_module_states(module_states())


def module_states() -> dict[str, str]:
    """Every module's state, probed in list order."""
    return {module: load_module(module) for module in MODULES}


def report_module_states(states: dict[str, str]) -> dict[str, str]:
    """Warn per failure kind and hand the states to the summary."""
    warn_load_failures(
        [m for m, state in states.items() if state == LOAD_FAILED]
    )
    warn_absent_modules([m for m, state in states.items() if state == ABSENT])
    return states


def load_module(module: str) -> str:
    """One module's state: the dry-run resolves whether the kernel
    tree carries it at all (a built-in answers yes; kmod answers 1
    for a name the tree lacks — every other answer, including sh's
    -1 infra failure, is a load failure, not an absence), then the
    real load runs."""
    rc, _out, _err = sh(["modprobe", "--dry-run", module])
    if rc == 1:
        return ABSENT
    if rc != 0:
        return LOAD_FAILED
    rc, _out, _err = sh(["modprobe", module])
    return LOADED if rc == 0 else LOAD_FAILED


def warn_load_failures(failed: list[str]) -> None:
    """A module the tree carries whose load failed: a genuine
    kernel-level failure, the generic warning (the daemon's
    ruleset load fails closed naming the cause)."""
    for module in failed:
        print(
            f"{TOOL}: WARNING modprobe {module} failed — check the kernel",
            file=sys.stderr,
        )


MODULES_ROOT = "/lib/modules"


def module_tree_present() -> bool:
    """Whether /lib/modules carries a tree for the running release
    at all — a container or a pruned host can hold none."""
    return Path(MODULES_ROOT, os.uname().release).is_dir()


def tree_cause() -> str:
    """Why the tree lacks a module, as the warning states it: no
    tree for this release (the kernel package is not installed —
    a container, a trimmed image) or a tree without the module (a
    trimmed kernel build; stock Debian flavors, cloud included,
    carry the consent modules — #550's evidence host ran a pruned
    tree, not a stock flavor)."""
    if not module_tree_present():
        return "no module tree is installed on this host"
    return "this kernel's build carries a trimmed module set"


def kernel_package() -> str:
    """The generic flavor's package for this machine — Debian's
    arch names, not uname's machine names (x86_64 installs
    linux-image-amd64, aarch64 linux-image-arm64)."""
    machine = os.uname().machine
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"linux-image-{arch}"


def warn_absent_modules(absent: list[str]) -> None:
    """Name the absence and its remediation (#550): the observation
    (the module is not in this kernel's tree) plus the cause shape
    and the generic-flavor fix — a causal claim beyond that is one
    the tool cannot verify."""
    for module in absent:
        print(
            f"{TOOL}: WARNING {module} is absent from this kernel's "
            f"module tree — {tree_cause()} "
            f"(running release {os.uname().release})",
            file=sys.stderr,
        )
    if absent:
        print(
            f"{TOOL}: the generic flavor carries them: apt install "
            f"{kernel_package()}, reboot, re-run",
            file=sys.stderr,
        )


def uplink() -> str:
    """The host's default-route interface, for the summary's
    MSKSD_EGRESS_UPLINK hint."""
    rc, out, _err = sh(["ip", "route", "show", "default"])
    if rc != 0:
        return ""
    words = out.split()
    for index, word in enumerate(words[:-1]):
        if word == "dev":
            return words[index + 1]
    return ""


def kvm_hint() -> str:
    try:
        grp.getgrnam("kvm")
    except KeyError, OSError:
        # KeyError: no such group; OSError: the NSS backend failed —
        # the script's getent degraded the same way.
        return (
            "the kvm group is absent — install the KVM stack (qemu-kvm) first"
        )
    return "adduser <daemon-user> kvm   (the kvm group gates the node)"


def consent_line(states: dict[str, str]) -> str:
    """The summary's consent verdict: interactive egress consent
    needs every consent module loaded (#550)."""
    missing = [
        module for module in CONSENT_MODULES if states.get(module) != LOADED
    ]
    if not missing:
        return "interactive egress consent available"
    return (
        "interactive egress consent UNAVAILABLE on this kernel — "
        f"missing: {', '.join(missing)}"
    )


def summary(ambient: str, states: dict[str, str]) -> None:
    say("done —")
    print(f"  wrapper:    {WRAPPER} (setpriv, {CAPS}+ep)")
    print(f"               dev-daemon use: DEV_CAPS={WRAPPER} msks-dev")
    print(f"  forwarding: net.ipv4.ip_forward=1 ({SYSCTL_FILE}, active now)")
    print(f"  modules:    {MODULES_FILE} ({','.join(MODULES)})")
    print(
        f"  tools:      ip={which('ip')} nft={which('nft')} "
        f"conntrack={which('conntrack')}"
    )
    print(f"  /dev/kvm:   {kvm_hint()}")
    print(f"  consent:    {consent_line(states)}")
    print(
        "  uplink:     point the daemon at this host's default-route "
        "interface (MSKSD_EGRESS_UPLINK, or DEV_UPLINK for "
        f"msks-dev): {uplink() or 'none found'}"
    )


def run_setup() -> None:
    """Every phase in order; the first refusal stops the run. The
    Debian PATH (sudo can strip /usr/sbin, where setcap and friends
    live) holds for the run and is restored after it."""
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = DEBIAN_PATH + ":" + saved
    try:
        check_root_and_platform()
        missing = missing_packages()
        if missing:
            install_packages(missing)
        install_wrapper()
        ambient = wrapper_ambient_proof()
        say(f"wrapper {WRAPPER} carries {CAPS}+ep (CapAmb {ambient})")
        land_sysctl()
        states = land_modules()
        summary(ambient, states)
    finally:
        os.environ["PATH"] = saved


def main() -> int:
    """The console entry: 0 on success, 1 on a named refusal."""
    try:
        run_setup()
    except HostSetupError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 1
    return 0
