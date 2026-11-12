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


def make_tree(tmp_path, modules):
    """A throwaway repo with the named modules at the given line
    counts: the gate's scanned set is git-scoped, so each tree
    starts as a repo with untracked-but-present files."""
    root = tmp_path / "tree"
    root.mkdir()
    for rel, count in modules.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * count)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def run_gate(root, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), str(root), *args],
        capture_output=True,
        text=True,
    )


def test_ceiling_passes_at_2000(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 2000})
    assert run_gate(root).returncode == 0


def test_ceiling_fails_at_2001(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 2001})
    done = run_gate(root)
    assert done.returncode == 1
    assert "src/msks/msks/big.py" in done.stderr
    assert "2001" in done.stderr


def test_nested_module_grades_with_the_top_level(tmp_path):
    root = make_tree(
        tmp_path,
        {"src/msks/msks/client/deep.py": 2001},
    )
    assert run_gate(root).returncode == 1


def test_exempted_over_ceiling_file_passes(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 2001})
    exempt = tmp_path / "exemptions.txt"
    exempt.write_text(
        "# a recorded, dated exception\n"
        "src/msks/msks/big.py split pending #863\n"
    )
    assert run_gate(root, "--exemptions", str(exempt)).returncode == 0


def test_stale_exemption_fails(tmp_path):
    root = make_tree(tmp_path, {"src/msks/msks/big.py": 2000})
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
