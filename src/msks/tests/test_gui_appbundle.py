"""The GUI's application bundle (#1048): the msks.app a macOS
user launches from the Dock and Launchpad — the plist that
names the client, the wrapper whose interpreter symlink keeps
the process inside the bundle, and the icon build over the
renders the wheel ships. Pure-builders and a planted tree; the
icon seam runs through a fake, so both arms run on every host."""

import plistlib
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from msks.client.gui import appbundle, shortcut


class FakeRunner:
    """The iconutil stand-in: every call answers the recorded
    code, and a green one plants the icns the real tool would."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.returncode == 0:
            icns = Path(command[-1])
            icns.write_bytes(b"fake icns")
        return subprocess.CompletedProcess(command, self.returncode)


def test_the_python_path_carries_both_roots() -> None:
    """The wrapper's baked import path (#1053 review): the
    package's own root first (an editable install keeps the
    package there, and a PYTHONPATH entry runs no ``.pth``
    hooks), the interpreter's site-packages second — and the
    two collapse to one word when a wheel install makes them
    the same directory."""
    import sysconfig

    path = appbundle.python_path()
    site = sysconfig.get_paths()["purelib"]
    assert site in path
    root = str(Path(appbundle.__file__).resolve().parents[3])
    assert path.startswith(root)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        appbundle.sysconfig,
        "get_paths",
        lambda *args, **kwargs: {"purelib": root},
    )
    try:
        assert appbundle.python_path() == root
    finally:
        monkey.undo()


def test_the_plist_names_the_client() -> None:
    """The bundle's own naming (#1048): the name pair the menu
    bar, Dock, and switcher read, the wrapper and icon the
    bundle ships, the version word the About dialog carries,
    and the identifier Launchpad keys the bundle by."""
    for icon in ("msks.icns", "msks.png"):
        plist = appbundle.info_plist(icon)
        assert plist["CFBundleName"] == "msks"
        assert plist["CFBundleDisplayName"] == "msks"
        assert plist["CFBundleIdentifier"] == appbundle.IDENTIFIER
        assert plist["CFBundleExecutable"] == appbundle.EXEC_NAME
        assert plist["CFBundleIconFile"] == icon
        assert plist["CFBundleVersion"]
        assert plist["NSHighResolutionCapable"] is True


def test_the_wrapper_quotes_whatever_the_path_holds() -> None:
    """The launch line (#1048): the interpreter beside the
    wrapper carries the bundle's own path, the install's
    site-packages ride along as a baked PYTHONPATH (the
    symlinked interpreter resolves its prefix from the base
    build, so the venv never loads without it — #1053 review),
    and the script it runs rides one single-quoted word: a
    path with spaces or an apostrophe arrives whole, and
    ``gui`` rides with the caller's own arguments."""
    plain = appbundle.wrapper_text(
        Path("/venv/bin/msks"), "/venv/lib/site-packages"
    )
    assert plain == (
        "#!/bin/sh\n"
        "export PYTHONPATH='/venv/lib/site-packages'\n"
        'exec "$(dirname "$0")/python" '
        "'/venv/bin/msks' gui \"$@\"\n"
    )
    messy = appbundle.wrapper_text(
        Path("/My Ops/chrism's bin/msks"), "/My Ops/site packages"
    )
    assert "\\'" in messy  # the apostrophe closes, escapes, reopens
    assert 'gui "$@"' in messy


def test_the_iconset_pairs_the_renders() -> None:
    """Every iconset slot (#1048) maps a render the wheel
    ships: the hicolor tree carries each size the icns build
    asks for, doubled Retina slots included."""
    for name, size in appbundle.ICONSET_SIZES:
        render = shortcut.icon_dir() / "hicolor" / size / "apps" / "msks.png"
        assert render.is_file(), f"{name} needs the {size} render"


def test_the_icon_takes_the_icns_the_platform_builds(tmp_path: Path) -> None:
    """A green iconutil answers the built icns (#1048): the
    plist points at it, and the tool sees the iconset dir in
    its argv."""
    runner = FakeRunner(0)
    assert appbundle.write_icon(tmp_path, runner) == "msks.icns"
    assert (tmp_path / "msks.icns").read_bytes() == b"fake icns"
    assert runner.calls[0][:3] == ["iconutil", "-c", "icns"]


def test_the_icon_falls_back_to_the_png_it_ships(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An iconutil that answers red, is missing, or dies
    (#1048) leaves the bundle iconful anyway: the largest
    shipped render lands beside the wrapper, the plist points
    at that, and the downgrade names itself on stderr."""
    red = FakeRunner(1)
    assert appbundle.write_icon(tmp_path, red) == "msks.png"
    assert (tmp_path / "msks.png").is_file()
    assert not (tmp_path / "msks.icns").exists()
    assert red.calls[0][:3] == ["iconutil", "-c", "icns"]
    assert "iconutil did not answer" in capsys.readouterr().err

    def absent(command, **kwargs):
        raise FileNotFoundError("no iconutil")

    tmp_path.joinpath("msks.png").unlink()
    assert appbundle.write_icon(tmp_path, absent) == "msks.png"
    assert (tmp_path / "msks.png").is_file()
    assert "iconutil is missing" in capsys.readouterr().err


def test_the_bundle_lands_idempotently(tmp_path: Path) -> None:
    """The plant (#1048): the wrapper carries its exec bit, the
    interpreter symlink points at the install that ran it, and
    the plist parses to the client's own naming. A re-run
    overwrites in place and re-points the symlink at a moved
    install's interpreter."""
    python = tmp_path / "venv-a" / "bin" / "python3.14"
    python.parent.mkdir(parents=True)
    python.write_text("")
    script = tmp_path / "venv-a" / "bin" / "msks"
    script.write_text("#!/bin/sh\n")
    apps = tmp_path / "Applications"
    bundle = appbundle.install_bundle(
        apps, python, script, runner=FakeRunner(0)
    )
    exe = bundle / "Contents" / "MacOS" / appbundle.EXEC_NAME
    assert exe.stat().st_mode & stat.S_IXUSR
    wrapper = exe.read_text()
    assert str(script) in wrapper
    # The import half rides baked: the interpreter's own
    # site-packages, and the editable install's source root
    # ahead of it when the two differ.
    assert str(Path(sysconfig.get_paths()["purelib"])) in wrapper
    link = bundle / "Contents" / "MacOS" / appbundle.PY_NAME
    assert link.is_symlink() and link.resolve() == python.resolve()
    plist = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert plist["CFBundleIdentifier"] == appbundle.IDENTIFIER
    assert plist["CFBundleVersion"] == appbundle.numeric_version()
    assert plist["CFBundleShortVersionString"]
    assert plist["CFBundleIconFile"] == "msks.icns"

    moved = tmp_path / "venv-b" / "bin" / "python3.14"
    moved.parent.mkdir(parents=True)
    moved.write_text("")
    # The re-run falls to the PNG arm: the stale icns goes with
    # it, and the plist points at the PNG the arm landed.
    appbundle.install_bundle(apps, moved, script, runner=FakeRunner(1))
    assert link.resolve() == moved.resolve()
    resources = bundle / "Contents" / "Resources"
    assert not (resources / "msks.icns").exists()
    assert (resources / "msks.png").is_file()
    plist = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert plist["CFBundleIconFile"] == "msks.png"


def test_the_run_installs_under_the_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command body (#1048): the bundle lands under the
    home's Applications and the line names its path, whatever
    icon arm the host's own iconutil answers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "msks").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "gui-shortcut")])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert appbundle.run() == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "Applications" / appbundle.BUNDLE_DIR) in out
    resources = (
        tmp_path
        / "Applications"
        / appbundle.BUNDLE_DIR
        / "Contents"
        / "Resources"
    )
    assert (resources / "msks.icns").is_file() or (
        resources / "msks.png"
    ).is_file()


def test_the_run_stands_down_without_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run whose console script sits alone (#1048) hears the
    stand-down line: the wrapper needs the install's own msks
    to launch."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "gui-shortcut")])
    assert appbundle.run() == 1
    assert appbundle.NOT_BESIDE in capsys.readouterr().err


def test_the_run_names_a_home_it_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No home the platform will answer (#1048) names itself in
    the one-line refusal — the shortcut command's own rule."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "msks").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "gui-shortcut")])

    def unresolvable() -> Path:
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", staticmethod(unresolvable))
    assert appbundle.run() == 1
    assert "cannot install the application bundle" in (capsys.readouterr().err)
