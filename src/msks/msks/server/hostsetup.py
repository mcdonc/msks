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
3. The systemd user service (#560), on a host systemd manages:
   the tool writes ``~/.config/systemd/user/msksd.service`` for
   the executing user — the operator who invoked the tool
   (``SUDO_USER`` under sudo), whose own state dir, image
   catalog, and #555 socket identity the service inherits —
   execs the daemon through the wrapper (the wrapper's file
   capabilities grant any executor the two caps, so a user unit
   holds the same posture a root unit would), creates the #555
   socket's runtime directory under the user's own
   ``$XDG_RUNTIME_DIR``, and ``loginctl enable-linger`` keeps
   the user manager — and the daemon — running across logouts
   and boots. A host without systemd (a container, a non-systemd
   init), a deployment whose msksd lives elsewhere, or
   ``--no-service`` keeps today's behavior: every host piece
   above, no unit.

The plumbing tools the daemon execs (ip, nft, conntrack) install
through apt when missing — and with #598, so does every other
package `msksd doctor` reads: the volume trio's e2fsprogs, the
image pair's qemu-utils and genisoimage, the client workflows'
openssh-client, rsync, and tmux, and the consent stack's two
NFQUEUE soname packages (probed through the linker cache, the
same evidence doctor's library rows read). The VMM lands from
the upstream static release — Debian's archive carries
cloud-hypervisor in no suite — when the host has none, for the
host's own architecture, and the landed binary answers
``--version`` before the run calls it landed; a present binary
stays untouched. The phase runs last, so a host whose egress
passes the Debian mirror but not github.com still finishes
every other piece. /dev/kvm access arrives through the kvm
group on the daemon's user, the same supplementary-group grant the
NixOS unit makes — the tool adds the executing user to the group
when missing (#560; bare runs and new logins need it on the
account). A user manager that was already running forked before
the group write reaches it — the daemon holds the old groups
until that manager restarts, and the service line says so.
Idempotent: re-run any time. Re-run after a util-linux
upgrade — the setpriv copy under /usr/local keeps running the old
binary until refreshed.

``--uninstall`` (#604) is the mirror, in teardown order: the
service stops first (workspaces included, inside the unit's
180s stop grace — a user manager that is down runs no stop at
all, because no manager means no daemon), then the unit file
and its enablement symlink, the sysctl and
modules files leave (``sysctl --system`` re-applies every
remaining sysctl.d file, so a second owner's setting survives),
the wrapper, the kvm membership (the tool keeps no state, so
any member leaves — the line names the restore), the
``~/.profile`` block, and the cloud-hypervisor binary — while
the apt packages stay (they are shared Debian packages; the
summary names the ones present with the ``apt-get`` line that
removes them). Idempotent the same way: re-run any time; a
piece already gone is a line in the summary, not a refusal,
and the first real refusal stops the run, the setup run's rule.
"""

import argparse
import contextlib
import grp
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOOL = "msks-setup-debian-host"

# The service phase's names (#560): the user unit's location
# under the executing user's home, and the directory whose
# presence says systemd is the manager (the same probe systemctl
# itself gates on; a container or another init leaves it absent).
# The service is a user service of the executing user — their
# state dir, image catalog, and #555 socket identity carry over —
# so no system unit, no User=/Group= lines, and no state
# relocation are involved.
USER_UNIT_RELATIVE = ".config/systemd/user/msksd.service"
SYSTEMD_MANAGER = "/run/systemd/system"

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

# command → package for the wrapper path, the phases the setup
# itself runs, and the daemon's and client's bare-name exec
# surface — the tools `msksd doctor` probes (#598).
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
    # the workspace-volume trio (#184) and the image tools the
    # daemon execs: the root overlay's prober and the seed
    # disk's packer
    "mkfs.ext4": "e2fsprogs",
    "resize2fs": "e2fsprogs",
    "e2fsck": "e2fsprogs",
    "qemu-img": "qemu-utils",
    "mkisofs": "genisoimage",
    # the client-side pair the documented workflows run over the
    # forward, and the consent terminal's host tmux (3.2 floor)
    "ssh": "openssh-client",
    "rsync": "rsync",
    "tmux": "tmux",
    # the VMM download's own trust root: slim images ship no
    # CA bundle, and urllib refuses the TLS handshake without
    # one. The package's probeable command names it.
    "update-ca-certificates": "ca-certificates",
}

# soname → package for the consent stack's NFQUEUE libraries
# (#598): no binary answers a `which`, so the linker cache is
# the probe.
SONAME_PACKAGES = {
    "libnetfilter_queue.so.1": "libnetfilter-queue1",
    "libnfnetlink.so.0": "libnfnetlink0",
}

# The VMM (#598): Debian's archive carries cloud-hypervisor in
# no suite, so the tool lands the documented upstream static
# release itself (docs/installation.md names the same source).
# The release carries one static build per architecture; the
# unsuffixed asset is x86-64, aarch64's carries -aarch64 —
# doctor's own fix hint names the same pair.
CLOUD_HYPERVISOR_RELEASE = (
    "https://github.com/cloud-hypervisor/cloud-hypervisor/releases"
    "/latest/download"
)
CLOUD_HYPERVISOR_ASSETS = {
    "x86_64": "cloud-hypervisor-static",
    "aarch64": "cloud-hypervisor-static-aarch64",
}
CLOUD_HYPERVISOR_BIN = "/usr/local/bin/cloud-hypervisor"


def cloud_hypervisor_url() -> str:
    """The upstream static-release URL for this host's machine —
    kernel_package()'s uname() probe is the in-file precedent."""
    asset = CLOUD_HYPERVISOR_ASSETS.get(
        os.uname().machine, "cloud-hypervisor-static"
    )
    return f"{CLOUD_HYPERVISOR_RELEASE}/{asset}"


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


def file_exists(path: str) -> bool:
    """Whether one landed file is there — the existence probe the
    uninstall phases gate on (write_file's read-side twin). A
    dangling symlink counts as present: the tool lands real
    files, so a link at a landed path is a stranger's, and the
    unlink that follows removes the link itself, never its
    target."""
    target = Path(path)
    return target.exists() or target.is_symlink()


def remove_file(path: str) -> bool:
    """Remove one landed file — write_file's mirror, the seam the
    tests rig. True when the file was there, False when it was
    already gone (the idempotent re-run — including a file that
    vanishes between the probe and the unlink); an unlink that
    itself fails is a named refusal."""
    if not file_exists(path):
        return False
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostSetupError(
            f"cannot remove {path} ({exc.strerror or exc})"
        ) from None
    return True


def replace_file(source: Path, target: Path) -> None:
    """One atomic rename — the seam the tests rig."""
    os.replace(source, target)


def which(tool: str) -> str | None:
    return shutil.which(tool)


#: Debian's admin directories: the systemd user manager's default
#: PATH carries them (the daemon's own search path), while a
#: non-root login shell's may not (#601).
PATH_EXTRA_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")

#: The idempotent marker for the ~/.profile block (#601).
PROFILE_MARKER = "# msks: Debian keeps the admin tools in /usr/sbin and /sbin"

#: The block the tool appends to the operator's ~/.profile (#601):
#: guarded additions at the tail of PATH — Debian's own order puts
#: /usr/local and the account's own bins ahead, so the block never
#: shadows a user-local tool that shares a name with an sbin one.
PROFILE_PATH_BLOCK = f"""\
{PROFILE_MARKER}
# added by msks-setup-debian-host; applies at the next login
case ":$PATH:" in
  *":/usr/sbin:"*) ;;
  *) PATH="$PATH:/usr/sbin" ;;
esac
case ":$PATH:" in
  *":/sbin:"*) ;;
  *) PATH="$PATH:/sbin" ;;
esac
export PATH
"""


def profile_text(profile: Path) -> str:
    """The profile's current text; absent reads as empty, and a
    decode failure is the named refusal (the tool never truncates
    a profile it cannot parse)."""
    try:
        return read_file(str(profile))
    except OSError:
        return ""
    except ValueError:
        raise HostSetupError(
            f"{profile} is not UTF-8 text; add /usr/sbin and /sbin "
            "to your PATH by hand"
        ) from None


def write_profile(
    profile: Path, text: str, uid: int, gid: int, fix: str
) -> None:
    """The profile's atomic write, both directions' shared tail:
    a sibling temp, one rename, the operator's own ownership
    (root-owned dotfiles would lock the account out of its own
    profile). *fix* is the by-hand remediation a refusal names."""
    partial = profile.with_name(".profile.msks-new")
    try:
        write_file(str(partial), text)
        replace_file(partial, profile)
    except OSError as exc:
        # The refusal leaves no partial behind (the VMM phase's
        # #599 pattern): best-effort, the error is the message.
        with contextlib.suppress(OSError):
            Path(partial).unlink(missing_ok=True)
        raise HostSetupError(f"cannot write {profile} ({exc}); {fix}") from exc
    chown_path(profile, uid, gid)


def land_user_path(home: str, uid: int, gid: int) -> str:
    """Persist the admin directories on the operator's login PATH
    (#601): an idempotent block in their ~/.profile, so the
    session that typed the command — and the next one — sees the
    tools the package phases land. Re-runs never duplicate the
    block; the marker answers. The file lands atomically and
    stays the operator's own; anything unreadable is a named
    refusal, and a re-run leaves an unparseable profile
    untouched."""
    profile = Path(home) / ".profile"
    text = profile_text(profile)
    if PROFILE_MARKER in text:
        return "already there (the next login carries /usr/sbin and /sbin)"
    separator = "" if not text or text.endswith("\n") else "\n"
    fresh = text + separator + PROFILE_PATH_BLOCK
    write_profile(
        profile,
        fresh,
        uid,
        gid,
        fix="add /usr/sbin and /sbin to your PATH by hand",
    )
    return f"{profile} gains /usr/sbin and /sbin at the next login"


def drop_user_path(home: str, uid: int, gid: int) -> str:
    """The ~/.profile block's mirror (#604): the block leaves with
    the tool and the operator's own lines stay — a profile that
    held nothing else was the tool's own file and goes whole. A
    block the operator edited inside stays put with the by-hand
    fix named — the tool never rewrites a profile it cannot
    recognize."""
    profile = Path(home) / ".profile"
    text = profile_text(profile)
    if PROFILE_MARKER not in text:
        return f"no msks block in {profile}"
    if PROFILE_PATH_BLOCK not in text:
        return (
            f"the block in {profile} was edited by hand — remove it "
            "yourself (it runs from the msks marker line to the "
            "export)"
        )
    stripped = text.replace(PROFILE_PATH_BLOCK, "")
    if not stripped.strip():
        remove_file(str(profile))
        return f"{profile} held only the block (removed)"
    write_profile(
        profile,
        stripped.rstrip("\n") + "\n",
        uid,
        gid,
        fix="remove the msks block by hand",
    )
    return f"{profile} carries the operator's own lines only"


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


def missing_soname_packages() -> list[str]:
    """The NFQUEUE soname packages the linker cache does not show.

    A probe that cannot run counts as absent: the install is
    idempotent, and a host doctor cannot read stays honest when
    the packages land anyway.
    """
    rc, out, _err = sh(["ldconfig", "-p"])
    if rc != 0:
        return list(SONAME_PACKAGES.values())
    return [p for soname, p in SONAME_PACKAGES.items() if soname not in out]


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


def fetch_upstream(url: str, dest: str) -> None:
    """Stream one upstream artifact to dest — the network seam
    the tests rig; no shell, no pipeline."""
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        open(dest, "wb") as sink,
    ):
        shutil.copyfileobj(response, sink)


def install_cloud_hypervisor() -> str | None:
    """The VMM from the upstream static release (#598): Debian's
    archive carries it in no suite, so the tool lands the binary
    the Installation chapter documents, for this host's
    architecture. Returns the binary's path when it landed, None
    when the host already had one — a present binary stays
    untouched (an operator's own install, a pinned release,
    wins). The landed binary answers `--version` before the run
    calls it landed: a truncated download, an architecture
    mismatch, or a dead redirect is a refusal that removes what
    it downloaded, not a broken VMM the next run defends."""
    if which("cloud-hypervisor") is not None:
        return None
    url = cloud_hypervisor_url()
    partial = CLOUD_HYPERVISOR_BIN + ".part"
    try:
        fetch_upstream(url, partial)
    except Exception as exc:  # any network failure, named for the fix
        _silent_unlink(partial)
        raise HostSetupError(
            f"cloud-hypervisor download failed ({exc}); install the "
            f"upstream static release from {url}"
        ) from exc
    os.chmod(partial, 0o755)
    os.replace(partial, CLOUD_HYPERVISOR_BIN)
    rc, out, err = sh([CLOUD_HYPERVISOR_BIN, "--version"])
    if rc != 0:
        _silent_unlink(CLOUD_HYPERVISOR_BIN)
        raise HostSetupError(
            f"the downloaded cloud-hypervisor does not run ({err.strip()} "
            f"or {out.strip()}); install the upstream static release "
            f"for {os.uname().machine} from {url}"
        )
    return CLOUD_HYPERVISOR_BIN


def _silent_unlink(path: str) -> None:
    """Best-effort cleanup on the refusal paths; the error that
    caused the refusal is the message, not the cleanup's."""
    try:
        os.unlink(path)
    except OSError:
        pass


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


def in_group(user: str, name: str) -> bool:
    """Whether *user* holds group *name* — by membership or as its
    primary group (the same two gates #555's peer policy reads).
    A database that answers nothing says no, and the caller's
    adduser is idempotent either way."""
    try:
        entry = grp.getgrnam(name)
        account = pwd_getpwnam(user)
    except KeyError, OSError:
        return False
    return user in entry.gr_mem or account.pw_gid == entry.gr_gid


def ensure_kvm_membership(user: str) -> tuple[str, bool]:
    """Make sure the executing user reaches /dev/kvm (#560): bare
    runs (msks-dev, a hand-started msksd) need the kvm group on
    the account — the tool adds the user when missing. Returns
    the summary's /dev/kvm line and whether the membership was
    added this run (a user manager already running forked before
    the group existed — its services, this daemon included, hold
    the group only after that manager restarts)."""
    if not group_present("kvm"):
        return (
            "the kvm group is absent — install the KVM stack (qemu-kvm) first",
            False,
        )
    if in_group(user, "kvm"):
        return (
            f"{user} is a member of kvm (the kvm group gates the node)",
            False,
        )
    rc, _out, err = sh(["adduser", user, "kvm"])
    if rc != 0:
        raise HostSetupError(f"adduser {user} kvm failed: {err}")
    return (
        f"added {user} to kvm — a fresh login picks the group up",
        True,
    )


def group_present(name: str) -> bool:
    """Whether the group database carries *name* (an NSS failure
    answers no — the same degrade kvm_hint's probe took)."""
    try:
        grp.getgrnam(name)
    except KeyError, OSError:
        return False
    return True


def executing_user() -> tuple[str, int, int, str]:
    """The operator the user service belongs to: name, uid, gid,
    home — SUDO_USER when sudo lifted the tool to root (the
    account that typed the command), the effective account
    otherwise (a root shell's own choice). The #555 posture: the
    daemon and its clients share one user, whose state dir, image
    catalog, and token-free socket identity the service keeps;
    the gid chowns the unit's parent chain into that user's
    hands."""
    name = os.environ.get("SUDO_USER")
    entry = None
    if name:
        try:
            entry = pwd_getpwnam(name)
        except KeyError, OSError:
            entry = None
    if entry is None:
        entry = pwd_getpwuid(os.geteuid())
    return entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir


def pwd_getpwnam(name: str):
    """pwd.getpwnam, as a seam the suite can pin (the same shape
    sh and write_file take)."""
    return pwd.getpwnam(name)


def pwd_getpwuid(uid: int):
    """pwd.getpwuid, as a seam the suite can pin."""
    return pwd.getpwuid(uid)


def msksd_sibling() -> str | None:
    """The msksd the deployment brought, as this tool's own bin
    sibling (the venv that shipped the tool ships the daemon), or
    None when no sibling exists — the deployment runs its daemon
    from elsewhere and the service phase names that and stands
    down (invoked_as()'s reasoning: an install root's bin dir is
    the one path that locates the deployment)."""
    sibling = Path(sys.argv[0]).resolve().parent / "msksd"
    return str(sibling) if sibling.exists() else None


def systemd_manages() -> bool:
    """Whether a systemd manager owns this host and systemctl is
    there to reach it — the unit is dead text without both."""
    return Path(SYSTEMD_MANAGER).is_dir() and which("systemctl") is not None


def unit_path(home: str) -> str:
    """Where the user unit lands: under the executing user's own
    config, the location the user manager loads. The file itself
    is root-owned mode 0644 (write_file's shape); the directory
    chain below the home belongs to the user, so an operator can
    replace the file — the same grant the world-executable
    wrapper already makes."""
    return str(Path(home) / USER_UNIT_RELATIVE)


def user_systemctl(user: str, uid: int, args: list[str]) -> list[str]:
    """One systemctl --user call made as the executing user: the
    tool runs as root, so runuser (util-linux, a package the tool
    already installs) drops to the account and names its runtime
    dir — the one the user manager's socket lives under."""
    return [
        "runuser",
        "-u",
        user,
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "systemctl",
        "--user",
        *args,
    ]


def unit_text(msksd: str, uplink_name: str) -> str:
    """The msksd.service user unit: the wrapper raises the two
    caps ambient (dev-daemon.sh's exec line verbatim — the file
    capabilities grant any executor the same posture a root unit
    would), the #555 local socket's runtime directory lands under
    the user's own $XDG_RUNTIME_DIR (the client's default
    candidate path), and egress runs enabled against the uplink
    the tool detected. A user unit carries no User=/Group=/HOME —
    the user manager is the account."""
    lines = [
        "# Generated by msks-setup-debian-host (#560): re-run the "
        "tool to refresh this file.",
        "[Unit]",
        "Description=msks daemon (workspaces as cloud-hypervisor microvms)",
        "Documentation=https://github.com/mcdonc/msks",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "ExecStart="
        + WRAPPER
        + " --inh-caps=+net_admin,+net_bind_service"
        + " --ambient-caps=+net_admin,+net_bind_service"
        + f' -- "{msksd}"',
        "Environment=MSKSD_EGRESS_ENABLED=true",
    ]
    if uplink_name:
        lines.append(f"Environment=MSKSD_EGRESS_UPLINK={uplink_name}")
    lines += [
        "RuntimeDirectory=msks",
        "Restart=always",
        "RestartSec=1",
        "TimeoutStopSec=180s",
        "StandardInput=null",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


RUNTIME_DIR_FMT = "/run/user/{}"
RETRY_DELAY = 1.0


def manager_running(uid: int) -> bool:
    """Whether the user manager for *uid* is up (its runtime dir
    exists). Read before linger is armed: a manager that was
    already running forked before any group this run adds, so its
    services hold the old supplementary groups."""
    return Path(RUNTIME_DIR_FMT.format(uid)).is_dir()


def chown_path(path: Path, uid: int, gid: int) -> None:
    """os.chown, as a seam the suite can pin."""
    os.chown(path, uid, gid)


def ensure_unit_parents(home: str, uid: int, gid: int) -> None:
    """Create the unit's parent chain user-owned. A fresh Debian
    account ships no ~/.config, and write_file's mkdir would land
    root-owned parents there — the user manager's enable step
    writes its symlink as the user and a root-owned parent
    refuses it, on this run and every re-run. Each level the
    chain is missing is created and handed to the executing user;
    levels that exist keep their owner."""
    current = Path(home)
    for part in Path(USER_UNIT_RELATIVE).parts[:-1]:
        current = current / part
        if not current.exists():
            current.mkdir()
            chown_path(current, uid, gid)
            current.chmod(0o755)


def user_manager_call(
    user: str, uid: int, args: list[str], timeout: float = 10.0
) -> tuple[int, str]:
    """One systemctl --user call, with the linger-startup retry:
    enable-linger returns after logind enqueues the user manager,
    so the first call can beat the manager's bus into existence —
    a "Failed to connect to bus" answer retries briefly before it
    is treated as a failure."""
    cmd = user_systemctl(user, uid, args)
    rc, _out, err = sh(cmd, timeout=timeout)
    tries = 0
    while rc != 0 and "Failed to connect to bus" in err and tries < 4:
        time.sleep(RETRY_DELAY)
        rc, _out, err = sh(cmd, timeout=timeout)
        tries += 1
    return rc, err


def write_unit(home: str, msksd: str, uplink_name: str) -> bool:
    """Build the user unit and land it at its path. True when a
    different unit was replaced — a re-run that changes the
    daemon's line or its uplink — so the caller can restart a
    running daemon onto the new unit. A missing unit file is a
    first landing; any other read failure is the caller's named
    refusal, not a silent rebuild."""
    path = unit_path(home)
    try:
        existing = read_file(path)
    except FileNotFoundError:
        existing = ""
    text = unit_text(msksd, uplink_name)
    write_file(path, text)
    return existing not in ("", text)


def linger(user: str) -> None:
    """Keep the user manager — and the daemon with it — running
    across logouts and boots. Idempotent; enabling it also starts
    the manager now when no session had."""
    rc, _out, err = sh(["loginctl", "enable-linger", user])
    if rc != 0:
        raise HostSetupError(
            f"loginctl enable-linger {user} failed: {err} "
            "(the daemon would stop at the operator's logout)"
        )


def load_unit(user: str, uid: int) -> None:
    """daemon-reload through the bus-race retry; a manager that
    never answers is a named refusal."""
    rc, err = user_manager_call(user, uid, ["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl --user daemon-reload failed: {err}")


def start_unit(user: str, uid: int) -> None:
    """Enable and start the service — a first landing's only
    start; the failure names the journal that holds the cause."""
    rc, err = user_manager_call(
        user, uid, ["enable", "--now", "msksd.service"]
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user enable --now msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def restart_running_unit(user: str, uid: int) -> None:
    """Restart the daemon, waited out: a stop stops workspaces
    first (the unit allows 180s), so this call carries a matching
    timeout, and the restart command's own failure is named."""
    rc, err = user_manager_call(
        user, uid, ["restart", "msksd.service"], timeout=240.0
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user restart msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def activate_unit(user: str, uid: int, changed: bool) -> None:
    """Linger on, load the unit, enable and start it, and restart
    a running daemon when the unit changed under it — a fresh
    landing starts through enable alone."""
    linger(user)
    load_unit(user, uid)
    start_unit(user, uid)
    if not changed:
        return
    rc, _err = user_manager_call(
        user, uid, ["is-active", "--quiet", "msksd.service"]
    )
    if rc == 0:
        restart_running_unit(user, uid)


def service_skip_cause(msksd: str | None) -> str | None:
    """Why the service phase stands down, as the summary's line
    states it: no msksd beside the tool, or no systemd manager.
    None means the phase proceeds."""
    if msksd is None:
        return (
            "skipped (no msksd beside this tool; the deployment's "
            "daemon is elsewhere — bare msksd unchanged)"
        )
    if not systemd_manages():
        return (
            "skipped (no systemd manager on this host — bare msksd unchanged)"
        )
    return None


def service_note(user: str, uid: int, manager_up_first: bool) -> str:
    """The summary's service line. A user manager that was already
    running forked before any kvm membership this run adds — and a
    re-run cannot prove the manager postdates an older membership
    either — so whenever the manager predates the proof, the line
    names the symptom (workspaces failing on /dev/kvm) and the
    manager restart that hands the daemon the group, with what
    that restart really stops (the user's units; login sessions
    survive it)."""
    note = f"user service msksd.service ({user}, enabled, started, lingering)"
    if manager_up_first:
        note += (
            f" — the manager may predate the kvm membership; if "
            f"workspaces fail on /dev/kvm, restart it: "
            f"sudo systemctl restart user@{uid}.service "
            f"(it stops that user's units — the daemon and its "
            f"workspaces; login sessions survive)"
        )
    return note


def install_service(
    user: str, uid: int, gid: int, home: str, uplink_name: str
) -> str:
    """Land the daemon's systemd user service (#560): the unit in
    the executing user's own config, enable-and-start with linger
    — and a restart when a re-run rewrote a unit that was already
    running, so a refreshed ExecStart or uplink takes effect.
    Returns the summary's service line; every stand-down names
    its cause instead of refusing (the host pieces above landed,
    and a container or an elsewhere daemon keeps a working
    host)."""
    msksd = msksd_sibling()
    cause = service_skip_cause(msksd)
    if cause is not None:
        return cause
    manager_up_first = manager_running(uid) and group_present("kvm")
    try:
        ensure_unit_parents(home, uid, gid)
        changed = write_unit(home, msksd, uplink_name)
        activate_unit(user, uid, changed)
    except OSError as exc:
        raise HostSetupError(f"landing the user unit failed: {exc}") from exc
    return service_note(user, uid, manager_up_first)


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


def summary(
    ambient: str,
    states: dict[str, str],
    kvm: str,
    service: str,
    uplink_name: str,
    login_path: str = "",
) -> None:
    say("done —")
    print(f"  wrapper:    {WRAPPER} (setpriv, {CAPS}+ep)")
    print(f"               dev-daemon use: DEV_CAPS={WRAPPER} msks-dev")
    print(f"  forwarding: net.ipv4.ip_forward=1 ({SYSCTL_FILE}, active now)")
    print(f"  modules:    {MODULES_FILE} ({','.join(MODULES)})")
    print(
        f"  tools:      ip={which('ip')} nft={which('nft')} "
        f"conntrack={which('conntrack')}"
    )
    print(f"  /dev/kvm:   {kvm}")
    print(f"  login PATH: {login_path}")
    print(f"  consent:    {consent_line(states)}")
    print(
        "  uplink:     point the daemon at this host's default-route "
        "interface (MSKSD_EGRESS_UPLINK, or DEV_UPLINK for "
        f"msks-dev): {uplink_name or 'none found'}"
    )
    print(f"  service:    {service}")
    if "started" in service:
        print(
            "               same-user clients connect token-free "
            "(#555): $XDG_RUNTIME_DIR/msks/msksd.sock"
        )


def run_setup(service: bool = True) -> None:
    """Every phase in order; the first refusal stops the run. The
    Debian PATH (sudo can strip /usr/sbin, where setcap and friends
    live) holds for the run and is restored after it."""
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = DEBIAN_PATH + ":" + saved
    try:
        check_root_and_platform()
        missing = list(
            dict.fromkeys(missing_packages() + missing_soname_packages())
        )
        if missing:
            install_packages(missing)
        install_wrapper()
        ambient = wrapper_ambient_proof()
        say(f"wrapper {WRAPPER} carries {CAPS}+ep (CapAmb {ambient})")
        land_sysctl()
        states = land_modules()
        user, uid, gid, home = executing_user()
        the_uplink = uplink()
        kvm_note, _kvm_added = ensure_kvm_membership(user)
        path_note = land_user_path(home, uid, gid)
        service_note = (
            install_service(user, uid, gid, home, the_uplink)
            if service
            else "skipped (--no-service)"
        )
        summary(
            ambient,
            states,
            kvm_note,
            service_note,
            the_uplink,
            login_path=path_note,
        )
        # The VMM lands last and after the summary: it is the one
        # phase that needs the open internet (a host behind an
        # egress policy that passes the Debian mirror but not
        # github.com still gets every other piece), and its
        # refusal names the manual install the Installation
        # chapter documents.
        vmm = install_cloud_hypervisor()
        if vmm is not None:
            say(f"cloud-hypervisor: landed {vmm} (upstream static release)")
    finally:
        os.environ["PATH"] = saved


def stop_unit(user: str, uid: int) -> None:
    """Disable and stop the service, waited out: a stop stops
    workspaces first (the unit allows 180s), so this call carries
    a matching timeout, and the failure names the journal that
    holds the cause — start_unit's refusal, mirrored."""
    rc, err = user_manager_call(
        user, uid, ["disable", "--now", "msksd.service"], timeout=240.0
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user disable --now msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def forget_unit(user: str, uid: int) -> None:
    """daemon-reload once the unit file is gone, so the manager
    forgets it — load_unit's refusal, mirrored."""
    rc, err = user_manager_call(user, uid, ["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl --user daemon-reload failed: {err}")


def disable_linger(user: str) -> None:
    """The linger the service phase armed leaves with it; the
    call is idempotent, and the failure names it."""
    rc, _out, err = sh(["loginctl", "disable-linger", user])
    if rc != 0:
        raise HostSetupError(
            f"loginctl disable-linger {user} failed: {err} "
            "(the user manager keeps running across logouts)"
        )


def wants_path(home: str) -> str:
    """The enablement symlink `enable --now` lands (the unit's
    WantedBy) — the piece `disable --now` is otherwise the only
    thing that removes, so a teardown without a bus call takes
    it by path. Derived from USER_UNIT_RELATIVE so the basename
    and the unit's own location stay one name."""
    relative = Path(USER_UNIT_RELATIVE)
    return str(
        Path(home) / relative.parent / "default.target.wants" / relative.name
    )


def teardown_running_service(
    user: str, uid: int, unit: str, wants: str
) -> str:
    """The manager-up teardown: stop and disable (the stop waits
    out the unit's shutdown grace), both files, the reload that
    forgets the unit, and the linger — the summary line names
    linger's restore."""
    stop_unit(user, uid)
    remove_file(unit)
    remove_file(wants)
    forget_unit(user, uid)
    disable_linger(user)
    return (
        f"user service msksd.service stopped, disabled, and "
        f"removed ({user}; lingering off — loginctl "
        f"enable-linger {user} restores it)"
    )


def teardown_busless_service(
    user: str, uid: int, home: str, unit: str, wants: str, manager_up: bool
) -> str:
    """The no-stop teardown: a down user manager runs no daemon,
    and a unit file that is already gone left none behind — the
    files and the linger leave by path, no stop call involved, so
    the teardown holds its every-piece-leaves contract anyway. A
    manager that is up still hears the reload, so it forgets the
    unit the files no longer name. Neither piece on disk is the
    stand-down: the linger hint names the by-hand fix, and the
    tool never removes state it cannot see."""
    unit_present = file_exists(unit)
    if not unit_present and not file_exists(wants):
        return (
            f"no msksd.service under {home} — lingering may remain; "
            f"loginctl disable-linger {user} turns it off"
        )
    remove_file(unit)
    remove_file(wants)
    if manager_up:
        forget_unit(user, uid)
    disable_linger(user)
    if unit_present:
        return (
            f"user service msksd.service removed ({user}; the user "
            "manager was down — no daemon was running; lingering off)"
        )
    return (
        "the msksd.service unit file was already gone; the enablement "
        f"symlink is removed and lingering is off ({user})"
    )


def uninstall_service(user: str, uid: int, home: str) -> str:
    """The service phase's mirror (#604): the full stop when a
    manager is up and the unit file is there, the busless path
    otherwise. Returns the summary's service line."""
    if not systemd_manages():
        return "skipped (no systemd manager on this host)"
    unit = unit_path(home)
    wants = wants_path(home)
    manager_up = manager_running(uid)
    if manager_up and file_exists(unit):
        return teardown_running_service(user, uid, unit, wants)
    return teardown_busless_service(user, uid, home, unit, wants, manager_up)


def drop_sysctl() -> str:
    """The forwarding sysctl's mirror: the boot-time file leaves,
    then `sysctl --system` re-applies every remaining sysctl.d
    file — the same set a boot applies. A second owner that
    wrote its own file keeps its value; a live-only setter the
    tool never saw (a container runtime's own forwarding) keeps
    its value too, because --system names nothing itself. A
    --system run that trips on some other file's keys is a
    warning, not a refusal — the file removal is this tool's
    piece, the rest of sysctl.d is not."""
    if not remove_file(SYSCTL_FILE):
        return f"{SYSCTL_FILE} already absent"
    rc, _out, err = sh(["sysctl", "--system"])
    if rc != 0:
        print(
            f"{TOOL}: WARNING sysctl --system failed ({err.strip()}) — "
            "the remaining sysctl.d files apply at the next boot",
            file=sys.stderr,
        )
    return (
        f"{SYSCTL_FILE} removed (sysctl --system re-applied the rest — "
        "net.ipv4.ip_forward keeps its live value until the host reboots "
        "where no remaining file names it; sysctl -w "
        "net.ipv4.ip_forward=0 turns it off now)"
    )


def drop_modules_file() -> str:
    """The modules file's mirror: the boot-time list leaves; the
    loaded modules stay — the kernel shares them with whatever
    else loaded them, and a reboot clears the live set."""
    if not remove_file(MODULES_FILE):
        return f"{MODULES_FILE} already absent"
    return f"{MODULES_FILE} removed (loaded modules stay until reboot)"


def drop_wrapper() -> str:
    """The capability wrapper leaves — the caps rode on it."""
    if not remove_file(WRAPPER):
        return f"{WRAPPER} already absent"
    return f"{WRAPPER} removed"


def drop_cloud_hypervisor() -> str:
    """The VMM's mirror: the binary the tool lands at
    /usr/local/bin goes (#598's own documented location). The
    `.part` sibling a killed download can leave goes with it,
    best-effort the install's own refusal paths made it — residue
    cleanup never refuses the phase."""
    _silent_unlink(CLOUD_HYPERVISOR_BIN + ".part")
    if not remove_file(CLOUD_HYPERVISOR_BIN):
        return f"{CLOUD_HYPERVISOR_BIN} already absent"
    return f"{CLOUD_HYPERVISOR_BIN} removed"


def remove_kvm_membership(user: str) -> str:
    """The kvm membership's mirror: the account leaves the kvm
    group. The tool cannot prove which run added a membership —
    it keeps no state — so any member leaves, including one the
    tool never added, and the line names the restore because
    /dev/kvm is a node other uses share."""
    if not group_present("kvm"):
        return "the kvm group is absent (nothing was removed)"
    if not in_group(user, "kvm"):
        return f"{user} is not a member of kvm"
    rc, _out, err = sh(["deluser", user, "kvm"])
    if rc != 0:
        raise HostSetupError(f"deluser {user} kvm failed: {err}")
    return f"removed {user} from kvm (adduser {user} kvm restores it)"


def present_packages() -> list[str]:
    """The dependency map's packages whose probes answer present
    on this host — the ones the uninstall summary names for the
    operator's own apt run. The probes are the install's own
    (which() and the linker cache), so the list on the way out
    reads the same evidence the list on the way in did."""
    missing_sonames = missing_soname_packages()
    packages = [
        package
        for tool, package in TOOL_PACKAGES.items()
        if which(tool) is not None
    ]
    packages += [
        package
        for soname, package in SONAME_PACKAGES.items()
        if package not in missing_sonames
    ]
    return list(dict.fromkeys(packages))


def uninstall_summary(
    service: str,
    forwarding: str,
    modules: str,
    wrapper: str,
    kvm: str,
    login_path: str,
    vmm: str,
    packages: list[str],
) -> None:
    """The uninstall summary: every piece the run removed, in the
    install summary's shape, plus the apt line — the dependency
    packages stay, and the operator removes the ones present
    himself when nothing else uses them."""
    say("done —")
    print(f"  service:    {service}")
    print(f"  forwarding: {forwarding}")
    print(f"  modules:    {modules}")
    print(f"  wrapper:    {wrapper}")
    print(f"  /dev/kvm:   {kvm}")
    print(f"  login PATH: {login_path}")
    print(f"  VMM:        {vmm}")
    if not packages:
        print("  packages:   none of the msks dependency packages are present")
        return
    print(
        "  packages:   the msks dependency packages stay installed; "
        "remove the ones present yourself when nothing else uses them:"
    )
    print(f"               apt-get remove --purge {' '.join(packages)}")


def run_uninstall(service: bool = True) -> None:
    """The setup's mirror (#604), in teardown order: the service
    stops first — its wrapper and caps vanish next — then every
    host piece leaves, and the summary names the dependency
    packages for the operator's own apt call. The first refusal
    stops the run, the install run's rule."""
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = DEBIAN_PATH + ":" + saved
    try:
        check_root_and_platform()
        user, uid, gid, home = executing_user()
        service_note = (
            uninstall_service(user, uid, home)
            if service
            else "skipped (--no-service)"
        )
        forwarding = drop_sysctl()
        modules = drop_modules_file()
        wrapper = drop_wrapper()
        kvm = remove_kvm_membership(user)
        login_path = drop_user_path(home, uid, gid)
        vmm = drop_cloud_hypervisor()
        uninstall_summary(
            service_note,
            forwarding,
            modules,
            wrapper,
            kvm,
            login_path,
            vmm,
            present_packages(),
        )
    finally:
        os.environ["PATH"] = saved


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Prepare a Debian deployment host for msksd "
        "(#546): the capability wrapper, the routing kernel "
        "settings, and a systemd service for the daemon (#560).",
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="land every host piece but the systemd unit (a "
        "container, a non-systemd init, a deployment that runs "
        "msksd its own way)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="undo the setup (#604): stop and remove the msksd "
        "service, remove the wrapper, the routing kernel files, "
        "the cloud-hypervisor binary, the kvm membership, and the "
        "profile block — the apt packages stay, and the summary "
        "names them",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """The console entry: 0 on success, 1 on a named refusal."""
    args = parse_args(argv)
    try:
        if args.uninstall:
            run_uninstall(service=not args.no_service)
        else:
            run_setup(service=not args.no_service)
    except HostSetupError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 1
    return 0
