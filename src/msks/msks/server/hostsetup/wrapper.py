from __future__ import annotations

from .util import HostSetupError, remove_file, sh, which

WRAPPER = "/usr/local/bin/msks-caps"

#: Where the client's console scripts link (#719, #755): the
#: /usr/local/bin real estate the wrapper and the VMM already
#: occupy — on every login PATH, so the links need no profile
#: edits and no login-shell gamble over ~/.local/bin.

CAPS = "cap_net_admin,cap_net_bind_service"


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


def drop_wrapper() -> str:
    """The capability wrapper leaves — the caps rode on it."""
    if not remove_file(WRAPPER):
        return f"{WRAPPER} already absent"
    return f"{WRAPPER} removed"
