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

__all__ = [
    "DEV_TOOL_ROLE", "OS_BRANCH_ATTRS", "EffectUse", "ModuleInfo", "RawImport", "SourceFacts", "TopLevelDef",
    "effect_category_of", "extract", "resolve_runtime_targets", "resolve_targets",
]

OS_BRANCH_ATTRS = frozenset({"os.name", "sys.platform", "platform.system"})
_ABSTRACT_BASE_NAMES = frozenset({"ABC", "abc.ABC", "Protocol", "typing.Protocol", "typing_extensions.Protocol"})
_ABC_META_NAMES = frozenset({"ABCMeta", "abc.ABCMeta"})
_DATABASE_PACKAGES = ("sqlite3.", "psycopg.", "psycopg2.", "asyncpg.", "pymysql.", "mysql.connector.", "sqlalchemy.", "pymongo.", "redis.", "aiosqlite.")
# Real network I/O only: `urllib.parse` and `http.HTTPStatus` are not (#129).
_NETWORK_PREFIXES = ("urllib.request.", "http.client.", "http.server.", "socket.")
# Third-party clients are matched by explicit call name, not prefix, so a local variable that shadows an
# unimported library name (`requests = [1]; requests.append(2)`) is not network use.
_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")
_NETWORK_APIS = frozenset(
    {"urllib.request.urlopen", "requests.Session", "httpx.Client", "httpx.AsyncClient", "aiohttp.ClientSession", "urllib3.PoolManager"}
    | {f"{lib}.{verb}" for lib in ("requests", "httpx") for verb in _HTTP_VERBS}
)
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
    string_literals: tuple[tuple[int, str], ...] = ()  # (line, value) of identifier-like string constants, docstrings excluded (#112)
    deferred: tuple[tuple[int, str], ...] = ()  # (code line, reason): lines justified by a `# deferred: <reason>` comment (#149)

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
    if target in _NETWORK_APIS or target.startswith(_NETWORK_PREFIXES):
        return "network"
    return None


_EXACT_EFFECTS: dict[str, str] = {
    target: category
    for category, targets in (
        ("env", ("os.environ", "os.getenv")),
        ("clock", (
            "time.time", "time.gmtime", "time.localtime", "time.strftime",
            "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today",
        )),
        ("identity", ("uuid.uuid1", "uuid.uuid4")),
        ("sleep", ("time.sleep", "asyncio.sleep")),
        ("console", ("print", "input", "sys.stdout.write", "sys.stderr.write")),
    )
    for target in targets
}


def _basic_category(target: str) -> str | None:
    if target in OS_BRANCH_ATTRS:
        return "os-branching"
    if target.startswith(("random.", "secrets.")):
        return "randomness"
    return _EXACT_EFFECTS.get(target)


def _effect_category(target: str) -> str | None:
    return _basic_category(target) or _storage_category(target) or _concurrency_category(target) or _import_category(target)


def effect_category_of(target: str) -> str | None:
    """Public form of :func:`_effect_category`: the effect category a canonical dotted call target belongs to, if any.

    Used by ``taint`` (#98) to seed its I/O-primitive list from the same table ``effect-*`` and ``effect-via-import``
    (#103) already use, so a call to ``print`` or ``open`` is recognised consistently everywhere.
    """
    return _effect_category(target)


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
        return _is_seam_string(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_seam_annotation(node.left) or _is_seam_annotation(node.right)
    if isinstance(node, ast.Subscript):
        return _is_seam_subscript(node)
    if isinstance(node, ast.Attribute):
        return node.attr in _SEAM_ANNOTATIONS
    return isinstance(node, ast.Name) and node.id in _SEAM_ANNOTATIONS


def _is_seam_string(text: str) -> bool:
    try:
        return _is_seam_annotation(ast.parse(text, mode="eval").body)
    except SyntaxError:
        return False


def _is_seam_subscript(node: ast.Subscript) -> bool:
    base = node.value
    name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
    if name in {"Optional", "Union", "Annotated"}:
        inner = node.slice
        return any(_is_seam_annotation(item) for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
    return _is_seam_annotation(base)


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


def _os_branch_imports(node: ast.ImportFrom) -> list[tuple[EffectUse, str]]:
    """``from sys import platform``: each OS-branching name an import binds, with the local name it is bound to."""
    if node.level != 0 or node.module is None:
        return []
    return [
        (EffectUse(node.lineno, "os-branching", f"{node.module}.{item.name}"), item.asname or item.name)
        for item in node.names
        if f"{node.module}.{item.name}" in OS_BRANCH_ATTRS
    ]


def _reference_use(node: ast.Attribute | ast.Name, aliases: dict[str, str], seams: dict[int, int]) -> EffectUse | None:
    """A name or attribute chain that resolves to an effectful target, with whether it sits in an injection default."""
    target = _dotted(node, aliases)
    category = _effect_category(target) if target is not None else None
    if target is None or category is None:
        return None
    seam = seams.get(id(node), 0) if category in _INJECTABLE_EFFECTS else 0
    return EffectUse(node.lineno, category, target, seam)


_SCOPE_BOUNDARIES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _own_scope_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[ast.AST]:
    """Every node directly in ``func``'s own scope: its body, but not a nested function/lambda/class/comprehension's
    (each of those is its own scope, so a ``global`` declared in ``func`` cannot reach an assignment inside one)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_BOUNDARIES):
            stack.extend(ast.iter_child_nodes(node))


def _global_effect_uses(tree: ast.AST) -> list[EffectUse]:
    """A function that both declares ``global x`` and assigns ``x`` (plain, augmented or in a ``for``/``with``
    target) mutates module state from inside a call; one that only reads a global is not a use."""
    uses: list[EffectUse] = []
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        declared: set[str] = set()
        assigned: dict[str, int] = {}
        for node in _own_scope_nodes(func):
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assigned.setdefault(node.id, node.lineno)
        uses.extend(EffectUse(assigned[name], "global", f"{func.name}.{name}") for name in sorted(declared & assigned.keys()))
    return uses


def _effect_uses(tree: ast.AST) -> tuple[EffectUse, ...]:
    aliases = _aliases(tree)
    seams = _injection_defaults(tree)
    rebound = _rebound_names(tree)
    uses: set[EffectUse] = set(_global_effect_uses(tree))
    imported_os_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for use, bound_name in _os_branch_imports(node):
                uses.add(use)
                imported_os_names.add(bound_name)
        found = [_path_io_use(node, aliases), _patch_use(node, aliases)]
        if isinstance(node, ast.Attribute | ast.Name) and not _is_shadowed(node, imported_os_names, rebound):
            found.append(_reference_use(node, aliases, seams))
        uses.update(use for use in found if use is not None)
    return tuple(sorted(uses))


_SHADOWABLE_BUILTINS = frozenset({"open", "print", "input"})


def _is_shadowed(node: ast.Attribute | ast.Name, imported_os_names: set[str], rebound: set[str]) -> bool:
    """A bare name that is an imported OS-branching name (already reported at its import) or a rebound builtin
    (``open``, ``print`` or ``input``) the module defines its own meaning for."""
    return isinstance(node, ast.Name) and (
        node.id in imported_os_names or (node.id in _SHADOWABLE_BUILTINS and node.id in rebound)
    )


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


_DEFERRED = re.compile(r"(?:^|#)\s*deferred\s*:\s*(\S.*)", re.IGNORECASE)
_SKIPPED_TOKENS = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT})


def _suppression_kind(comment: str) -> str | None:
    ruff = re.match(r"ruff\s*:\s*noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
    noqa = re.match(r"noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
    ignored_type = re.match(r"type\s*:\s*ignore\b(?:\s*\[([^\]]*)\])?", comment, re.IGNORECASE)
    if ruff is not None:
        return "ruff-noqa-coded" if (ruff.group(1) or "").strip() else "ruff-noqa-blanket"
    if noqa is not None:
        return "noqa-coded" if (noqa.group(1) or "").strip() else "noqa-blanket"
    if ignored_type is not None:
        return "type-ignore-coded" if (ignored_type.group(1) or "").strip() else "type-ignore-blanket"
    return None


def _comment_facts(source: str) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    """One tokenize pass: suppression directives ``(line, kind)`` and ``# deferred: <reason>`` justifications.

    A deferred comment justifies the code line it trails, or the next code line when it stands alone (comment lines
    that continue it are skipped; a blank line ends it). The reason must not be empty.
    """
    suppressions: list[tuple[int, str]] = []
    deferred: list[tuple[int, str]] = []
    pending: str | None = None
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comment = token.string[1:].strip()
            kind = _suppression_kind(comment)
            if kind is not None:
                suppressions.append((token.start[0], kind))
            reason = _DEFERRED.search(comment)
            if reason is None:
                continue
            if token.line.lstrip().startswith("#"):
                pending = str(reason.group(1)).strip()
            else:
                deferred.append((token.start[0], str(reason.group(1)).strip()))
        elif token.type == tokenize.NL and not token.line.strip():
            pending = None
        elif pending is not None and token.type not in _SKIPPED_TOKENS:
            deferred.append((token.start[0], pending))
            pending = None
    return tuple(suppressions), tuple(deferred)


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


def _arbor_dictionary(found: list[tuple[ast.expr | None, bool]]) -> dict[str, object] | str:
    """The module's ``__arbor__`` literal dictionary, or the error message saying why it is not usable."""
    if any(conditional for _, conditional in found):
        return "__arbor__ must not be assigned inside conditional code"
    value = found[-1][0]
    try:
        annotation = ast.literal_eval(value) if value is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return "__arbor__ must be a literal dictionary"
    if not isinstance(annotation, dict):
        return "__arbor__ must be a dictionary"
    unknown = sorted(set(annotation) - _ARBOR_LEGAL_KEYS)
    if unknown:
        return f"__arbor__ has unknown key(s): {', '.join(map(repr, unknown))}"
    return {str(key): item for key, item in annotation.items()}


def _arbor_fields(annotation: dict[str, object]) -> tuple[str | None, str | None, str | None]:
    if "role" in annotation and annotation.get("role") != DEV_TOOL_ROLE:
        return None, f'__arbor__."role" must be "{DEV_TOOL_ROLE}"', None
    role = DEV_TOOL_ROLE if "role" in annotation else None
    if "ring" not in annotation:
        return None, (None if role is not None else '__arbor__ dictionary is missing "ring"'), role
    ring = annotation["ring"]
    if not isinstance(ring, str):
        return None, '__arbor__."ring" must be a string', None
    return ring, None, role


def _arbor_ring(tree: ast.Module) -> tuple[str | None, str | None, str | None]:
    """``(ring, error, role)`` from the module's ``__arbor__`` assignment."""
    found = _find_arbor_assignments(list(tree.body), False)
    if not found:
        return None, None, None
    annotation = _arbor_dictionary(found)
    if isinstance(annotation, str):
        return None, annotation, None
    return _arbor_fields(annotation)


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
    suppressions, deferred = _comment_facts(source)
    return SourceFacts(
        tuple(imports),
        _effect_uses(tree),
        _script_deps(source),
        suppressions,
        _discard_call_lines(tree),
        _top_level_defs(tree, _aliases(tree)),
        arbor_ring,
        arbor_error,
        arbor_role,
        _file_ref_lines(tree),
        _main_guard_line(tree),
        _string_literals(tree),
        deferred,
    )


_LITERAL_NAME = re.compile(r"[A-Za-z_][\w.\-]{0,79}")
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _string_literals(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    """Identifier-like string constants (an environment variable, a key, a protocol name); a docstring is prose, not a spelling."""
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, _DOCSTRING_OWNERS) and node.body and isinstance(node.body[0], ast.Expr)
    }
    return tuple(sorted({
        (node.lineno, node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        and _LITERAL_NAME.fullmatch(node.value)
    }))


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

    ``from pkg import sub`` is an edge to ``pkg.sub`` alone, as ``from pkg.sub import x`` is; ``pkg`` is added only when some
    imported name is not a submodule (it is read off the package) or is ``*``. That is the question a ring check asks.
    """
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=False), [], None)


def resolve_runtime_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Like ``resolve_targets``, but for what actually runs: importing ``pkg.sub`` also runs ``pkg/__init__``.

    Package splitting must carry that file with the modules that import its submodules, so ``from pkg import sub`` keeps the
    edge to ``pkg`` next to the one to ``pkg.sub``."""
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=True), [], None)


def _import_base(module: str, is_package: bool, imp: RawImport, tops: Collection[str]) -> tuple[list[str] | None, list[str], str | None]:
    """The dotted path an import names, or (None, external names, error) when it is not first-party or cannot be resolved."""
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if imp.level:
        if imp.level - 1 > len(pkg):
            return None, [], f"relative import climbs above the source root ({'.' * imp.level}{imp.module})"
        return pkg[: len(pkg) - (imp.level - 1)] + (imp.module.split(".") if imp.module else []), [], None
    base = imp.module.split(".")
    if base[0] not in tops:
        return None, [base[0]] if base[0] else [], None
    return base, [], None


def _import_edges(base: list[str], imp: RawImport, known: Collection[str], *, runtime_init: bool) -> list[str]:
    if not imp.names:  # plain `import a.b.c`
        return [_longest_known(base, known)]
    subs = [".".join([*base, n]) for n in imp.names if n != "*" and ".".join([*base, n]) in known]
    head = [_longest_known(base, known)] if base else []
    if runtime_init:
        return subs if not imp.module and subs else [*head, *subs]
    all_submodules = bool(subs) and len(subs) == len(imp.names)  # every name is a submodule: nothing is read off the package itself
    return ([] if all_submodules else head) + subs


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
