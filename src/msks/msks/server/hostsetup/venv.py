"""The venv phase: the shared ``/opt/msks`` install (#1118).

The first phase of both setup tools: a venv at ``/opt/msks/venv``
holding the msks release that matches the tool's own version.
Every later phase resolves against it — the system unit's
``ExecStart``, the ``/usr/local/bin`` links, ``msksd_sibling()`` —
so one install serves every account on the host. Stock Debian and
Fedora hold no Python 3.14: uv provisions the standalone
interpreter, parked under ``/opt/msks/python`` beside the venv
(root's home is no place for it — 0700 by default, and the msksd
account must traverse the whole chain). The macOS tool's
host-side install (the brew-prefix links' target) runs this same
phase over its own refusal class: the shared middle lives here,
parameterized by the error and the progress line.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from ...spec.version import __version__
from .profile import DEBIAN_PROFILE, HostProfile
from .util import HostSetupError, say, sh, which

#: The shared install's root: the venv, the provisioned Python,
#: and the bootstrap uv all live under it (#1080's naming), so
#: the uninstall mirror removes one tree.
VENV_ROOT = "/opt/msks"

#: The venv every later phase resolves against.
VENV_DIR = "/opt/msks/venv"

#: The venv's bin dir — the ``/usr/local/bin`` links' target and
#: the unit's ``ExecStart`` resolution.
VENV_BIN = "/opt/msks/venv/bin"

VENV_PYTHON = "/opt/msks/venv/bin/python"

#: What proves the root holds this tool's venv (every venv shape
#: — uv's included — writes it): the uninstall mirror's probe.
VENV_MARKER = "/opt/msks/venv/pyvenv.cfg"

#: The bootstrap uv: a venv holding nothing but uv, built with
#: the tool's own interpreter when the host carries no uv binary
#: (the by-hand flow's shape; the wrapper of #1119 always lands
#: one beside the tool it runs).
UV_VENV = "/opt/msks/uv"
UV_PYTHON = "/opt/msks/uv/bin/python"
UV_BIN = "/opt/msks/uv/bin/uv"

#: Where uv parks the managed interpreters it provisions: beside
#: the venv, on ground every account crosses — not under the
#: installing account's home (a 0700 home breaks the msksd
#: account's exec chain) and not under root's.
PYTHON_INSTALL_DIR = "/opt/msks/python"

#: The interpreter the wheel targets: uv provisions its managed
#: 3.14 (the only-managed preference makes the geometry certain).
PY_VERSION = "3.14"

#: The uv release the phase installs when the host holds no uv —
#: the same pin the macOS VM bootstrap carries (base.py imports
#: it from here; one pin, one tool).
UV_PIN = "0.9.7"

#: The installed-version probe: the venv's own interpreter names
#: the msks it holds (the macOS bootstrap's guard, shared).
VERSION_PROBE = "import importlib.metadata as m; print(m.version('msks'))"

#: One progress line: the phase's *tell* seam (the Linux tools
#: prefix the tool's name, the macOS tool speaks its own shape).
Tell = Callable[[str], None]


def tool_version() -> str:
    """The release this tool's own build carries — the venv
    installs the same version (the macOS tool's ``vm_version``,
    the same seam shape)."""
    return __version__


def uv_env() -> dict[str, str]:
    """The uv variables every uv call carries: managed
    interpreters only, parked under ``/opt/msks/python`` — the
    geometry that survives the throwaway venv the wrapper of
    #1119 runs the tool from."""
    return {
        "UV_PYTHON_INSTALL_DIR": PYTHON_INSTALL_DIR,
        "UV_PYTHON_PREFERENCE": "only-managed",
    }


def run_uv(
    cmd: list[str], error: type[Exception], env: dict[str, str] | None = None
) -> str:
    """One uv (or venv-building) child through the ``sh`` seam,
    its stderr tail the named refusal's cause. The timeout is
    the packages phase's own allowance: these children download
    (uv itself, the managed CPython, the wheel and its
    dependencies), and ``sh``'s 10-second default would kill a
    slow link's install at the first phase."""
    rc, _out, err = sh(cmd, timeout=600.0, env=env)
    if rc != 0:
        raise error(f"{cmd[0]} failed: {err.strip() or 'no output'}")
    return err


def ensure_uv(error: type[Exception], tell: Tell) -> list[str]:
    """The uv the phase drives: PATH's own when the host carries
    one, else a bootstrap venv at ``/opt/msks/uv`` holding the
    pin — the same provisioning the in-VM bootstrap and the
    wrapper of #1119 use. Returns the command's argv head."""
    found = which("uv")
    if found is not None:
        return [found]
    run_uv(
        [sys.executable, "-m", "venv", UV_VENV],
        error,
    )
    run_uv(
        [UV_PYTHON, "-m", "pip", "install", "-q", f"uv=={UV_PIN}"],
        error,
    )
    tell(f"provisioned uv {UV_PIN} at {UV_BIN} (no uv on this host)")
    return [UV_BIN]


def installed_version() -> str | None:
    """The msks version ``/opt/msks/venv`` holds, or None when
    the venv is absent or its interpreter will not answer — the
    create step's cue, and the equal-state probe's answer."""
    rc, out, _err = sh([VENV_PYTHON, "-c", VERSION_PROBE])
    if rc != 0:
        return None
    return out.strip() or None


def python_word() -> str:
    """The venv interpreter's own version word (``Python
    3.14.10``), or ``an unreadable Python`` when it will not
    answer — the summary's word, never a refusal."""
    rc, out, _err = sh([VENV_PYTHON, "--version"])
    word = out.strip()
    if rc != 0 or not word:
        return "an unreadable Python"
    return word


def create_venv(uv: list[str], error: type[Exception], tell: Tell) -> None:
    """``/opt/msks/venv`` through uv: the managed 3.14
    (provisioned on demand into ``/opt/msks/python``), a fresh
    venv over it, and the provisioned word printed. A venv whose
    probe failed is replaced whole — ``--clear`` says so in
    uv's own vocabulary (uv 0.11 refuses a replace without it,
    and the pin does not govern PATH's own uv)."""
    run_uv(
        [*uv, "venv", "--clear", "--python", PY_VERSION, VENV_DIR],
        error,
        env=os.environ | uv_env(),
    )
    tell(f"{VENV_DIR}: {python_word()} (uv-managed, {PYTHON_INSTALL_DIR})")


def install_wheel(
    uv: list[str], wheel: str | None, error: type[Exception]
) -> None:
    """The wheel into the venv: the file ``--wheel`` named, or
    the PyPI release pinned to the tool's own version."""
    spec = os.path.abspath(wheel) if wheel else f"msks=={tool_version()}"
    run_uv(
        [*uv, "pip", "install", "--python", VENV_PYTHON, spec],
        error,
        env=os.environ | uv_env(),
    )


def wheel_version(wheel: str) -> str | None:
    """The version in the wheel's own filename (``msks-0.9.2-py3-
    none-any.whl`` answers ``0.9.2``), or None when the name is
    not the project's wheel shape."""
    parts = Path(wheel).name.removesuffix(".whl").split("-")
    if len(parts) < 2 or parts[0] != "msks":
        return None
    return parts[1]


def refuse_dev_build(wheel: str | None, error: type[Exception]) -> None:
    """A development build has no release to install (#704's
    shape): the PyPI path resolves ``msks==`` the dev number to
    nothing, and falling back to the same-numbered released wheel
    silently installs code that predates the tool running it. The
    refusal names the flag."""
    if wheel is None and ".dev" in tool_version():
        raise error(
            f"this tool is a development build ({tool_version()}); the "
            "venv installs a release from PyPI or a wheel in hand — "
            "build one (uv build) and pass --wheel"
        )


def check_wheel(wheel: str | None, error: type[Exception]) -> None:
    """The ``--wheel`` file's two named refusals: a path that
    holds no file, and a version that disagrees with the tool's
    own — the macOS tool's skew guard, converged on one spelling
    across the three tools."""
    if wheel is None:
        return
    if not Path(wheel).is_file():
        raise error(f"--wheel {wheel}: no such file")
    held = wheel_version(wheel)
    if held != tool_version():
        raise error(
            f"the wheel at {wheel} is msks "
            f"{held or 'a name that carries no version'}, not this "
            f"tool's {tool_version()} — pass --wheel with a build of "
            "this tree"
        )


def verify_installed(error: type[Exception]) -> str:
    """The landing proof: the venv holds the tool's own version
    (a resolution that landed anything else — a same-numbered
    stale release, a wrong wheel — refuses here, before any later
    phase builds on it)."""
    landed = installed_version()
    if landed != tool_version():
        raise error(
            f"the venv holds msks {landed or 'nothing'}, not this "
            f"tool's {tool_version()} — pass --wheel with a build of "
            "this tree"
        )
    return landed


def tell_refresh(held: str, tell: Tell) -> None:
    """The progress word over an existing venv: a named wheel at
    the same version names the overwrite (a rebuild in hand), a
    version change names both words."""
    if held == tool_version():
        tell(f"installing the named wheel over msks {held}")
    else:
        tell(
            f"refreshing the wheel in {VENV_DIR} ({held} -> {tool_version()})"
        )


def refresh_venv(wheel: str | None, error: type[Exception], tell: Tell) -> str:
    """The shared middle of the phase: the equal-state answer
    (the #546 contract — a re-run at the same version runs no uv
    command at all), the create-or-refresh, and the landing
    proof. A named wheel always installs: the operator asserted
    new state at the same version (a rebuild in hand), and the
    equal-state skip is the unnamed release's alone. Returns the
    summary's venv line."""
    held = installed_version()
    if held == tool_version() and wheel is None:
        return f"{VENV_DIR}: already msks {held} ({python_word()})"
    if held is not None:
        tell_refresh(held, tell)
    uv = ensure_uv(error, tell)
    if held is None:
        create_venv(uv, error, tell)
    install_wheel(uv, wheel, error)
    landed = verify_installed(error)
    return f"{VENV_DIR}: {python_word()}, msks {landed}"


def landed_venv(wheel: str | None, error: type[Exception], tell: Tell) -> str:
    """The root and the umask, then the shared middle: 022 makes
    every file uv writes world-readable — under a paranoid umask
    the console scripts would land 0700 and no other account
    could exec them."""
    saved = os.umask(0o022)
    try:
        try:
            os.makedirs(VENV_ROOT, mode=0o755, exist_ok=True)
            os.chmod(VENV_ROOT, 0o755)
        except OSError as exc:
            raise error(f"cannot create {VENV_ROOT} ({exc})") from exc
        return refresh_venv(wheel, error, tell)
    finally:
        os.umask(saved)


def install_venv(
    wheel: str | None, profile: HostProfile = DEBIAN_PROFILE
) -> str:
    """The phase both setup tools run first (#1118): the
    refusals that precede every side effect (a dev build without
    a wheel, a wheel that disagrees), then the landed venv over
    the profile's own progress line. Returns the summary's venv
    line."""

    def tell(message: str) -> None:
        say(profile.tool, message)

    refuse_dev_build(wheel, HostSetupError)
    check_wheel(wheel, HostSetupError)
    return landed_venv(wheel, HostSetupError, tell)


#: The rmtree seam (the file phases' remove_file shape): the
#: suite rigs the failure.
rmtree = shutil.rmtree


def drop_venv(error: type[Exception] = HostSetupError) -> str:
    """The venv phase's mirror: the whole ``/opt/msks`` tree
    goes — the venv, the provisioned Python, the bootstrap uv —
    beside the links and unit the uninstall already mirrors. A
    root that holds no venv is a stranger's (left as-is, named);
    an absent root is already gone. ``/var/lib/msksd`` is not
    this phase's: the kept-state line names it and its size."""
    if not Path(VENV_MARKER).exists():
        if Path(VENV_ROOT).exists():
            return f"{VENV_ROOT} holds no venv (left as-is)"
        return f"{VENV_ROOT} already absent"
    try:
        rmtree(VENV_ROOT)
    except OSError as exc:
        raise error(f"cannot remove {VENV_ROOT} ({exc})") from exc
    return f"{VENV_ROOT} removed (the venv, the provisioned Python, uv)"
