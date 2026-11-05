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

__all__ = ["DEV_TOOL_ROLE", "OS_BRANCH_ATTRS", "EffectUse", "ModuleInfo", "RawImport", "SourceFacts", "TopLevelDef", "extract", "resolve_targets"]

OS_BRANCH_ATTRS = frozenset({"os.name", "sys.platform", "platform.system"})
_ABSTRACT_BASE_NAMES = frozenset({"ABC", "abc.ABC", "Protocol", "typing.Protocol", "typing_extensions.Protocol"})
_ABC_META_NAMES = frozenset({"ABCMeta", "abc.ABCMeta"})
_DATABASE_PACKAGES = ("sqlite3.", "psycopg.", "psycopg2.", "asyncpg.", "pymysql.", "mysql.connector.", "sqlalchemy.", "pymongo.", "redis.", "aiosqlite.")
_FILESYSTEM_APIS = frozenset({
    "open", "io.open", "os.open", "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs", "os.rmdir",
    "os.removedirs", "os.listdir", "os.scandir", "os.walk", "os.stat", "os.chmod", "os.symlink", "os.truncate", "glob.glob", "glob.iglob",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copyfileobj", "shutil.copymode", "shutil.copystat", "shutil.copytree",
    "shutil.move", "shutil.rmtree", "shutil.make_archive", "shutil.unpack_archive", "shutil.chown",
    "tempfile.NamedTemporaryFile", "tempfile.TemporaryFile", "tempfile.TemporaryDirectory", "tempfile.SpooledTemporaryFile",
    "tempfile.mkstemp", "tempfile.mkdtemp", "tempfile.mktemp",
})
_PATH_IO_METHODS = frozenset({
    "read_text", "write_text", "read_bytes", "write_bytes", "open", "mkdir", "touch", "unlink", "rmdir", "rename", "replace",
    "iterdir", "glob", "rglob", "stat",
})
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
    seam: int = 0  # 0: a use; 1: a parameter default whose annotation is a Callable/Mapping; 2: a parameter default otherwise


@dataclass(frozen=True)
class TopLevelDef:
    """A module-level class or function. ``is_abstract`` is a static shape fact, not a policy decision:

    a class is abstract when it inherits (directly) from ``abc.ABC``/``typing.Protocol`` or declares
    ``metaclass=ABCMeta``; a function is abstract when its body is a stub (``...``, ``pass`` or
    ``raise NotImplementedError``, past an optional docstring).
    """

    name: str
    line: int
    is_class: bool
    is_abstract: bool


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[RawImport, ...]
    effect_uses: tuple[EffectUse, ...]
    script_deps: tuple[str, ...] | None  # PEP 723 `dependencies`, None when the file has no block
    suppressions: tuple[tuple[int, str], ...] = ()  # (line, kind); comments only, not strings
    discard_calls: tuple[int, ...] = ()  # `discard(value, reason=...)`; no type-aware Result claim
    top_level_defs: tuple[TopLevelDef, ...] = ()  # module-level classes and functions only
    arbor_ring: str | None = None
    arbor_error: str | None = None
    arbor_role: str | None = None  # `__arbor__ = {"role": "dev_tool"}`: the module claims to be developer tooling
    file_refs: tuple[int, ...] = ()  # lines that read `__file__`: a location-dependent path breaks when the module moves
    main_guard: int = 0  # line of a top-level `if __name__ == "__main__":`, 0 when there is none: the module is also a program

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


def _storage_category(target: str) -> str | None:
    if target.startswith(_DATABASE_PACKAGES):
        return "database"
    if target in _FILESYSTEM_APIS:
        return "filesystem"
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
    return _storage_category(target) or _concurrency_category(target) or _import_category(target)


def _concurrency_category(target: str) -> str | None:
    if target.startswith("threading.") or target in _THREAD_POOL_APIS:
        return "threads"
    if target.startswith(("multiprocessing.", "subprocess.", "asyncio.create_subprocess_")) or target in _PROCESS_OS_APIS | _PROCESS_POOL_APIS:
        return "process"
    return None


def _import_category(target: str) -> str | None:
    if target.startswith("importlib.") or target in {"builtins.__import__", "__import__"}:
        return "dynamic-import"
    return None


_INJECTABLE_EFFECTS = frozenset({"clock", "env", "identity", "randomness", "sleep", "threads", "process"})


_SEAM_ANNOTATIONS = frozenset({"Callable", "Mapping", "MutableMapping"})


def _is_seam_annotation(node: ast.expr | None) -> bool:
    """Whether a parameter annotation says "a callable or a mapping", looking through Optional/Union/`|` and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _is_seam_annotation(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_seam_annotation(node.left) or _is_seam_annotation(node.right)
    if isinstance(node, ast.Subscript):
        base = node.value
        name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
        if name in {"Optional", "Union", "Annotated"}:
            inner = node.slice
            return any(_is_seam_annotation(item) for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
        return _is_seam_annotation(base)
    if isinstance(node, ast.Attribute):
        return node.attr in _SEAM_ANNOTATIONS
    return isinstance(node, ast.Name) and node.id in _SEAM_ANNOTATIONS


def _injection_defaults(tree: ast.AST) -> dict[int, int]:
    """Node id -> seam kind for every node inside a bare Name/Attribute parameter default: a reference there is the injection seam."""
    seams: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            pairs = [(None, d) for d in [*node.args.defaults, *node.args.kw_defaults]]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            positional = [*node.args.posonlyargs, *node.args.args]
            pairs = [*zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults, strict=True),
                     *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)]
            pairs = [(arg.annotation, default) for arg, default in pairs]
        else:
            continue
        for annotation, default in pairs:
            if isinstance(default, ast.Name | ast.Attribute):
                kind = 1 if _is_seam_annotation(annotation) else 2
                seams.update((id(inner), kind) for inner in ast.walk(default))  # `os.environ.get` also holds `os.environ`
    return seams


def _rebound_names(tree: ast.AST) -> set[str]:
    """Names the module assigns, defines or takes as parameters (imports are resolved by `_aliases`): a bare builtin name among them is not the builtin."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _path_io_use(node: ast.AST, aliases: dict[str, str]) -> EffectUse | None:
    """``Path(...).read_text()`` and friends: the receiver is a call, which `_dotted` cannot name."""
    if not (isinstance(node, ast.Attribute) and node.attr in _PATH_IO_METHODS and isinstance(node.value, ast.Call)):
        return None
    constructor = _dotted(node.value.func, aliases)
    if constructor not in {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath"}:
        return None
    return EffectUse(node.lineno, "filesystem", f"{constructor}.{node.attr}")


def _os_branch_imports(node: ast.AST) -> list[tuple[EffectUse, str]]:
    """``from sys import platform``: each OS-branching name an import binds, with the local name it is bound to."""
    if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None):
        return []
    return [
        (EffectUse(node.lineno, "os-branching", f"{node.module}.{item.name}"), item.asname or item.name)
        for item in node.names
        if f"{node.module}.{item.name}" in OS_BRANCH_ATTRS
    ]


def _effect_uses(tree: ast.AST) -> tuple[EffectUse, ...]:
    aliases = _aliases(tree)
    seams = _injection_defaults(tree)
    rebound = _rebound_names(tree)
    uses: set[EffectUse] = set()
    imported_os_names: set[str] = set()
    for node in ast.walk(tree):
        for use, bound_name in _os_branch_imports(node):
            uses.add(use)
            imported_os_names.add(bound_name)
        if isinstance(node, ast.Attribute | ast.Name):
            if isinstance(node, ast.Name) and (node.id in imported_os_names or (node.id == "open" and "open" in rebound)):
                continue
            target = _dotted(node, aliases)
            if target is not None:
                category = _effect_category(target)
                if category is not None:
                    seam = seams.get(id(node), 0) if category in _INJECTABLE_EFFECTS else 0
                    uses.add(EffectUse(node.lineno, category, target, seam))
        path_use = _path_io_use(node, aliases)
        if path_use is not None:
            uses.add(path_use)
        patch = _patch_use(node, aliases)
        if patch is not None:
            uses.add(patch)
    return tuple(sorted(uses))


def _patch_use(node: ast.AST, aliases: dict[str, str]) -> EffectUse | None:
    """``patch("pkg.mod.name")`` / ``monkeypatch.setattr("pkg.mod.name", ...)``: the static patch target."""
    if not isinstance(node, ast.Call):
        return None
    function = _dotted(node.func, aliases)
    if function is None or not node.args:
        return None
    is_mock_patch = function in {"unittest.mock.patch", "mock.patch"} or function.endswith(".mock.patch")
    is_monkeypatch = function.endswith(".setattr") and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "monkeypatch"
    if not (is_mock_patch or is_monkeypatch):
        return None
    arg = node.args[0]
    target = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else _dotted(arg, aliases)
    return EffectUse(node.lineno, "patch", target) if target is not None else None


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


def _is_stub_statement(stmt: ast.stmt, aliases: dict[str, str]) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        target = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return _dotted(target, aliases) == "NotImplementedError"
    return False


def _is_stub_body(body: list[ast.stmt], aliases: dict[str, str]) -> bool:
    stmts = body[1:] if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else body
    if not stmts:
        return True
    return len(stmts) == 1 and _is_stub_statement(stmts[0], aliases)


def _is_abstract_class(node: ast.ClassDef, aliases: dict[str, str]) -> bool:
    for base in node.bases:
        if _dotted(base, aliases) in _ABSTRACT_BASE_NAMES:
            return True
    for keyword in node.keywords:
        if keyword.arg == "metaclass" and _dotted(keyword.value, aliases) in _ABC_META_NAMES:
            return True
    return False


def _top_level_defs(tree: ast.AST, aliases: dict[str, str]) -> tuple[TopLevelDef, ...]:
    out: list[TopLevelDef] = []
    body = tree.body if isinstance(tree, ast.Module) else []
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            out.append(TopLevelDef(stmt.name, stmt.lineno, True, _is_abstract_class(stmt, aliases)))
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(TopLevelDef(stmt.name, stmt.lineno, False, _is_stub_body(stmt.body, aliases)))
    return tuple(out)


_ARBOR_LEGAL_KEYS = frozenset({"ring", "role"})
DEV_TOOL_ROLE = "dev_tool"
_CONDITIONAL_CONTAINERS = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


def _is_arbor_target(stmt: ast.stmt) -> bool:
    is_assignment = isinstance(stmt, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__arbor__" for target in stmt.targets
    )
    is_annotated_assignment = (
        isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "__arbor__"
    )
    return is_assignment or is_annotated_assignment


def _conditional_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
    if isinstance(stmt, ast.If):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, ast.Try):
        return [stmt.body, stmt.orelse, stmt.finalbody, *(handler.body for handler in stmt.handlers)]
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body]
    return []


def _find_arbor_assignments(stmts: list[ast.stmt], conditional: bool) -> list[tuple[ast.expr | None, bool]]:
    found: list[tuple[ast.expr | None, bool]] = []
    for stmt in stmts:
        if _is_arbor_target(stmt):
            value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
            found.append((value, conditional))
        elif isinstance(stmt, _CONDITIONAL_CONTAINERS):
            for body in _conditional_bodies(stmt):
                found += _find_arbor_assignments(body, True)
    return found


def _arbor_ring(tree: ast.Module) -> tuple[str | None, str | None, str | None]:
    """``(ring, error, role)`` from the module's ``__arbor__`` assignment."""
    found = _find_arbor_assignments(list(tree.body), False)
    if not found:
        return None, None, None
    if any(conditional for _, conditional in found):
        return None, "__arbor__ must not be assigned inside conditional code", None
    value = found[-1][0]
    try:
        annotation = ast.literal_eval(value) if value is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None, "__arbor__ must be a literal dictionary", None
    if not isinstance(annotation, dict):
        return None, "__arbor__ must be a dictionary", None
    unknown = sorted(set(annotation) - _ARBOR_LEGAL_KEYS)
    if unknown:
        return None, f"__arbor__ has unknown key(s): {', '.join(map(repr, unknown))}", None
    declared_role = annotation.get("role")
    if "role" in annotation and declared_role != DEV_TOOL_ROLE:
        return None, f'__arbor__."role" must be "{DEV_TOOL_ROLE}"', None
    role = DEV_TOOL_ROLE if "role" in annotation else None
    if "ring" not in annotation:
        if role is not None:
            return None, None, role
        return None, '__arbor__ dictionary is missing "ring"', None
    ring: object = annotation["ring"]
    if not isinstance(ring, str):
        return None, '__arbor__."ring" must be a string', None
    return ring, None, role


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
    arbor_ring, arbor_error, arbor_role = _arbor_ring(tree)
    return SourceFacts(
        tuple(imports),
        _effect_uses(tree),
        _script_deps(source),
        _suppression_comments(source),
        _discard_call_lines(tree),
        _top_level_defs(tree, _aliases(tree)),
        arbor_ring,
        arbor_error,
        arbor_role,
        _file_ref_lines(tree),
        _main_guard_line(tree),
    )


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(a, ast.Name) and a.id == "__name__" and isinstance(b, ast.Constant) and b.value == "__main__"
               for a, b in (sides, sides[::-1]))


def _main_guard_line(tree: ast.Module) -> int:
    return next((stmt.lineno for stmt in tree.body if isinstance(stmt, ast.If) and _is_main_guard(stmt.test)), 0)


def _file_ref_lines(tree: ast.AST) -> tuple[int, ...]:
    return tuple(sorted(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "__file__"))


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
    all_submodules = bool(subs) and len(subs) == len(imp.names)  # every name is a submodule: nothing is read off the package itself
    head = [_longest_known(base, known)] if base and not all_submodules else []
    return [*head, *subs], [], None


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
