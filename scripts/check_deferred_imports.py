#!/usr/bin/env python3
"""Detect non-module-scope (deferred) imports in Python packages.

Imports inside functions, methods, or branches are flagged. Add
``# allow-deferred-import`` to suppress — either on the import line or on
a comment line directly above it (needed when the import is long enough
that a trailing comment would exceed the line-length limit).

Usage:
    check_deferred_imports.py src/msks/msks src/msks/msks/cli
    check_deferred_imports.py src/msks/msks/main.py  # discovers package
    check_deferred_imports.py                        # discovers from cwd
"""

import ast
import os
import sys
from pathlib import Path

# Trees never scanned, in any discovery mode: the virtualenv (its
# site-packages are full of packages whose internals are not ours to
# gate), hidden/build trees, and foreign dependency trees.
SKIP_DIRS = frozenset({"node_modules", "__pycache__"})


def dir_is_skipped(name: str) -> bool:
    """A directory pruned from every walk (hidden, build, vendored)."""
    return name.startswith(".") or name in SKIP_DIRS


def iter_pyfiles(root: Path):
    """Every .py under root, pruned trees excluded (os.walk with
    in-place dir pruning — rglob cannot prune, and would pay for
    walking the venv to throw it away)."""
    for dirpath, dirnames, filenames in os.walk(root):
        prune_dirs(dirnames)
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def prune_dirs(dirnames: list[str]) -> None:
    """Drop pruned trees from an os.walk dir list, in place."""
    dirnames[:] = [d for d in dirnames if not dir_is_skipped(d)]


def is_top_level(node: ast.AST) -> bool:
    """Check if a node is at module scope (parent is ast.Module)."""
    return isinstance(getattr(node, "_parent", None), ast.Module)


def is_type_checking(node: ast.AST) -> bool:
    """Check if a node sits directly inside a module-scope
    ``if TYPE_CHECKING:`` block.

    That block is the canonical module-scope pattern for annotation-only
    imports (erased at runtime), so its imports are exempt without a marker.
    Only at module scope: a function-local ``if TYPE_CHECKING:`` still has a
    runtime ``else`` half whose imports execute per call, so those stay
    gated.
    """
    parent = getattr(node, "_parent", None)
    if not (isinstance(parent, ast.If) and is_top_level(parent)):
        return False
    return is_type_checking_test(parent.test)


def is_type_checking_test(test: ast.AST) -> bool:
    """Is an ``if`` test the ``TYPE_CHECKING`` name (bare or imported)?"""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def catches_import_error(handler: ast.ExceptHandler) -> bool:
    """Does an except clause catch ImportError/ModuleNotFoundError?
    Handles both bare names and tuple handlers."""
    return any(
        name in ("ImportError", "ModuleNotFoundError")
        for name in handled_names(handler)
    )


def handled_names(handler: ast.ExceptHandler) -> list[str]:
    """The exception names a handler lists (bare or tuple)."""
    names = handler.type
    if isinstance(names, ast.Tuple):
        return [e.id for e in names.elts if isinstance(e, ast.Name)]
    if isinstance(names, ast.Name):
        return [names.id]
    return []


def is_guarded_optional(node: ast.AST) -> bool:
    """Check if a node is a module-scope ``try: import`` over ``ImportError``.

    The guarded-optional-dependency pattern (import at module scope inside
    ``try/except ImportError``) is also module-scope in spirit — the import
    executes once at load, with a fallback for absent extras. Both halves
    count: the ``try`` body and the fallback imports inside the matching
    ``except ImportError`` handlers.
    """
    trial = enclosing_try(node)
    if trial is None or not is_top_level(trial):
        return False
    return any(
        isinstance(h, ast.ExceptHandler) and catches_import_error(h)
        for h in trial.handlers
    )


def enclosing_try(node: ast.AST) -> ast.Try | None:
    """The ``try`` a guarded-optional import sits in — directly (try
    body) or one hop up (a fallback import inside an except handler)
    — or None (annotation set in parse_file)."""
    parent = getattr(node, "_parent", None)
    if isinstance(parent, ast.Try):
        return parent
    handler_parent = getattr(parent, "_parent", None)
    if isinstance(parent, ast.ExceptHandler) and isinstance(
        handler_parent, ast.Try
    ):
        return handler_parent
    return None


def parse_file(filepath: Path):
    """Parse a file and annotate parent nodes. Returns (tree, lines),
    or an error string the caller reports loudly — a file the gate's
    own interpreter cannot parse is never silently skipped (the
    xenon-gate rule, #3415: a silent skip turns the gate off for that
    file while everything stays green)."""
    try:
        source_text = filepath.read_text()
        tree = ast.parse(source_text)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        return f"{filepath}: cannot parse ({exc.__class__.__name__}: {exc})"
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node
    return tree, source_text.splitlines()


def line_has_comment(lines: list[str], lineno: int, comment: str) -> bool:
    return comment in lines[lineno - 1] if lineno <= len(lines) else False


def is_marked(lines: list[str], lineno: int, comment: str) -> bool:
    """Is the import at *lineno* suppressed? The marker may sit on the
    import line itself, on a comment line directly above it, or — when
    the import sits inside a nested block (``if``/``try``/…) — on any
    consecutive comment line above it."""
    if line_has_comment(lines, lineno, comment):
        return True
    i = lineno - 2  # 0-based index of the line above
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped.startswith("#"):
            return False
        if comment in stripped:
            return True
        i -= 1
    return False


def is_exempt(node: ast.AST) -> bool:
    """Is the import module-scope in one of the canonical patterns (plain
    top-level, ``if TYPE_CHECKING:``, or a guarded optional dependency)?"""
    return (
        is_top_level(node)
        or is_type_checking(node)
        or is_guarded_optional(node)
    )


def is_flagged_import(node: ast.AST, source_lines: list[str]) -> bool:
    """Is this import node a deferred import that carries no allow marker?"""
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    if is_exempt(node):
        return False
    return not is_marked(source_lines, node.lineno, "allow-deferred-import")


def deferred_import_error(root: Path, pyfile: Path, node) -> str:
    """The error line for one flagged deferred import."""
    rel = pyfile.relative_to(root.parent)
    if isinstance(node, ast.Import):
        names = ", ".join(a.name for a in node.names)
        return f"{rel}:{node.lineno}: deferred import: import {names}"
    module = node.module or ""
    prefix = "." * node.level + module
    names = ", ".join(a.name for a in node.names)
    return (
        f"{rel}:{node.lineno}: deferred import: from {prefix} import {names}"
    )


def file_deferred_errors(root: Path, pyfile: Path) -> list[str]:
    """The flagged deferred imports in one file, in source order
    (ast.walk is breadth-first; sort the nodes back to line order).
    A parse failure is a loud error, not a skip."""
    parsed = parse_file(pyfile)
    if isinstance(parsed, str):
        return [parsed]
    tree, source_lines = parsed
    flagged = sorted(
        (
            node
            for node in ast.walk(tree)
            if is_flagged_import(node, source_lines)
        ),
        key=lambda node: node.lineno,
    )
    return [deferred_import_error(root, pyfile, node) for node in flagged]


def check_deferred_imports(package_dir: str) -> list[str]:
    """Flag imports that are not at module scope.

    Lines with ``# allow-deferred-import`` (on the import line or the
    comment line directly above) are exempted.
    Returns error lines.
    """
    root = Path(package_dir).resolve()
    if not root.is_dir():
        return [f"ERROR: {package_dir} is not a directory"]
    errors: list[str] = []
    for pyfile in sorted(iter_pyfiles(root)):
        errors.extend(file_deferred_errors(root, pyfile))
    return errors


def find_package_root(filepath: Path) -> Path | None:
    """Walk up from a .py file to find the top-level package directory.

    Returns the deepest directory that still has an ``__init__.py`` in
    every ancestor up to the package root, or None if the file isn't
    inside a package.
    """
    parent = filepath.parent
    root = None
    while (parent / "__init__.py").exists():
        root = parent
        parent = parent.parent
    return root


def packages_from_files(files: list[str]) -> list[str]:
    """Derive unique package directories from a list of .py file paths."""
    roots: set[str] = set()
    for f in files:
        p = Path(f).resolve()
        if p.suffix != ".py":
            continue
        pkg = find_package_root(p)
        if pkg is not None:
            roots.add(str(pkg))
    return sorted(roots)


def find_packages_in_dir(directory: Path) -> list[str]:
    """Find all Python packages (dirs with __init__.py) under
    *directory*, pruned trees excluded."""
    roots: set[Path] = set()
    for init in sorted(iter_pyfiles(directory)):
        if init.name != "__init__.py":
            continue
        pkg = find_package_root(init)
        if pkg is not None:
            roots.add(pkg)
    return sorted(str(r) for r in roots)


def resolve_package_dirs(args: list[str]) -> list[str]:
    """The package dirs to scan: discovered under cwd when no args,
    derived from .py file paths, else the args themselves. Mixed args
    are partitioned — a directory arg is never dropped because a file
    arg also appeared."""
    if not args:
        # No arguments: discover packages under cwd
        return find_packages_in_dir(Path.cwd())
    roots: set[str] = set()
    for arg in args:
        if arg.endswith(".py"):
            roots.update(packages_from_files([arg]))
        else:
            roots.add(arg)
    return sorted(roots)


def report_errors(all_errors: list[str]) -> int:
    """Print the error lines; the exit code (1 on any deferred import)."""
    if not all_errors:
        return 0
    for line in all_errors:
        print(line, file=sys.stderr)
    return 1


def main() -> int:
    package_dirs = resolve_package_dirs(sys.argv[1:])
    if not package_dirs:
        return 0
    all_errors: list[str] = []
    for pkg_dir in package_dirs:
        all_errors.extend(check_deferred_imports(pkg_dir))
    return report_errors(all_errors)


if __name__ == "__main__":
    sys.exit(main())
