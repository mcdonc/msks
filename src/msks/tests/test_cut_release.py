"""The release-cut gate (``scripts/cut-release.sh``, #922).

Runs the same script a release cut runs, against throwaway repos
on ``main`` with a local bare origin, and pins the pre-push
refusals: the tagged tree's pyproject version must equal the tag
(no dev markers — the workflow's wheel gate checks out the tag,
so a bump landing after it releases nothing), and the changelog
section the release notes extract must exist. Every refusal dies
before the tag is pushed, which the fixture pins by asserting the
gate's own words — a script that pushed first would die on the
push, not on the gate.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "cut-release.sh"

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
    False. The script's pre-push gates die inside the fixture;
    the pass path pushes the tag and waits on Actions, which a
    throwaway origin cannot serve."""
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
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
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


def run_cut(root, tag="v0.9.2"):
    """The script over the fixture, its stdout and stderr kept."""
    return subprocess.run(
        ["bash", str(SCRIPT), tag],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_a_version_disagreement_refuses_before_the_push(tmp_path):
    """#922: the tagged tree's pyproject version must equal the
    tag — the refusal names the version it found and the four
    files the bump commit carries, and it dies before the tag is
    pushed."""
    root = make_cut(tmp_path, version="0.9.1")
    done = run_cut(root)
    assert done.returncode == 1
    assert "0.9.1" in done.stderr
    assert "version bump" in done.stderr
    for name in BUMPER_FILES:
        assert name in done.stderr


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
    absent dies locally, its own words naming the miss."""
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
