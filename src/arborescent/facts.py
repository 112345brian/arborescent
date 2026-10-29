"""One AST pass per file: every import (lazy and type-only ones included), effect uses, PEP 723 metadata.

The result is plain data (``SourceFacts``) so it can be cached by content hash and reused by every check.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
from collections.abc import Collection, Iterable
from dataclasses import dataclass

__all__ = ["OS_BRANCH_ATTRS", "EffectUse", "ModuleInfo", "RawImport", "SourceFacts", "extract", "resolve_targets"]

OS_BRANCH_ATTRS = frozenset({"os.name", "sys.platform", "platform.system"})
_THREAD_POOL_APIS = frozenset({
    "concurrent.futures.ThreadPoolExecutor",
    "concurrent.futures.thread.ThreadPoolExecutor",
    "multiprocessing.pool.ThreadPool",
})
_PROCESS_POOL_APIS = frozenset({
    "concurrent.futures.ProcessPoolExecutor",
    "concurrent.futures.process.ProcessPoolExecutor",
})
_PROCESS_OS_APIS = frozenset({
    "os.system", "os.popen", "os.fork", "os.forkpty", "os.execv", "os.execve", "os.execl",
    "os.execle", "os.execlp", "os.execlpe", "os.execvp", "os.execvpe", "os.spawnl", "os.spawnle",
    "os.spawnlp", "os.spawnlpe", "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    "os.posix_spawn", "os.posix_spawnp", "os.startfile",
})
_PEP723 = re.compile(r"^# /// script\n((?:#.*\n)+?)# ///$", re.MULTILINE)


@dataclass(frozen=True)
class RawImport:
    """``import a.b`` -> (0, "a.b", ()); ``from ..x import y`` -> (2, "x", ("y",))."""

    level: int
    module: str
    names: tuple[str, ...]
    line: int
    lazy: bool  # inside a function body
    type_only: bool  # under ``if TYPE_CHECKING``


@dataclass(frozen=True, order=True)
class EffectUse:
    """A direct use of an effectful API; ``target`` is its canonical dotted name or static patch target."""

    line: int
    category: str
    target: str


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[RawImport, ...]
    effect_uses: tuple[EffectUse, ...]
    script_deps: tuple[str, ...] | None  # PEP 723 `dependencies`, None when the file has no block
    suppressions: tuple[tuple[int, str], ...] = ()  # (line, kind); comments only, not strings
    discard_calls: tuple[int, ...] = ()  # `discard(value, reason=...)`; no type-aware Result claim
    arbor_ring: str | None = None
    arbor_error: str | None = None

    @property
    def os_uses(self) -> tuple[tuple[int, str], ...]:
        """Compatibility view for the original OS-branching rule."""
        return tuple((use.line, use.target) for use in self.effect_uses if use.category == "os-branching")


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str  # posix, relative to the policy root
    is_package: bool
    facts: SourceFacts


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk(node: ast.AST, lazy: bool, type_only: bool, imports: list[RawImport]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            imports += [RawImport(0, a.name, (), child.lineno, lazy, type_only) for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            names = tuple(a.name for a in child.names)
            imports.append(RawImport(child.level, child.module or "", names, child.lineno, lazy, type_only))
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            _walk(child.test, lazy, type_only, imports)
            for stmt in child.body:
                _walk_stmt(stmt, lazy, True, imports)
            for stmt in child.orelse:
                _walk_stmt(stmt, lazy, type_only, imports)
            continue
        _walk(child, lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)), type_only, imports)


def _walk_stmt(stmt: ast.stmt, lazy: bool, type_only: bool, imports: list[RawImport]) -> None:
    holder = ast.Module(body=[stmt], type_ignores=[])
    _walk(holder, lazy, type_only, imports)


def _aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname if item.asname is not None else item.name.split(".")[0]] = item.name if item.asname is not None else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value, aliases)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _effect_category(target: str) -> str | None:
    if target in OS_BRANCH_ATTRS:
        return "os-branching"
    if target in {"os.environ", "os.getenv"}:
        return "env"
    if target in {"time.time", "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today"}:
        return "clock"
    if target in {"uuid.uuid1", "uuid.uuid4"}:
        return "identity"
    if target.startswith(("random.", "secrets.")):
        return "randomness"
    if target in {"time.sleep", "asyncio.sleep"}:
        return "sleep"
    if target.startswith("threading.") or target in _THREAD_POOL_APIS:
        return "threads"
    if target.startswith(("multiprocessing.", "subprocess.", "asyncio.create_subprocess_")) or target in _PROCESS_OS_APIS | _PROCESS_POOL_APIS:
        return "process"
    if target.startswith("importlib.") or target in {"builtins.__import__", "__import__"}:
        return "dynamic-import"
    return None


def _effect_uses(tree: ast.AST) -> tuple[EffectUse, ...]:
    aliases = _aliases(tree)
    uses: set[EffectUse] = set()
    imported_os_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            for item in node.names:
                target = f"{node.module}.{item.name}"
                if target in OS_BRANCH_ATTRS:
                    uses.add(EffectUse(node.lineno, "os-branching", target))
                    imported_os_names.add(item.asname or item.name)
        if isinstance(node, ast.Attribute | ast.Name):
            if isinstance(node, ast.Name) and node.id in imported_os_names:
                continue
            target = _dotted(node, aliases)
            if target is not None:
                category = _effect_category(target)
                if category is not None:
                    uses.add(EffectUse(node.lineno, category, target))
        if not isinstance(node, ast.Call):
            continue
        function = _dotted(node.func, aliases)
        if function is None:
            continue
        is_mock_patch = function in {"unittest.mock.patch", "mock.patch"} or function.endswith(".mock.patch")
        is_monkeypatch = function.endswith(".setattr") and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "monkeypatch"
        if (is_mock_patch or is_monkeypatch) and node.args:
            arg = node.args[0]
            target = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else _dotted(arg, aliases)
            if target is not None:
                uses.add(EffectUse(node.lineno, "patch", target))
    return tuple(sorted(uses))


def _suppression_comments(source: str) -> tuple[tuple[int, str], ...]:
    found: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string[1:].strip()
        ruff = re.match(r"ruff\s*:\s*noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
        noqa = re.match(r"noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
        ignored_type = re.match(r"type\s*:\s*ignore\b(?:\s*\[([^\]]*)\])?", comment, re.IGNORECASE)
        if ruff is not None:
            kind = "ruff-noqa-coded" if (ruff.group(1) or "").strip() else "ruff-noqa-blanket"
        elif noqa is not None:
            kind = "noqa-coded" if (noqa.group(1) or "").strip() else "noqa-blanket"
        elif ignored_type is not None:
            kind = "type-ignore-coded" if (ignored_type.group(1) or "").strip() else "type-ignore-blanket"
        else:
            continue
        found.append((token.start[0], kind))
    return tuple(found)


def _discard_call_lines(tree: ast.AST) -> tuple[int, ...]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_discard = (isinstance(function, ast.Name) and function.id == "discard") or (
            isinstance(function, ast.Attribute) and function.attr == "discard"
        )
        if is_discard and node.args and any(keyword.arg == "reason" for keyword in node.keywords):
            lines.append(node.lineno)
    return tuple(sorted(lines))


def _arbor_ring(tree: ast.Module) -> tuple[str | None, str | None]:
    value: ast.expr | None = None
    found = False
    for stmt in tree.body:
        is_assignment = isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__arbor__" for target in stmt.targets
        )
        is_annotated_assignment = (
            isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "__arbor__"
        )
        if is_assignment or is_annotated_assignment:
            value = stmt.value
            found = True
    if not found:
        return None, None
    try:
        annotation = ast.literal_eval(value) if value is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None, "__arbor__ must be a literal dictionary"
    if not isinstance(annotation, dict):
        return None, "__arbor__ must be a dictionary"
    if "ring" not in annotation:
        return None, '__arbor__ dictionary is missing "ring"'
    ring = annotation["ring"]
    if not isinstance(ring, str):
        return None, '__arbor__."ring" must be a string'
    return ring, None


def _script_deps(source: str) -> tuple[str, ...] | None:
    if "# /// script" not in source:
        return None
    block = _PEP723.search(source.replace("\r\n", "\n"))
    if not block:
        return None
    text = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in block.group(1).splitlines())
    try:
        deps = tomllib.loads(text).get("dependencies", [])
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
        return None
    return tuple(str(re.split(r"[<>=!~\[ ;]", dep, maxsplit=1)[0]) for dep in deps)


def extract(source: str, filename: str = "<source>") -> SourceFacts:
    tree = ast.parse(source, filename=filename)
    imports: list[RawImport] = []
    _walk(tree, False, False, imports)
    arbor_ring, arbor_error = _arbor_ring(tree)
    return SourceFacts(
        tuple(imports),
        _effect_uses(tree),
        _script_deps(source),
        _suppression_comments(source),
        _discard_call_lines(tree),
        arbor_ring,
        arbor_error,
    )


def resolve_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Split one import into (internal module names, external top-level names, error).

    Internal names are dotted modules from ``known``; ``from . import a, b`` yields the submodules ``a`` and ``b``
    when they are modules, and ``from .m import x`` yields ``m``.
    """
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if imp.level:
        if imp.level - 1 > len(pkg):
            return [], [], f"relative import climbs above the source root ({'.' * imp.level}{imp.module})"
        base = pkg[: len(pkg) - (imp.level - 1)] + (imp.module.split(".") if imp.module else [])
    else:
        base = imp.module.split(".")
        if base[0] not in tops:
            return [], [base[0]] if base[0] else [], None
    if not imp.names:  # plain `import a.b.c`
        return [_longest_known(base, known)], [], None
    subs = [".".join([*base, n]) for n in imp.names if n != "*" and ".".join([*base, n]) in known]
    head = [_longest_known(base, known)] if base else []
    return (subs if not imp.module and subs else [*head, *subs]), [], None


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
