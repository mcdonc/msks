"""The client/server import boundary (roadmap standing decision #1).

The client package (``msks.client``, when it exists) may not import
server modules — enforced here so the boundary exists before the first
client does. The checker itself is tested against a violating file so
the enforcement is real, not vacuous.
"""

import ast
from pathlib import Path

FORBIDDEN = ("msks.server", "msks.model", "msks.app")
CLIENT_DIR = Path(__file__).resolve().parent.parent / "msks" / "client"


def server_imports(path: Path) -> list[str]:
    """The forbidden server-side imports a module makes, if any."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(
                node.module == m or node.module.startswith(m + ".")
                for m in FORBIDDEN
            ):
                found.append(f"from {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == m or alias.name.startswith(m + ".")
                    for m in FORBIDDEN
                ):
                    found.append(f"import {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module == "msks":
            for alias in node.names:
                if any(f"msks.{alias.name}" == m for m in FORBIDDEN):
                    found.append(f"from msks import {alias.name}")
    return found


def test_checker_catches_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from msks.server.api import build_api\n"
        "import msks.model\n"
        "import msks.server.api\n"
        "from msks import server, model\n"
    )
    assert server_imports(bad) == [
        "from msks.server.api",
        "import msks.model",
        "import msks.server.api",
        "from msks import server",
        "from msks import model",
    ]


def test_checker_allows_clean_client(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("import httpx\nfrom msks.clientlib import thing\n")
    assert server_imports(good) == []


def test_client_package_respects_boundary() -> None:
    if not CLIENT_DIR.is_dir():
        return
    for path in CLIENT_DIR.rglob("*.py"):
        assert server_imports(path) == [], f"{path} imports server modules"
