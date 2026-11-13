"""The module-length gate (``scripts/lines-gate.sh``, #863).

Runs the same script the commit-time hook and ``msks-preflight``
run, against throwaway trees, and pins the boundary: 2,000 lines
passes, 2,001 fails, the exemption list admits an over-ceiling
file, a stale entry fails, and a nested module grades with the
top-level ones. The seeded list is empty and the real tree passes
the gate, so the suite fails if a bypassed commit lets a module
cross the ceiling.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "lines-gate.sh"
SEEDED = SCRIPT.parent / "lines-exemptions.txt"


def make_tree(tmp_path, modules, ignore=""):
    """A throwaway repo with the named modules at the given line
    counts: the gate's scanned set is git-scoped, so each tree
    starts as a repo with untracked-but-present files. ``ignore``
    becomes a .gitignore, pinning that the gate's set follows
    git's exclusion rules."""
    root = tmp_path / "tree"
    root.mkdir()
    for rel, count in modules.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * count)
    if ignore:
        (root / ".gitignore").write_text(ignore)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def run_gate(root, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), str(root), *args],
        capture_output=True,
        text=True,
    )


def test_ceiling_passes_at_1500(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 1500})
    assert run_gate(root).returncode == 0


def test_ceiling_fails_at_1501(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 1501})
    done = run_gate(root)
    assert done.returncode == 1
    assert "src/msks/msks/big.py" in done.stderr
    assert "1501" in done.stderr


def test_nested_module_grades_with_the_top_level(tmp_path):
    root = make_tree(
        tmp_path,
        {"src/msks/msks/client/deep.py": 1501},
    )
    assert run_gate(root).returncode == 1


def test_exempted_over_ceiling_file_passes(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 1501})
    exempt = tmp_path / "exemptions.txt"
    exempt.write_text(
        "# a recorded, dated exception\n"
        "src/msks/msks/big.py split pending #863\n"
    )
    assert run_gate(root, "--exemptions", str(exempt)).returncode == 0


def test_stale_exemption_fails(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 1500})
    exempt = tmp_path / "exemptions.txt"
    exempt.write_text("src/msks/msks/big.py split pending #863\n")
    done = run_gate(root, "--exemptions", str(exempt))
    assert done.returncode == 1
    assert "stale exemption" in done.stderr


def test_exemption_for_absent_file_fails(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/small.py": 10})
    exempt = tmp_path / "exemptions.txt"
    exempt.write_text("src/msks/msks/gone.py split pending #863\n")
    done = run_gate(root, "--exemptions", str(exempt))
    assert done.returncode == 1
    assert "stale exemption" in done.stderr


def test_unterminated_final_line_counts(tmp_path):
    """A 1,501-line file whose last line lacks its newline still
    trips the gate: counting is ``grep -c ''``, not ``wc -l``."""
    root = tmp_path / "tree"
    (root / "src/msks/msks").mkdir(parents=True)
    (root / "src/msks/msks/big.py").write_text("x = 1\n" * 1500 + "x = 1")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    assert run_gate(root).returncode == 1


def test_gitignored_module_stays_out(tmp_path):
    """The scanned set is git-scoped: a gitignored over-ceiling
    module grades never, and dropping --exclude-standard from the
    gate would flip this pin."""
    root = make_tree(
        tmp_path,
        {
            "src/msks/msks/ignored.py": 2500,
            "src/msks/msks/graded.py": 10,
        },
        ignore="src/msks/msks/ignored.py\n",
    )
    assert run_gate(root).returncode == 0


def test_every_offender_reports(tmp_path):
    root = make_tree(
        tmp_path,
        {
            "src/msks/msks/one.py": 2100,
            "src/msks/msks/two.py": 2200,
        },
    )
    done = run_gate(root)
    assert done.returncode == 1
    assert "src/msks/msks/one.py" in done.stderr
    assert "src/msks/msks/two.py" in done.stderr


def test_spaced_path_never_matches_an_entry(tmp_path):
    """The header's invariant, pinned: an over-ceiling path with a
    space never matches an entry (entries are single tokens), so
    the offender reports with its full path and the colliding
    entry fails the stale pass — two complaints, never silence."""
    root = tmp_path / "tree"
    (root / "src/msks/msks").mkdir(parents=True)
    (root / "src/msks/msks/my file.py").write_text("x = 1\n" * 2500)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    exempt = tmp_path / "exemptions.txt"
    exempt.write_text("src/msks/msks/my other.py reason\n")
    done = run_gate(root, "--exemptions", str(exempt))
    assert done.returncode == 1
    assert "src/msks/msks/my file.py" in done.stderr
    assert "2500" in done.stderr
    assert "stale exemption" in done.stderr


def test_seeded_exemption_list_is_empty():
    entries = [
        line
        for line in SEEDED.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries == []


def test_the_tree_passes_the_gate():
    repo_root = Path(__file__).resolve().parents[3]
    done = subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo_root, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
