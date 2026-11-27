"""Phase 8: the dictation prerequisites (#1216, on macOS).

The GUI's dictate pill records through the PipeWire names
(``pw-record`` capture, ``pw-cat`` tones, ``notify-send``
notification) and transcribes through ``whisper-cli``. A Linux
host holds the tools already; a stock Mac holds none of them,
and PipeWire itself does not exist there. The phase brews the
two real engines the Mac can carry — ``whisper.cpp`` for
``whisper-cli``, ``ffmpeg`` for the capture engine — and lands
three shims at the brew prefix's bin, the client links' own
real estate: ``pw-record`` answering the client's raw-capture
contract over ffmpeg's avfoundation, ``pw-cat`` over ``afplay``,
``notify-send`` over ``osascript``.

Each shim carries :data:`SENTINEL`: a re-run refreshes the
tool's own, a foreign file at a path refuses by name — the run
never clobbers what it did not land — and the uninstall arm
removes only the tool's. The brew packages stay on uninstall:
shared packages, the Linux tools' own rule.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from .base import TOOL, MacSetupError, run
from .links import FALLBACK_BREW_BINS, brewed_prefix

# --- phase 8: the dictation prerequisites (#1216) ----------------------


#: The brew formulas the phase installs and the PATH binary
#: each provides (#1216): whisper.cpp for the transcription
#: engine, ffmpeg for the capture engine the pw-record shim
#: drives. A binary the PATH already answers leaves its formula
#: alone — the cloud-hypervisor precedent (a tool already on
#: PATH stays untouched).
DICTATION_FORMULAS = {"whisper.cpp": "whisper-cli", "ffmpeg": "ffmpeg"}

#: The PipeWire command names the GUI's dictation calls (#1216)
#: — the shims the phase lands at the brew prefix's bin, each
#: carrying :data:`SENTINEL`.
SHIM_NAMES = ("pw-record", "pw-cat", "notify-send")

#: The line every landed shim carries: the phase's own mark, the
#: re-run's refresh cue, and the uninstall sweep's proof.
SENTINEL = "Landed by msks-setup-macos-host"


def missing_engines() -> list[str]:
    """The dictation engines the PATH does not answer (#1216),
    in the formula map's order the install pass names them."""
    return [
        binary
        for binary in DICTATION_FORMULAS.values()
        if shutil.which(binary) is None
    ]


def dictation_bin_dir() -> str:
    """The brew prefix's bin dir — the shims' real estate, the
    client links' own (#719): user-owned, on PATH for every brew
    user. A brew that will not answer refuses by name — the same
    Homebrew line the tool's Lima install already requires."""
    prefix = brewed_prefix()
    if not prefix:
        raise MacSetupError(
            "brew did not answer `brew --prefix`; the dictation "
            "shims need Homebrew (the tool installs Lima through "
            "it)"
        )
    return f"{prefix}/bin"


def shim_body(name: str) -> str:
    """One shim's script, shipped in the wheel beside the Lima
    template the VM phase renders (#671's own asset seam)."""
    return (
        files("msks.server").joinpath(f"assets/dictation/{name}").read_text()
    )


def is_ours(path: Path) -> bool:
    """Whether a standing file carries the tool's sentinel —
    unreadable ground reads as foreign, never as a claim."""
    try:
        return path.exists() and SENTINEL in path.read_text()
    except OSError:
        return False


def install_dictation() -> str:
    """The phase (#1216): brew the engines the PATH does not
    answer, then land the three shims. A brew that fails refuses
    through run's own named tail; a foreign file at a shim path
    refuses by name. Returns the summary line the entry says."""
    missing = missing_engines()
    if missing:
        formulas = [
            formula
            for formula, binary in DICTATION_FORMULAS.items()
            if binary in missing
        ]
        run(["brew", "install", *formulas])
    engines = " and ".join(DICTATION_FORMULAS.values())
    notes = [f"the dictation engines answer ({engines})"]
    notes.extend(land_one_shim(name) for name in SHIM_NAMES)
    return "; ".join(notes)


def land_one_shim(name: str) -> str:
    """One shim's landing: the asset's bytes at the brew
    prefix's bin, mode 0755 — fresh, or refreshed when the tool's
    own sentinel already stands there (a rerun's upgrade
    shape). A foreign file at the path refuses by name, the
    claim arms the client links' own."""
    path = Path(dictation_bin_dir()) / name
    held = path.exists()
    if held and not is_ours(path):
        raise MacSetupError(
            f"{path} already exists and is not this tool's shim; "
            "remove it (or move your own command) and re-run"
        )
    path.write_text(shim_body(name))
    path.chmod(0o755)
    word = "refreshed" if held else "landed"
    return f"{path} {word}"


def dictation_row() -> str:
    """The dictation row (#1216): the engines answering on PATH
    and the shims standing at the brew prefix — each missing
    piece refuses with the re-run remedy, the check rows' law."""
    missing = missing_engines()
    if missing:
        names = " and ".join(missing)
        raise MacSetupError(
            f"{names} not on PATH; re-run {TOOL} to brew the dictation engines"
        )
    bin_dir = dictation_bin_dir()
    for name in SHIM_NAMES:
        path = Path(bin_dir) / name
        if not is_ours(path):
            raise MacSetupError(
                f"the {name} shim at {path} is missing; re-run {TOOL}"
            )
    engines = " and ".join(DICTATION_FORMULAS.values())
    return f"dictation answers: {engines} on PATH, the shims at {bin_dir}"


def remove_dictation_shims() -> str:
    """The uninstall arm's mirror: the tool's shims go whatever
    the engines do, a foreign file at a path stays — the
    uninstall never removes an operator's own file — and the
    brew packages stay with the engines (shared packages, the
    Linux tools' own rule). The arm never requires brew (#701's
    lesson): the prefix answers or the fallback bins sweep."""
    bins = shim_dirs()
    removed = [path for name in SHIM_NAMES for path in sweep_shims(bins, name)]
    return shims_gone(removed)


def shim_dirs() -> list[str]:
    """The directories the uninstall sweeps for shims (#701's
    lesson: the arm never requires brew): the prefix's bin when the
    prefix answers, and the fallback bins always."""
    bins: list[str] = []
    try:
        bins.append(dictation_bin_dir())
    except MacSetupError:
        pass
    bins.extend(FALLBACK_BREW_BINS)
    return bins


def shims_gone(removed: list[str]) -> str:
    """The uninstall arm's report for the shims it swept."""
    if not removed:
        return "no dictation shim found"
    if len(removed) == 1:
        return f"the dictation shim {removed[0]} is gone"
    return f"the dictation shims {', '.join(removed)} are gone"


def sweep_shims(bins: list[str], name: str) -> list[str]:
    """One shim name's sweep over the candidate dirs (#1216): every
    path holding the tool's own sentinel goes — the brew prefix's
    bin and a fallback bin can both hold one after a prefix
    migration — and a path holding nothing of the tool's leaves the
    name unswept there. The paths removed, in sweep order."""
    removed = []
    for bin_dir in dict.fromkeys(bins):
        path = Path(bin_dir) / name
        if is_ours(path):
            path.unlink()
            removed.append(str(path))
    return removed
