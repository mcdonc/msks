"""The tool and library checks.

The binaries the daemon execs (the VMM, virtiofsd, ssh, tmux),
the NFQUEUE sonames through the linker cache, and the NFQUEUE
module-load probe.
"""

from __future__ import annotations

import re
import shutil
import sys

from ...microvm.fsdaemons import (
    virtiofsd_floor_message,
    virtiofsd_meets_floor,
    virtiofsd_version,
)
from .hints import install_hint
from .probes import probe_which, run
from .report import CheckResult


def binary_missing(
    name: str,
    manager: str | None,
    *,
    is_warning: bool,
    use: str,
) -> CheckResult:
    """The miss for a PATH-absent binary, naming the path that breaks."""
    suffix = f" ({use})" if use else ""
    return CheckResult(
        name=name,
        ok=False,
        is_warning=is_warning,
        message=f"{name} not found on PATH{suffix}",
        hint=install_hint(name, manager),
    )


def check_binary(
    name: str,
    probe: list[str],
    manager: str | None,
    *,
    is_warning: bool = False,
    use: str = "",
) -> CheckResult:
    """Check a binary is on PATH and answers its probe.

    *probe* holds the capability command (``["mkisofs", "--version"]``);
    an empty list only verifies PATH presence, for tools whose every
    invocation mutates something (``resize2fs`` has no read-only mode
    worth trusting). *use* names the path that needs the tool, and
    appears in the missing message so the operator knows what breaks.
    """
    path = probe_which(name)
    if not path:
        return binary_missing(name, manager, is_warning=is_warning, use=use)
    if not probe:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")
    # The probe execs the resolved path, never the bare name: a
    # tool found only through the admin directories would exec
    # through the session PATH again and miss (#601 review).
    rc, _out, err = run([path, *probe[1:]])
    if rc != 0:
        detail = (err or f"exit {rc}").strip()[:200]
        return CheckResult(
            name=name,
            ok=False,
            is_warning=is_warning,
            message=f"{name} found at {path} but probe failed: {detail}",
            hint=install_hint(name, manager),
        )
    return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")


def check_virtiofsd(binary: str, manager: str | None) -> CheckResult:
    """The host-mount daemon's check (#655): presence, then the
    version floor. The mounts' argument vectors carry
    ``--translate-uid``/``--translate-gid`` squash mappings and
    ``--readonly``, which landed upstream in virtiofsd 1.13.0 —
    older builds parse none of them and exit at start (Linux
    Mint 22's archive ships 1.10.0), so a below-floor binary
    warns with the gap named instead of passing a check whose
    mounts then fail one start at a time. Warning grade like the
    presence check it replaces: a daemon without allowed host
    roots never execs virtiofsd."""
    use = "serves host mounts into each workspace (#620)"
    path = probe_which(binary)
    if not path:
        return binary_missing(binary, manager, is_warning=True, use=use)
    rc, out, err = run([path, "--version"])
    if rc != 0:
        detail = (err or f"exit {rc}").strip()[:200]
        return CheckResult(
            name=binary,
            ok=False,
            is_warning=True,
            message=f"{binary} found at {path} but probe failed: {detail}",
            hint=install_hint(binary, manager),
        )
    return virtiofsd_version_result(binary, path, virtiofsd_version(out))


def virtiofsd_version_result(
    binary: str, path: str, version: tuple[int, ...] | None
) -> CheckResult:
    """The version half of the virtiofsd row: the floor verdict
    for a binary whose ``--version`` answered."""
    if not virtiofsd_meets_floor(version):
        return CheckResult(
            name=binary,
            ok=False,
            is_warning=True,
            message=virtiofsd_floor_message(version),
        )
    found = ".".join(str(part) for part in version)
    return CheckResult(
        name=binary, ok=True, message=f"virtiofsd {found} ok ({path})"
    )


def ssh_result(manager: str | None, message: str) -> CheckResult:
    """A warning-grade ssh miss carrying the install hint."""
    return CheckResult(
        name="ssh",
        ok=False,
        is_warning=True,
        message=message,
        hint=install_hint("ssh", manager),
    )


def ssh_banner_ok(rc: int, err: str) -> bool:
    """True when the probe answered with OpenSSH's version banner."""
    return rc in (0, 1) and "OpenSSH" in err


def check_ssh(manager: str | None) -> CheckResult:
    """Check the host ssh client (#110's forward smoke, #112's workflow).

    ``ssh -V`` writes its version to stderr; the exit code varies
    by release (0 or 1), so the rc check steps aside for the
    "OpenSSH" banner in the probe's output instead.
    """
    path = probe_which("ssh")
    if not path:
        return ssh_result(
            manager,
            "ssh not found on PATH (the documented ssh workflow "
            "and the forward-path smoke run over `msks forward`)",
        )
    # The resolved path, never the bare name: a session without
    # the admin directories would exec through it again (#601).
    rc, out, err = run([path, "-V"])
    if ssh_banner_ok(rc, err):
        return CheckResult(name="ssh", ok=True, message=f"ssh ok ({path})")
    detail = (err.strip() or out.strip() or f"exit {rc}")[:200]
    return ssh_result(
        manager, f"ssh found at {path} but probe failed: {detail}"
    )


# The consent terminal's documented floor (#379): display-popup
# and the launch chain's bind-time-validated commands both need
# tmux 3.2.
TMUX_MIN_VERSION = (3, 2)


def parse_tmux_version(out: str) -> tuple[int, int] | None:
    """Parse ``tmux -V`` output (``"tmux 3.6a"``) → ``(3, 6)``.

    None when no MAJOR.MINOR pair is present, so the caller
    reports an unparseable version instead of guessing.
    """
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def tmux_unparseable(out: str, manager: str | None) -> CheckResult:
    """The miss for a ``tmux -V`` answer with no version in it."""
    probe = out.strip()[:40] or "tmux -V failed"
    return CheckResult(
        name="tmux",
        ok=False,
        message=(
            f"tmux version unparseable ({probe}); "
            "the consent terminal needs >= 3.2"
        ),
        hint=install_hint("tmux", manager),
    )


def tmux_version_result(
    version: tuple[int, int], path: str, manager: str | None
) -> CheckResult:
    """The result for one parsed tmux version, against the floor."""
    label = ".".join(map(str, version))
    if version < TMUX_MIN_VERSION:
        return CheckResult(
            name="tmux",
            ok=False,
            message=(
                f"tmux {label} < 3.2 — the consent terminal needs "
                "display-popup"
            ),
            hint=install_hint("tmux", manager),
        )
    return CheckResult(
        name="tmux", ok=True, message=f"tmux {label} ok ({path})"
    )


def check_tmux(manager: str | None) -> CheckResult:
    """Check the host tmux meets the 3.2 floor the consent
    terminal documents (#379).

    ``display-popup`` landed in 3.2 and the launch chain's
    bind-time-validated commands stay inside the 3.2 grammar, so
    an older or unparseable tmux is an error — the consent flow
    cannot launch its terminal on it.
    """
    path = probe_which("tmux")
    if not path:
        return CheckResult(
            name="tmux",
            ok=False,
            message="tmux not found on PATH",
            hint=install_hint("tmux", manager),
        )
    # Same as ssh: exec the resolved path (#601).
    rc, out, _err = run([path, "-V"])
    version = parse_tmux_version(out) if rc == 0 else None
    if version is None:
        return tmux_unparseable(out, manager)
    return tmux_version_result(version, path, manager)


# The definitive capability test for the two libraries: the
# vendored ctypes binding (#542) dlopens libnetfilter_queue by
# soname — the same load msks.net.nfqbind performs at import — so
# it exercises exactly what the consent path loads at runtime.
NFQ_LOAD_PROBE = [
    sys.executable,
    "-c",
    "import ctypes\n"
    "for soname in ('libnetfilter_queue.so.1', "
    "'libnetfilter_queue.so'):\n"
    " try:\n"
    "  ctypes.CDLL(soname); break\n"
    " except OSError:\n"
    "  pass\n"
    "else:\n"
    " raise SystemExit(1)",
]


def check_library(name: str, manager: str | None) -> CheckResult:
    """Check a shared library is loadable, through layered probers.

    pkg-config first (it answers when the -dev package
    ships the .pc file), then ``ldconfig -p`` (the runtime soname
    cache, present without any -dev package), then the ctypes
    soname load itself — which also covers hosts whose linker
    database lists nothing (the daemon's environment carries the
    store paths). Each layer that answers "present" passes; all
    three failing is the error.
    """
    via = library_probe_result(name)
    if via is not None:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({via})")
    return CheckResult(
        name=name,
        ok=False,
        message=(
            f"{name} not found via pkg-config, ldconfig, or "
            "the ctypes soname load"
        ),
        hint=install_hint(name, manager),
    )


def pkg_config_evidence(name: str) -> str | None:
    """ "pkg-config" when the -dev package ships the .pc file."""
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        return None
    rc, _out, _err = run([pkg_config, "--exists", name])
    return "pkg-config" if rc == 0 else None


def ldconfig_evidence(name: str) -> str | None:
    """ "ldconfig" when the runtime soname cache lists the library."""
    ldconfig = shutil.which("ldconfig")
    if ldconfig is None:
        return None
    rc, out, _err = run([ldconfig, "-p"])
    if rc == 0 and name in out:
        return "ldconfig"
    return None


def import_evidence() -> str | None:
    """ "ctypes load" when a soname loads."""
    rc, _out, _err = run(NFQ_LOAD_PROBE)
    return "ctypes load" if rc == 0 else None


def library_probe_result(name: str) -> str | None:
    """The evidence string naming how *name* was found, or None."""
    return (
        pkg_config_evidence(name)
        or ldconfig_evidence(name)
        or import_evidence()
    )


def check_nfq_load(manager: str | None) -> CheckResult:
    """Check the consent binding's library loads — the definitive
    capability test: the vendored ctypes binding (#542) dlopens
    the soname the consent path dlopens. Off Linux the library is
    a Linux package, so the miss names the platform contract; on
    Linux a miss means the runtime soname is absent, and the fix
    names the package that carries it."""
    if sys.platform != "linux":
        return CheckResult(
            name="nfq-binding",
            ok=False,
            message=(
                "libnetfilter_queue is a Linux library — the consent "
                "stack does not run on this platform (#542)"
            ),
            hint="run msksd on a Linux host",
        )
    rc, _out, _err = run(NFQ_LOAD_PROBE)
    if rc == 0:
        return CheckResult(
            name="nfq-binding",
            ok=True,
            message="nfq-binding ok (ctypes load)",
        )
    return CheckResult(
        name="nfq-binding",
        ok=False,
        message=(
            "libnetfilter_queue did not load (the probe tries the "
            "versioned and unversioned sonames) — the consent "
            "stack's vendored binding needs it at runtime"
        ),
        hint=install_hint("libnetfilter_queue", manager),
    )
