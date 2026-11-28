#!/usr/bin/env python3
"""Test-assertion gate over the suite (#1219).

Every test under ``src/msks/tests`` checks an outcome. This gate
fails when a test body carries no assertion signal, so a test that
passes when nothing raises cannot land.

A test carries a signal when its body holds an ``assert``, an
``assert*`` or ``fail`` call, or a ``raises``/``warns`` context
manager. It also carries one when a helper it calls holds one, or
when a requested fixture does: calls resolve through imports
(including the bare ``from test_consent_page import wait_for`` form
pytest's ``sys.path`` allows), re-exports, and class hierarchies.
Three weaker signals count as checks too: a poll helper that raises
on timeout (``wait_for``, ``poll``, ``press_until``,
``call_until``, ``asyncio.wait_for``), a helper that raises on a
failed condition (``subprocess.run(check=True)``, ``verify_*``), and
an ``assert`` inside a nested ``scenario()`` coroutine the test runs.
A ``raise`` inside a nested fake is not a check.

A test that is assertion-free by design marks its ``def`` line (or
the comment line above it) with ``# no-assert: <reason>``. A marker
on a test that does carry a signal fails the gate as stale.

Runs at commit time (the generated pre-commit hook) and inside
``msks-preflight``; exit status 1 names each offender with its
file:line and the calls its body makes.
"""

import ast
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
TESTS_PKG = "msks.tests"

NONE, WEAK, DIRECT = 0, 1, 2
CTX_CHECKS = {"raises", "warns", "deprecated_call"}
TIMEOUT_CTX = {"timeout", "fail_after"}
EXPECT_LIKE = (
    "expect",
    "verify",
    "require",
    "assert",
    "must_",
    "should_",
    "check_",
)
STOP_LIKE = {"skip", "xfail", "exit"}
POLL_LIKE = {"wait_for", "poll", "press_until", "call_until"}
SKIP_MARKS = {"skip", "skipif", "xfail"}
MARKER = "no-assert:"
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
NESTED_NODES = (*FUNC_NODES, ast.Lambda)


def dec_target(dec):
    """The attribute or name a decorator references: 'fixture', 'skip'..."""
    d = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(d, ast.Attribute):
        return d.attr
    return getattr(d, "id", None)


def is_mark_holder(expr) -> bool:
    if isinstance(expr, ast.Attribute):
        return expr.attr == "mark"
    return isinstance(expr, ast.Name) and expr.id == "mark"


def mark_name(dec):
    d = dec.func if isinstance(dec, ast.Call) else dec
    if not isinstance(d, ast.Attribute) or d.attr not in SKIP_MARKS:
        return None
    if is_mark_holder(d.value):
        return d.attr
    return None


def skip_marker(node) -> str | None:
    for dec in node.decorator_list:
        found = mark_name(dec)
        if found:
            return found
    return None


def targets_name(node: ast.Assign, name: str) -> bool:
    return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)


def carries_skip(value) -> bool:
    """Whether a pytestmark value (one mark or a list of them) is a skip."""
    marks = getattr(value, "elts", [value])
    return any(mark_name(m) for m in marks)


def module_skip(tree: ast.Module) -> bool:
    """A module-level ``pytestmark`` carrying a skip mark skips every
    test in the module."""
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign) and targets_name(node, "pytestmark")
        ):
            continue
        if carries_skip(node.value):
            return True
    return False


def usefixtures(node) -> list[str]:
    names = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and dec_target(dec) == "usefixtures":
            names += constant_args(dec)
    return names


def constant_args(call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant)]


def var_names(args) -> list[str]:
    out = []
    if args.vararg:
        out.append(args.vararg.arg)
    if args.kwarg:
        out.append(args.kwarg.arg)
    return out


def arg_names(node) -> list[str]:
    args = node.args
    positional = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    names = [x.arg for x in positional] + var_names(args)
    return [n for n in names if n not in ("self", "cls")]


def has_marker(lines, node) -> bool:
    def_line = lines[node.lineno - 1]
    if MARKER in def_line:
        return True
    if node.lineno < 2:
        return False
    above = lines[node.lineno - 2].strip()
    return above.startswith("#") and MARKER in above


def call_name(call) -> str | None:
    fnode = call.func
    if isinstance(fnode, ast.Attribute):
        return fnode.attr
    return getattr(fnode, "id", None)


def is_assert_call(attr) -> bool:
    return attr is not None and (
        attr.startswith("assert") or attr in ("fail", "failIf", "failUnless")
    )


def is_raises_call_form(attr, call) -> bool:
    """``raises(Exc, func, *args)``: the call form checks, no ``with``."""
    return attr in CTX_CHECKS and len(call.args) > 1


def is_asyncio_wait_for(call, attr) -> bool:
    if attr != "wait_for" or not isinstance(call.func, ast.Attribute):
        return False
    return ast.unparse(call.func.value) in ("asyncio", "anyio")


def is_expect_like(attr) -> bool:
    return (
        attr is not None
        and attr.startswith(EXPECT_LIKE)
        and not attr.startswith("assert")
    )


def has_check_true(call) -> bool:
    for k in call.keywords:
        if (
            k.arg == "check"
            and isinstance(k.value, ast.Constant)
            and k.value.value is True
        ):
            return True
    return False


def polled_condition(call) -> str:
    for a in call.args:
        if isinstance(a, ast.Lambda):
            return ast.unparse(a.body)
    return ast.unparse(call)[:100]


def ancestors(module) -> list[str]:
    """The module's packages, nearest first:
    msks.tests.x -> [msks.tests, msks]."""
    out = []
    m = module
    while "." in m:
        m = m.rsplit(".", 1)[0]
        out.append(m)
    return out


def dotted(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class Class:
    key: str
    node: ast.ClassDef
    module: str
    methods: dict = field(default_factory=dict)


@dataclass
class Func:
    module: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    cls: str | None = None
    own: int = NONE
    strength: int = NONE
    deps: set = field(default_factory=set)
    fixture_deps: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    weak_reasons: set = field(default_factory=set)
    polled: list = field(default_factory=list)
    skip_call: bool = False
    nested_assert: bool = False
    skip_marker: str | None = None
    marked: bool = False

    @property
    def key(self) -> str:
        return f"{self.module}::{self.qualname}"

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def line(self) -> int:
        return self.node.lineno

    @property
    def is_test(self) -> bool:
        return self.name.startswith("test_") and self.module.startswith(
            f"{TESTS_PKG}."
        )

    @property
    def is_fixture(self) -> bool:
        return any(
            dec_target(d) == "fixture" for d in self.node.decorator_list
        )


class Index:
    """Every module under src/msks, its functions, classes, and imports."""

    def __init__(self):
        self.funcs = {}
        self.classes = {}
        self.module_defs = {}
        self.module_scope = {}
        self.module_index = set()
        self.conftest_fixtures = defaultdict(dict)
        self.trees = {}
        self.lines = {}
        self.skipped_modules = set()

    def load(self, src_root: Path) -> None:
        paths = [
            p
            for p in sorted(src_root.rglob("*.py"))
            if "__pycache__" not in p.parts
        ]
        for path in paths:
            self.load_module(path, src_root)
        for module, tree in self.trees.items():
            # Scopes resolve against the complete module index: a second pass.
            self.module_scope[module] = build_scope(tree, module, self)

    def load_module(self, path: Path, src_root: Path) -> None:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module = dotted(path, src_root)
        self.module_index.add(module)
        self.trees[module] = tree
        self.lines[module] = source.splitlines()
        if module_skip(tree):
            self.skipped_modules.add(module)
        defs = {}
        for node in tree.body:
            if isinstance(node, FUNC_NODES):
                defs[node.name] = (
                    "func",
                    self.add_func(module, node.name, node),
                )
            elif isinstance(node, ast.ClassDef):
                defs[node.name] = ("class", self.add_class(module, node))
        self.module_defs[module] = defs

    def add_func(self, module, qualname, node, cls=None) -> str:
        f = Func(module=module, qualname=qualname, node=node, cls=cls)
        f.skip_marker = skip_marker(node)
        f.marked = has_marker(self.lines[module], node)
        self.funcs[f.key] = f
        if f.is_fixture and module.endswith(".conftest"):
            self.conftest_fixtures[module[: -len(".conftest")]][f.name] = f.key
        return f.key

    def add_class(self, module, node) -> str:
        ckey = f"{module}::{node.name}"
        cls = Class(key=ckey, node=node, module=module)
        self.classes[ckey] = cls
        for sub in node.body:
            if isinstance(sub, FUNC_NODES):
                qualname = f"{node.name}.{sub.name}"
                cls.methods[sub.name] = self.add_func(
                    module, qualname, sub, cls=ckey
                )
        return ckey

    # --- name resolution ---

    def resolve_dotted(self, full, module):
        """'a.b.c' to a module, function, or class; a bare name is retried
        under each of the importing module's ancestor packages."""
        for cand in [full] + [f"{anc}.{full}" for anc in ancestors(module)]:
            hit = self.dotted_hit(cand)
            if hit:
                return hit
        return None

    def dotted_hit(self, cand):
        if cand in self.module_index:
            return ("module", cand)
        mod, _, attr = cand.rpartition(".")
        local = self.module_defs.get(mod, {}).get(attr)
        if local and local[0] in ("func", "class"):
            return local
        return self.reexport(mod, attr)

    def reexport(self, mod, attr):
        entry = (self.module_scope.get(mod) or {}).get(attr)
        if entry and entry[0] in ("func", "class"):
            return entry
        return None

    def follow(self, entry, module):
        """Resolve an 'unknown' scope entry now that every scope is built."""
        if entry and entry[0] == "unknown":
            return self.resolve_dotted(entry[1], module) or entry
        return entry

    def module_entry(self, target, module):
        hit = self.resolve_dotted(target, module)
        if hit and hit[0] == "module":
            return hit
        return ("module", target)

    def name_entity(self, name, scope, module):
        entry = self.module_defs.get(module, {}).get(name) or self.follow(
            scope.get(name), module
        )
        if entry and entry[0] in ("func", "class"):
            return entry
        return None

    def entry_member(self, entry, parts):
        if not entry:
            return None
        if entry[0] == "module":
            return self.resolve_in_module(entry[1], parts)
        if entry[0] == "class":
            return self.lookup_method(entry[1], parts)
        return None

    def resolve_entity(self, expr, scope, module, cls_key):
        """A Name, Attribute, or Call expression to ('func'|'class', key);
        a Call resolves to its constructor's class (an instance)."""
        if isinstance(expr, ast.Name):
            return self.name_entity(expr.id, scope, module)
        if isinstance(expr, ast.Call):
            return self.resolve_entity(expr.func, scope, module, cls_key)
        if isinstance(expr, ast.Attribute):
            return self.attribute_entity(expr, scope, module, cls_key)
        return None

    def attribute_entity(self, expr, scope, module, cls_key):
        parts, base = split_attribute(expr)
        if isinstance(base, ast.Name):
            return self.name_base(base.id, parts, scope, module, cls_key)
        if isinstance(base, ast.Call):
            return self.instance_base(base, parts, scope, module, cls_key)
        return None

    def name_base(self, name, parts, scope, module, cls_key):
        if name == "self" and cls_key:
            return self.lookup_method(cls_key, parts)
        entry = self.module_defs.get(module, {}).get(name) or self.follow(
            scope.get(name), module
        )
        return self.entry_member(entry, parts)

    def instance_base(self, call, parts, scope, module, cls_key):
        """``X().method()``: a method on an instance of a known class."""
        inner = self.resolve_entity(call.func, scope, module, cls_key)
        if inner and inner[0] == "class":
            return self.lookup_method(inner[1], parts)
        return None

    def resolve_in_module(self, mod, parts):
        cur = mod
        for i, p in enumerate(parts):
            if f"{cur}.{p}" in self.module_index:
                cur = f"{cur}.{p}"
                continue
            return self.member(cur, p, parts[i + 1 :])
        return None

    def member(self, mod, attr, rest):
        if rest:
            d = self.module_defs.get(mod, {}).get(attr)
            return (
                self.lookup_method(d[1], rest)
                if d and d[0] == "class"
                else None
            )
        return self.name_entity(attr, self.module_scope.get(mod) or {}, mod)

    def lookup_method(self, cls_key, parts):
        """parts[0] on a class and its bases, breadth first."""
        seen, queue = set(), deque([cls_key])
        while queue:
            key = queue.popleft()
            hit = self.method_on(key, parts, seen)
            if hit:
                return hit
            if key in self.classes:
                queue.extend(self.base_keys(self.classes[key]))
        return None

    def method_on(self, key, parts, seen):
        if key in seen or key not in self.classes:
            return None
        seen.add(key)
        m = self.classes[key].methods.get(parts[0])
        if m is not None and len(parts) == 1:
            return ("func", m)
        return None

    def base_keys(self, cls: Class) -> list[str]:
        out = []
        for b in cls.node.bases:
            scope = self.module_scope.get(cls.module) or {}
            r = self.resolve_entity(b, scope, cls.module, None)
            if r and r[0] == "class":
                out.append(r[1])
        return out

    def resolve_callee(self, call, scope, module, cls_key):
        """The Func key a call invokes, or None when it cannot be resolved."""
        fnode = call.func
        if isinstance(fnode, ast.Name):
            return self.func_key(self.name_entity(fnode.id, scope, module))
        if isinstance(fnode, ast.Attribute):
            return self.func_key(
                self.resolve_entity(fnode, scope, module, cls_key)
            )
        return None

    def func_key(self, entry):
        return entry[1] if entry and entry[0] == "func" else None

    # --- fixtures ---

    def tests(self) -> list:
        return [f for f in self.funcs.values() if f.is_test]

    def visible_fixture(self, module, name):
        local = self.local_fixture(module, name)
        if local:
            return local
        for pkg in [module, *ancestors(module)]:
            key = self.conftest_fixtures.get(pkg, {}).get(name)
            if key:
                return key
        return None

    def local_fixture(self, module, name):
        local = self.module_defs.get(module, {}).get(name)
        if local and local[0] == "func" and self.funcs[local[1]].is_fixture:
            return local[1]
        return None


def split_attribute(expr):
    parts = [expr.attr]
    v = expr.value
    while isinstance(v, ast.Attribute):
        parts.append(v.attr)
        v = v.value
    parts.reverse()
    return parts, v


def from_base(n, module) -> str:
    base = n.module or ""
    for _ in range(n.level or 0):
        base = (
            f"{ancestors(module)[0]}.{base}" if base else ancestors(module)[0]
        )
    return base


def build_scope(tree, module, ix: Index) -> dict:
    """Local name -> what it refers to: a module, a function, a class,
    or an 'unknown' dotted name (retried at analysis time)."""
    scope = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            import_scope(n, module, ix, scope)
        elif isinstance(n, ast.ImportFrom):
            from_scope(n, module, ix, scope)
    return scope


def import_scope(n, module, ix: Index, scope: dict) -> None:
    for a in n.names:
        top = a.name.split(".")[0]
        target = a.name if a.asname else top
        scope[a.asname or top] = ix.module_entry(target, module)


def from_scope(n, module, ix: Index, scope: dict) -> None:
    base = from_base(n, module)
    for a in n.names:
        full = f"{base}.{a.name}" if base else a.name
        entry = ix.resolve_dotted(full, module) or ("unknown", full)
        scope[a.asname or a.name] = entry


class Analyzer:
    """Fills one function's own signal, its callees, and its fixtures."""

    def __init__(self, ix: Index):
        self.ix = ix

    def run(self, f: Func) -> None:
        self.f = f
        self.scope = self.ix.module_scope.get(f.module) or {}
        if f.module in self.ix.skipped_modules:
            f.skip_marker = f.skip_marker or "module pytestmark"
        body = ast.Module(body=f.node.body, type_ignores=[])
        self.visit(body, False)
        if f.is_test:
            self.find_fixtures(f)

    def visit(self, node, nested) -> None:
        for child in ast.iter_child_nodes(node):
            self.step(child, nested)

    def step(self, child, nested) -> None:
        if isinstance(child, NESTED_NODES):
            self.visit(child, True)
            return
        self.signal(child, nested)
        self.visit(child, nested)

    def signal(self, child, nested) -> None:
        if isinstance(child, ast.Assert):
            self.mark(DIRECT, nested)
        elif isinstance(child, ast.Raise):
            self.raise_signal(nested)
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            self.with_signals(child, nested)
        elif isinstance(child, ast.Call):
            self.call_signals(child, nested)

    def mark(self, level, nested) -> None:
        f = self.f
        if not nested:
            f.own = max(f.own, level)
        elif level == DIRECT:
            f.nested_assert = True
            f.own = max(f.own, WEAK)

    def raise_signal(self, nested) -> None:
        if nested:
            return  # a raise inside a nested fake is behavior, not a check
        self.mark(WEAK, False)
        self.f.weak_reasons.add("raise in test body")

    def with_signals(self, node, nested) -> None:
        for item in node.items:
            expr = item.context_expr
            if not isinstance(expr, ast.Call):
                continue
            name = call_name(expr)
            if name in CTX_CHECKS:
                self.mark(DIRECT, nested)
            elif name in TIMEOUT_CTX:
                self.mark(WEAK, nested)

    def call_signals(self, call, nested) -> None:
        name = ast.unparse(call.func)
        attr = call_name(call)
        self.strong_call(attr, call, nested)
        self.weak_call(attr, call, name, nested)
        self.record_callee(call, name)

    def strong_call(self, attr, call, nested) -> None:
        if is_assert_call(attr):
            self.mark(DIRECT, nested)
        elif is_raises_call_form(attr, call):
            self.mark(DIRECT, nested)
        elif attr in STOP_LIKE:
            self.f.skip_call = True

    def weak_call(self, attr, call, name, nested) -> None:
        f = self.f
        if is_asyncio_wait_for(call, attr):
            self.mark(WEAK, nested)
            f.polled.append(polled_condition(call))
        elif is_expect_like(attr):
            self.mark(WEAK, nested)
            f.weak_reasons.add(f"expect-like call {name}")
        if has_check_true(call):
            self.mark(WEAK, nested)
            f.weak_reasons.add(f"check=True: {name}")

    def record_callee(self, call, name) -> None:
        f = self.f
        callee = self.ix.resolve_callee(call, self.scope, f.module, f.cls)
        if callee:
            f.deps.add(callee)
            if self.ix.funcs[callee].name in POLL_LIKE:
                f.polled.append(polled_condition(call))
        elif name not in f.calls:
            f.calls.append(name)

    def find_fixtures(self, f: Func) -> None:
        for name in arg_names(f.node) + usefixtures(f.node):
            key = self.ix.visible_fixture(f.module, name)
            if key:
                f.fixture_deps.add(key)


def fixed_point(funcs: dict) -> None:
    """A function's strength is its own signal or the strength of
    anything it calls, until nothing changes."""
    changed = True
    while changed:
        changed = False
        for f in funcs.values():
            s = max([f.own, *(funcs[d].strength for d in f.deps)])
            if s > f.strength:
                f.strength = s
                changed = True


def helper_strength(t: Func, funcs: dict) -> int:
    called = (funcs[d].strength for d in t.deps if not funcs[d].is_fixture)
    return max(called, default=NONE)


def all_signals_absent(t: Func) -> bool:
    return t.own == NONE and not t.deps and not t.fixture_deps


def is_skip(t: Func) -> bool:
    if t.skip_marker:
        return True
    return t.skip_call and all_signals_absent(t)


def classify(t: Func, funcs: dict) -> str:
    level = max(t.own, helper_strength(t, funcs))
    if level == DIRECT:
        return "OK"
    if is_skip(t):
        return "SKIP"
    if t.polled:
        return "POLLED"  # a wait_for(lambda: ...) condition is the check
    return "RAISES" if level == WEAK else "NONE"


def graded(tests: list, funcs: dict):
    cats = {t.key: classify(t, funcs) for t in tests}
    return unmarked(tests, cats), stale_markers(tests, cats)


def unmarked(tests: list, cats: dict) -> list:
    return [t for t in tests if cats[t.key] == "NONE" and not t.marked]


def stale_markers(tests: list, cats: dict) -> list:
    return [t for t in tests if t.marked and cats[t.key] != "NONE"]


def relpath(module: str) -> str:
    return f"src/{module.replace('.', '/')}.py"


def print_offenders(offenders: list) -> None:
    for t in sorted(offenders, key=lambda t: (t.module, t.line)):
        calls = ", ".join(t.calls[:6]) + (" ..." if len(t.calls) > 6 else "")
        print(f"{relpath(t.module)}:{t.line} {t.qualname}")
        if calls:
            print(f"    calls: {calls}")


def print_stale(stale: list) -> None:
    for t in sorted(stale, key=lambda t: (t.module, t.line)):
        where = f"{relpath(t.module)}:{t.line} {t.qualname}"
        print(f"stale marker: {where} carries an assertion")


def report(tests: list, offenders: list, stale: list) -> None:
    marked = sum(1 for t in tests if t.marked)
    print(
        f"test-assertions: {len(tests)} tests, {marked} marked '# {MARKER}', "
        f"{len(offenders)} without an assertion"
    )
    print_offenders(offenders)
    print_stale(stale)
    if offenders or stale:
        how = f"add an assertion, or mark the def line '# {MARKER} <reason>'"
        print(f"test-assertions: FAIL - {how}")


def main() -> int:
    ix = Index()
    ix.load(SRC_ROOT)
    analyzer = Analyzer(ix)
    for f in ix.funcs.values():
        analyzer.run(f)
    fixed_point(ix.funcs)
    tests = ix.tests()
    offenders, stale = graded(tests, ix.funcs)
    report(tests, offenders, stale)
    return 1 if offenders or stale else 0


if __name__ == "__main__":
    sys.exit(main())
