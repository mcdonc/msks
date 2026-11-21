"""The release-cut gate (``scripts/cut-release.sh``, #922).

Runs the same script a release cut runs, against throwaway repos
on ``main`` with a local bare origin, and pins the refusals and
the one pass-through the fixture can serve: the tagged tree's
pyproject version must equal the tag (no dev markers — the
workflow's wheel gate checks out the tag, so a bump landing after
it releases nothing), the changelog section the release notes
extract must exist, and a cut that passes both gates pushes the
tag and then dies waiting on Actions — a throwaway origin serves
no Deploy Docs run.

The refusals are pinned by the gate's own words, and the pass
path by the Actions-watch die. A regression that pushes before
gating turns the refusal tests into suite timeouts: the default
Actions-lookup budget (thirty two-second sleeps) alone outlasts
the sixty-second per-test ceiling, and the gate's words never
print. A gate regression that refuses a valid cut fails the
pass-path test.
"""

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "cut-release.sh"
NOTES_TOOL = SCRIPT.parent / "release_notes.py"

#: The version-bump commit's file set, the recovery a version
#: disagreement names (the v0.9.1 bump's shape).
BUMPER_FILES = (
    "pyproject.toml",
    "src/msks/msks/spec/version.py",
    "nix/msks-pkg.nix",
    "uv.lock",
)


def make_cut(tmp_path, version="0.9.2", section=True):
    """A throwaway repo cut-ready except where a test differs: on
    ``main``, clean, tagged at HEAD with ``v0.9.2``, its origin a
    local bare repo, pyproject carrying *version*, and a
    changelog whose ``v0.9.2`` section exists unless *section* is
    False. The release-notes tool ships into the fixture — the
    notes gate runs it by path from the repo it stands in."""
    root = tmp_path / "cut"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "msks"\nversion = "{version}"\n'
    )
    changes = "# Changelog\n\n## \\[Unreleased]\n\n"
    if section:
        changes += "\n## \\[v0.9.2] - 2030-01-01\n\n### Added\n\n- one.\n"
    (root / "docs").mkdir()
    (root / "docs" / "changes.md").write_text(changes)
    (root / "scripts").mkdir()
    shutil.copy(NOTES_TOOL, root / "scripts" / "release_notes.py")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "the cut"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=root, check=True
    )
    subprocess.run(["git", "tag", "v0.9.2"], cwd=root, check=True)
    return root


def run_cut(root, tag="v0.9.2", **env):
    """The script over the fixture, its stdout and stderr kept;
    *env* names the script's test knobs (the Actions-lookup
    budget among them). The script's python3 is the interpreter
    the suite itself runs under — its bin dir goes first on PATH,
    so a host whose ambient python3 is older than tomllib (or
    absent) cannot break the gates the fixture means to run
    (#1038)."""
    import os
    import sys

    air = dict(os.environ)
    air["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{air['PATH']}"
    air.update({key: str(value) for key, value in env.items()})
    return subprocess.run(
        ["bash", str(SCRIPT), tag],
        cwd=root,
        capture_output=True,
        text=True,
        env=air,
    )


def test_a_version_disagreement_refuses_before_the_push(tmp_path):
    """#922: the tagged tree's pyproject version must equal the
    tag — the refusal names the version it found, the four files
    the bump commit carries, and the tag surgery a tag already on
    origin needs."""
    root = make_cut(tmp_path, version="0.9.1")
    done = run_cut(root)
    assert done.returncode == 1
    assert "0.9.1" in done.stderr
    assert "version bump" in done.stderr
    for name in BUMPER_FILES:
        assert name in done.stderr
    assert ":refs/tags/v0.9.2" in done.stderr


def test_a_dev_marker_refuses_before_the_push(tmp_path):
    """#922: an in-progress version never releases — a dev marker
    in the tagged pyproject names itself, whatever the tag says."""
    root = make_cut(tmp_path, version="0.9.3.dev0")
    done = run_cut(root)
    assert done.returncode == 1
    assert "dev marker" in done.stderr
    assert "0.9.3.dev0" in done.stderr


def test_a_missing_changelog_section_refuses_before_the_push(tmp_path):
    """The notes gate (#564): a tag whose changelog section is
    absent dies locally through the real matcher — the fixture
    ships the tool, so the refusal is the section's absence, not
    a script the gate cannot find."""
    root = make_cut(tmp_path, section=False)
    done = run_cut(root)
    assert done.returncode == 1
    assert "no release-notes section" in done.stderr


def test_the_version_gate_fires_before_the_notes_gate(tmp_path):
    """Both gates wrong at once read as the version's refusal —
    the wheel gate's order, the bump before the notes."""
    root = make_cut(tmp_path, version="0.9.1", section=False)
    done = run_cut(root)
    assert done.returncode == 1
    assert "version bump" in done.stderr
    assert "no release-notes section" not in done.stderr


def test_a_valid_cut_pushes_then_dies_at_the_watch(tmp_path):
    """The pass-through: both gates green, the tag lands on the
    fixture's origin, and the script dies waiting for the Deploy
    Docs run a throwaway origin cannot register — the lookup's
    own budget shrunk to seconds by its test knob."""
    root = make_cut(tmp_path)
    done = run_cut(root, CUT_RELEASE_TRIES=2, CUT_RELEASE_POLL_S=1)
    assert done.returncode == 1
    assert "pushing v0.9.2" in done.stdout
    assert "no Deploy Docs run appeared for v0.9.2" in done.stderr
    pushed = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "refs/tags/v0.9.2"],
        cwd=tmp_path / "origin.git",
        capture_output=True,
        text=True,
    )
    assert pushed.returncode == 0
