#!/usr/bin/env python3
"""Import-cycle gate over the daemon sources (#387).

Walks every import under ``src/msks/msks`` with ``ast`` and fails
when the module-level import graph has a cycle — the class of
fragility #387 closed (``model → microvm → consent → model``),
where Python must resolve the loop by partial initialization.

The walk counts an import as a module edge only when it can loop:
a module importing itself or its own ancestor package is the
standard intra-package idiom (the ancestor is already initialized
or initializing when the module runs) and never contributes. The
package-edge whitelist — the layering contract — lives in the
suite (``src/msks/tests/test_layering.py``), which loads this
module for the same walk so the two never drift.

Runs at commit time (the generated pre-commit hook) and inside
``msks-preflight``; exit status 1 names the cycle's chain with the
file:line of each import along it.
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "msks" / "msks"


def module_of(path: Path) -> tuple[str, ...] | None:
    """The dotted module path for a file under PKG_ROOT, or None
    for a file outside the package's __init__.py chains (the
    alembic tree loads by exec, outside the import system)."""
    top = None
    cur = path.parent
    while (cur / "__init__.py").is_file():
        top = cur
        if cur.parent == cur:
            break
        cur = cur.parent
    if top is None:
        return None
    stem = path.relative_to(top).with_suffix("").parts
    if stem[-1] == "__init__":
        stem = stem[:-1]
    return (top.name, *stem)


def package_of(parts: tuple[str, ...]) -> str:
    """The component a module belongs to: the first segment under
    the package root (underscore segments are the root's own)."""
    if len(parts) == 1 or parts[1].startswith("_"):
        return parts[0]
    return parts[1]


def relative_base(
    pkg_parts: tuple[str, ...], node: ast.ImportFrom
) -> tuple[str, ...] | None:
    """The package prefix a relative ImportFrom resolves against,
    or None when it climbs past the package root."""
    if node.level > len(pkg_parts):
        return None
    if node.level:
        return pkg_parts[: len(pkg_parts) - (node.level - 1)]
    return ()


def resolved_module(
    node: ast.ImportFrom, base: tuple[str, ...]
) -> tuple[str, ...]:
    """``base`` plus the statement's dotted module, when it has
    one."""
    return base + (tuple(node.module.split(".")) if node.module else ())


def public_names(node: ast.ImportFrom) -> list[str]:
    """The imported names, star imports excluded."""
    return [a.name for a in node.names if a.name != "*"]


def from_targets(
    node: ast.ImportFrom, base: tuple[str, ...]
) -> list[tuple[str, ...]]:
    """Dotted targets of ``from <module> import names`` resolved
    under ``base``."""
    mod = resolved_module(node, base)
    if not mod:
        return [(n,) for n in public_names(node)]
    return [mod + (n,) for n in public_names(node)]


def import_targets(
    pkg_parts: tuple[str, ...], node: ast.AST
) -> list[tuple[str, ...]]:
    """Dotted absolute targets of one import statement."""
    if isinstance(node, ast.Import):
        return [tuple(a.name.split(".")) for a in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    base = relative_base(pkg_parts, node)
    return from_targets(node, base) if base is not None else []


def statement_imports(pkg_parts: tuple[str, ...], node: ast.AST):
    """Yield ``(lineno, dst_module)`` for one statement's imports
    that resolve inside the msks package."""
    for target in import_targets(pkg_parts, node):
        if target and target[0] == "msks":
            yield node.lineno, target


def file_imports(path: Path, parts: tuple[str, ...]):
    """Yield ``(lineno, dst_module)`` for one file's imports that
    resolve inside the msks package."""
    pkg_parts = parts if path.name == "__init__.py" else parts[:-1]
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        yield from statement_imports(pkg_parts, node)


def intra_package_imports():
    """Yield ``(file, lineno, src_module, dst_module)`` for every
    import under PKG_ROOT that resolves inside the msks package."""
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = module_of(path)
        if parts is None:
            continue
        for lineno, target in file_imports(path, parts):
            yield path, lineno, parts, target


def import_sites() -> dict[tuple[str, str], tuple[str, int]]:
    """Module edges that can loop: ``{(src, dst): (file, line)}``."""
    sites: dict[tuple[str, str], tuple[str, int]] = {}
    for path, lineno, src, dst in intra_package_imports():
        src_name, dst_name = ".".join(src), ".".join(dst)
        if dst_name == src_name or src_name.startswith(dst_name + "."):
            continue
        site = (str(path.relative_to(PKG_ROOT)), lineno)
        sites.setdefault((src_name, dst_name), site)
    return sites


def find_cycle() -> list[str] | None:
    """One import cycle as a module chain (first repeats last), or
    None when the graph is acyclic."""
    sites = import_sites()
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(module: str) -> str | None:
        color[module] = GREY
        stack.append(module)
        for src, dst in sites:
            if src != module:
                continue
            state = color.get(dst, WHITE)
            if state == GREY:
                return dst  # a back-edge: the import stack loops
            if state == WHITE:
                found = visit(dst)
                if found:
                    return found
        color[module] = BLACK
        stack.pop()
        return None

    for module in dict.fromkeys(src for src, _ in sites):
        if color.get(module, WHITE) == WHITE:
            found = visit(module)
            if found:
                return stack[stack.index(found) :] + [found]
    return None


def main() -> int:
    cycle = find_cycle()
    if cycle is None:
        print("check_import_cycles: none under src/msks/msks")
        return 0
    sites = import_sites()
    chain = []
    for src, dst in zip(cycle, cycle[1:]):
        file, line = sites[(src, dst)]
        chain.append(f"{src} -> {dst} ({file}:{line})")
    print(
        "check_import_cycles: import cycle — #387 closed these;\n  "
        + "\n  ".join(chain),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
