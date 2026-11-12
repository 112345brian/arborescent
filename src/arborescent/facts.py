"""One AST pass per file: every import (lazy and type-only ones included), effect uses, PEP 723 metadata.

The result is plain data (``SourceFacts``) so it can be cached by content hash and reused by every check.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .forwarder_facts import Forwarder, forwarders
from .purity_facts import (
    Helpers,
    PurityFacts,
    PurityUnit,
    local_names,
    module_containers,
    mutated_container,
    purity_facts,
)

__all__ = [
    "DEV_TOOL_ROLE", "OS_BRANCH_ATTRS", "EffectUse", "Forwarder", "FunctionCalls", "ModuleInfo", "PurityFacts", "PurityUnit", "RawImport", "SourceFacts", "TopLevelDef",
    "absolute_callee", "effect_category_of", "extract", "resolve_runtime_targets", "resolve_targets",
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
# Path methods that touch file contents or the directory tree, and ones that only ask the filesystem a question.
_PATH_QUERY_METHODS = frozenset({"exists", "is_file", "is_dir", "is_symlink", "is_mount", "resolve", "absolute", "expanduser", "samefile"})
_PATH_CONSTRUCTORS = frozenset({"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath"})
# Methods that return another path when called on a path, so the result is still path-valued.
_PATH_RETURNING = frozenset({"resolve", "absolute", "expanduser", "with_name", "with_suffix", "with_stem", "joinpath", "relative_to", "readlink"})
_PATH_ATTRIBUTES = frozenset({"parent", "parents"})
_FS_QUERY_APIS = frozenset({
    "os.path.exists", "os.path.lexists", "os.path.isfile", "os.path.isdir", "os.path.islink", "os.path.ismount", "os.path.getsize",
    "os.path.getmtime", "os.path.getatime", "os.path.getctime", "os.path.samefile", "os.path.realpath", "os.path.abspath",
    "os.path.expanduser", "os.getcwd", "os.getcwdb", "os.access", "shutil.which", "shutil.disk_usage", "tempfile.gettempdir",
    "pathlib.Path.cwd", "pathlib.Path.home",
})
_MONOTONIC_APIS = frozenset({
    "time.monotonic", "time.monotonic_ns", "time.perf_counter", "time.perf_counter_ns", "time.process_time", "time.process_time_ns",
    "time.thread_time", "time.thread_time_ns",
})
_HOST_APIS = frozenset({
    "sys.argv", "sys.stdin", "os.getpid", "os.getppid", "os.cpu_count", "os.getlogin", "os.uname", "socket.gethostname", "socket.getfqdn",
    "getpass.getuser",
})
_HOST_PREFIXES = ("locale.", "platform.")
# Synchronisation primitives guard shared state; they neither start threads nor touch the outside world.
_STATE_PRIMITIVES = frozenset({"Lock", "RLock", "Semaphore", "BoundedSemaphore", "Event", "Condition", "Barrier", "local"})
# What loads a module at run time, which is what tach cannot see. `importlib.resources`, `importlib.metadata` and `find_spec` do not.
_DYNAMIC_IMPORT_APIS = frozenset({
    "importlib.import_module", "importlib.__import__", "importlib.reload", "importlib.util.module_from_spec",
    "importlib.util.spec_from_file_location", "importlib.util.spec_from_loader", "builtins.__import__", "__import__",
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
class FunctionCalls:
    """The imported callables one function or method calls, as canonical dotted names (``alias.attr()`` resolved).

    ``name`` is ``f`` or ``Class.method``. Only calls whose root name was imported are kept, so a local variable's
    method call is not recorded; a function that calls nothing imported has no entry. Reused by the test-reference check (#104).
    """

    name: str
    line: int
    calls: tuple[str, ...]  # sorted, unique


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
    forwarders: tuple[Forwarder, ...] = ()  # module-level pass-through functions (#138)
    weak_port_returns: tuple[tuple[int, str, str], ...] = ()  # (line, "Protocol.method", why): a Protocol method whose return type says nothing (#140)
    surface_size: int = 0  # public names: a static `__all__`, else public top-level bindings (#136)
    surface_line: int = 0  # line of `__all__`, else of the first public binding
    reexports: tuple[tuple[str, int, str, str], ...] = ()  # surface names bound by an import: (name, level, module, imported name or "")
    deferred: tuple[tuple[int, str], ...] = ()  # (code line, reason): lines justified by a `# deferred: <reason>` comment (#149)
    calls: tuple[tuple[int, str], ...] = ()  # (line, dotted callee) of calls on an imported or module-level name; a relative import keeps its dots (#85)
    result_defs: tuple[str, ...] = ()  # module-level functions annotated `-> *Result` / `-> *Outcome` (#141)
    bare_calls: tuple[tuple[int, str], ...] = ()  # (line, dotted callee) of a call used as a whole statement; relative targets keep leading dots (#141)
    function_calls: tuple[FunctionCalls, ...] = ()  # module-level functions and class methods that call something imported (#104)
    module_doc: str | None = None  # the module docstring, cleaned; None when there is none (#110)
    arbor_reason: str | None = None  # `__arbor__["ring_reason"]`: why the module sits in a ring a tighter one would also fit (#161)
    dynamic_imports: tuple[tuple[int, str], ...] = ()  # (line, literal module name, or "" when the target is not a plain absolute string literal) (#161)
    purity: PurityFacts = field(default_factory=PurityFacts)  # top-level definition spans plus logging, memo and module-state facts (#173)
    untokenizable: str = ""  # "<line>: <message>" when `ast` accepted the source but `tokenize` did not, so `suppressions` and `deferred` are unknown (#181)

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


def _aliases(nodes: Sequence[ast.AST]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes:
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
            "time.time", "time.time_ns", "time.gmtime", "time.localtime", "time.strftime",
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
    if target.startswith(("random.", "secrets.")) or target == "os.urandom":
        return "randomness"
    return _EXACT_EFFECTS.get(target)


def _query_category(target: str) -> str | None:
    """Questions asked of the machine: the filesystem, the monotonic clock, the process and the host."""
    if target in _FS_QUERY_APIS:
        return "fs-query"
    if target in _MONOTONIC_APIS:
        return "monotonic"
    if target in _HOST_APIS or target.startswith(_HOST_PREFIXES):
        return "host"
    return None


def _effect_category(target: str) -> str | None:
    return (
        _basic_category(target) or _query_category(target) or _storage_category(target)
        or _concurrency_category(target) or _import_category(target)
    )


def effect_category_of(target: str) -> str | None:
    """Public form of :func:`_effect_category`: the effect category a canonical dotted call target belongs to, if any.

    Used by ``taint`` (#98) to seed its I/O-primitive list from the same table ``effect-*`` and ``effect-via-import``
    (#103) already use, so a call to ``print`` or ``open`` is recognised consistently everywhere.
    """
    return _effect_category(target)


def _concurrency_category(target: str) -> str | None:
    if target.startswith("threading."):
        return "state" if target.removeprefix("threading.") in _STATE_PRIMITIVES else "threads"
    if target in _THREAD_POOL_APIS:
        return "threads"
    if target.startswith(("multiprocessing.", "subprocess.", "asyncio.create_subprocess_")) or target in _PROCESS_OS_APIS | _PROCESS_POOL_APIS:
        return "process"
    return None


def _import_category(target: str) -> str | None:
    if target in _DYNAMIC_IMPORT_APIS or target.endswith(".exec_module"):
        return "dynamic-import"
    return None


_INJECTABLE_EFFECTS = frozenset({"clock", "env", "identity", "randomness", "sleep", "threads", "process", "monotonic", "host"})


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


def _injection_defaults(nodes: Sequence[ast.AST]) -> dict[int, int]:
    """Node id -> seam kind for every node inside a bare Name/Attribute parameter default: a reference there is the injection seam."""
    seams: dict[int, int] = {}
    for node in nodes:
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
            held = _seam_expression(default)
            if held is not None:
                kind = 1 if _is_seam_annotation(annotation) else 2
                seams.update((id(inner), kind) for inner in ast.walk(held))  # `os.environ.get` also holds `os.environ`
    return seams


def _seam_expression(default: ast.expr | None) -> ast.expr | None:
    """The part of a parameter default that is the seam: a bare Name/Attribute, or the callee of ``lambda: clock()``.

    A lambda default whose body is a call defers that call until the parameter is used, exactly as a bare reference
    does; the call's own arguments are not part of the seam, so an effect passed to it still counts."""
    if isinstance(default, ast.Name | ast.Attribute):
        return default
    if isinstance(default, ast.Lambda) and isinstance(default.body, ast.Call):
        return default.body.func
    return None


def _rebound_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Names the module assigns, defines or takes as parameters (imports are resolved by `_aliases`): a bare builtin name among them is not the builtin."""
    bound: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _annotation_is_path(node: ast.expr | None, aliases: dict[str, str]) -> bool:
    """Whether an annotation names a ``pathlib`` path, looking through ``Optional``/``Union``/``|`` and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_is_path(ast.parse(node.value, mode="eval").body, aliases)
        except SyntaxError:
            return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_is_path(node.left, aliases) or _annotation_is_path(node.right, aliases)
    if isinstance(node, ast.Subscript):
        base = _dotted(node.value, aliases) or ""
        if base.rsplit(".", 1)[-1] in {"Optional", "Union", "Annotated"}:
            inner = node.slice
            return any(_annotation_is_path(item, aliases) for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
        return False
    return isinstance(node, ast.Name | ast.Attribute) and _dotted(node, aliases) in _PATH_CONSTRUCTORS


def _is_pathish(node: ast.expr, bound: set[str], aliases: dict[str, str]) -> bool:
    """Whether an expression is a path: a ``Path(...)`` call, a name bound from one, ``a / b`` with a path side, ``p.parent`` or ``p.resolve()``."""
    if isinstance(node, ast.Name):
        return node.id in bound
    if isinstance(node, ast.Call):
        callee = _dotted(node.func, aliases)
        if callee in _PATH_CONSTRUCTORS or callee in {"pathlib.Path.cwd", "pathlib.Path.home"}:
            return True
        return isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_RETURNING and _is_pathish(node.func.value, bound, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_pathish(node.left, bound, aliases) or _is_pathish(node.right, bound, aliases)
    return isinstance(node, ast.Attribute) and node.attr in _PATH_ATTRIBUTES and _is_pathish(node.value, bound, aliases)


def _path_binding(node: ast.AST, aliases: dict[str, str]) -> tuple[list[str], ast.expr | None]:
    """(names, value) for a node that may bind a path: a ``Path``-annotated name comes with no value to inspect (``None``)."""
    if isinstance(node, ast.arg):
        return ([node.arg], None) if _annotation_is_path(node.annotation, aliases) else ([], None)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if _annotation_is_path(node.annotation, aliases):
            return [node.target.id], None
        return ([node.target.id], node.value) if node.value is not None else ([], None)
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    return [], None


def _scope_path_names(scope: ast.AST, aliases: dict[str, str]) -> set[str]:
    """Names one scope binds to a path: a parameter or variable annotated ``Path``, or assigned a path expression."""
    bound: set[str] = set()
    assigns: list[tuple[list[str], ast.expr]] = []
    for node in _own_scope_nodes(scope):
        names, value = _path_binding(node, aliases)
        if value is None:
            bound.update(names)
        else:
            assigns.append((names, value))
    for _ in range(3):  # `a = Path(x); b = a / "y"; c = b.parent` settles in a few passes
        before = len(bound)
        for names, value in assigns:
            if names and _is_pathish(value, bound, aliases):
                bound.update(names)
        if len(bound) == before:
            break
    return bound


def _path_receiver_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> list[EffectUse]:
    """``p.read_text()``, ``p.exists()`` on a name this scope bound to a path (a ``Path(...)`` value or a ``Path`` annotation).

    Scope by scope, so a ``str`` named ``path`` in another function is not mistaken for one. A module that never
    imports ``pathlib`` has no path to find and is skipped without a second walk."""
    if not any(target == "pathlib" or target.startswith("pathlib.") for target in aliases.values()):
        return []
    uses: list[EffectUse] = []
    scopes = [tree, *(n for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef))]
    for scope in scopes:
        bound = _scope_path_names(scope, aliases)
        if not bound:
            continue
        for node in _own_scope_nodes(scope):
            if isinstance(node, ast.Attribute) and (node.attr in _PATH_IO_METHODS or node.attr in _PATH_QUERY_METHODS) and _is_pathish(node.value, bound, aliases):
                use = _path_method_use(node, f"pathlib.Path.{node.attr}")
                if use is not None:
                    uses.append(use)
    return uses


def _path_method_use(node: ast.Attribute, target: str) -> EffectUse | None:
    if node.attr in _PATH_IO_METHODS:
        return EffectUse(node.lineno, "filesystem", target)
    if node.attr in _PATH_QUERY_METHODS:
        return EffectUse(node.lineno, "fs-query", target)
    return None


def _path_io_use(node: ast.AST, aliases: dict[str, str]) -> EffectUse | None:
    """``Path(...).read_text()`` and friends: the receiver is a call, which `_dotted` cannot name."""
    if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)):
        return None
    constructor = _dotted(node.value.func, aliases)
    if constructor not in _PATH_CONSTRUCTORS:
        return None
    return _path_method_use(node, f"{constructor}.{node.attr}")


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


def _own_scope_nodes(func: ast.AST) -> Iterable[ast.AST]:
    """Every node directly in ``func``'s own scope: its body, but not a nested function/lambda/class/comprehension's
    (each of those is its own scope, so a ``global`` declared in ``func`` cannot reach an assignment inside one)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_BOUNDARIES):
            stack.extend(ast.iter_child_nodes(node))


def _global_effect_uses(nodes: Sequence[ast.AST]) -> list[EffectUse]:
    """A function that both declares ``global x`` and assigns ``x`` (plain, augmented or in a ``for``/``with``
    target) mutates module state from inside a call; one that only reads a global is not a use."""
    uses: list[EffectUse] = []
    for func in (n for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        declared: set[str] = set()
        assigned: dict[str, int] = {}
        for node in _own_scope_nodes(func):
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assigned.setdefault(node.id, node.lineno)
        uses.extend(EffectUse(assigned[name], "global", f"{func.name}.{name}") for name in sorted(declared & assigned.keys()))
    return uses


_CACHE_DECORATORS = frozenset({"functools.cache", "functools.lru_cache"})  # not `cached_property`: that cache is per instance
_CACHE_NAME = re.compile(r"cache|memo", re.IGNORECASE)


def _module_state_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> list[EffectUse]:
    """#194, module state that is not a ``global`` rebinding. A function that writes a module-level ``dict``/``list``/``set`` (item
    assignment, ``del`` or a mutating method call) is a ``registry`` use, or a ``cache`` use when the container's name says cache or
    memo; a ``functools.cache``/``lru_cache`` decorator is a ``cache`` use. A name the function (or one around it) binds itself is a
    local, not the module's container; import-time fills and reads are not uses. Recorded in every module, failing only where a ring
    lists the category in ``forbid`` (see ``MODULE_STATE_CATEGORIES``)."""
    uses = [
        EffectUse(decorator.lineno, "cache", target)
        for func in nodes if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in func.decorator_list
        if (target := _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator, aliases)) in _CACHE_DECORATORS
    ]
    containers = module_containers(tree, aliases, _dotted) if isinstance(tree, ast.Module) else set()
    if not containers:
        return uses
    stack: list[tuple[ast.AST, tuple[tuple[str, set[str]], ...]]] = [(tree, ())]
    while stack:
        node, scopes = stack.pop()
        for child in ast.iter_child_nodes(node):
            inner = scopes
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scopes, (child.name, local_names(child)))
            elif scopes and (name := mutated_container(child, containers)) is not None and not any(name in bound for _, bound in scopes):
                category = "cache" if _CACHE_NAME.search(name) else "registry"
                uses.append(EffectUse(getattr(child, "lineno", 0), category, f"{scopes[-1][0]}.{name}"))
            stack.append((child, inner))
    return uses


def _annotation_node_ids(nodes: Sequence[ast.AST]) -> set[int]:
    """Ids of every node inside a type annotation (parameter, return, variable): a hint names a type, it does not use it."""
    ids: set[int] = set()
    for node in nodes:
        annotation: ast.expr | None = None
        if isinstance(node, ast.arg | ast.AnnAssign):
            annotation = node.annotation
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotation = node.returns
        if annotation is not None:
            ids.update(id(inner) for inner in ast.walk(annotation))
    return ids


def _seeded_random_ids(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> set[int]:
    """Ids of the callee in ``random.Random(<seed>)``: a generator built from a seed is deterministic, so it is no randomness use."""
    seeded: set[int] = set()
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)):
            continue
        if _dotted(node.func, aliases) != "random.Random":
            continue
        args = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(not (isinstance(arg, ast.Constant) and arg.value is None) for arg in args):
            seeded.add(id(node.func))
    return seeded


def _effect_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[EffectUse, ...]:
    seams = _injection_defaults(nodes)
    rebound = _rebound_names(nodes)
    hints = _annotation_node_ids(nodes)
    seeded = _seeded_random_ids(nodes, aliases)
    uses: set[EffectUse] = set(_global_effect_uses(nodes))
    uses.update(_path_receiver_uses(tree, nodes, aliases))
    uses.update(_module_state_uses(tree, nodes, aliases))
    imported_os_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for use, bound_name in _os_branch_imports(node):
                uses.add(use)
                imported_os_names.add(bound_name)
        found = [_path_io_use(node, aliases), _patch_use(node, aliases)]
        if isinstance(node, ast.Attribute | ast.Name) and id(node) not in hints and not _is_shadowed(node, imported_os_names, rebound):
            found.append(None if id(node) in seeded else _reference_use(node, aliases, seams))
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


def _token_error_text(error: tokenize.TokenError | SyntaxError) -> str:
    """``"<line>: <message>"`` for a tokenizer failure; the line is 0 when the error carries none."""
    message = str(error.args[0]) if error.args else type(error).__name__
    where = error.args[1] if len(error.args) > 1 and isinstance(error.args[1], tuple) else ()
    line = where[0] if where and isinstance(where[0], int) else 0
    if isinstance(error, SyntaxError) and line == 0 and error.lineno is not None:
        line = error.lineno
    return f"{line}: {message}"


def _comment_facts(source: str) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...], str]:
    """One tokenize pass: suppression directives ``(line, kind)`` and ``# deferred: <reason>`` justifications, then the error.

    ``ast`` accepts sources ``tokenize`` rejects (a doubled carriage return, #181), so the comment facts of such a file
    are unknowable: both tuples are empty and the third item is ``"<line>: <message>"``, ``""`` when the pass succeeded.
    Newlines are deliberately not normalised first: facts are keyed by ``ast`` line numbers, which a rewrite could shift.

    A deferred comment justifies the code line it trails, or the next code line when it stands alone (comment lines
    that continue it are skipped; a blank line ends it). The reason must not be empty.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as error:
        return (), (), _token_error_text(error)
    suppressions, deferred = _comments_in(tokens)
    return suppressions, deferred, ""


def _comments_in(tokens: Sequence[tokenize.TokenInfo]) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    suppressions: list[tuple[int, str]] = []
    deferred: list[tuple[int, str]] = []
    pending: str | None = None
    for token in tokens:
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


def _discard_call_lines(nodes: Sequence[ast.AST]) -> tuple[int, ...]:
    lines: list[int] = []
    for node in nodes:
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


_PROTOCOL_NAMES = frozenset({"Protocol", "typing.Protocol", "typing_extensions.Protocol"})
_ANY_NAMES = frozenset({"Any", "typing.Any", "typing_extensions.Any", "object", "builtins.object"})
_BARE_CONTAINERS = frozenset({"dict", "list", "tuple", "set", "frozenset"})
_BARE_TYPING_CONTAINERS = frozenset({"Dict", "List", "Tuple", "Set", "FrozenSet"})
_STRING_KEYED_MAPPINGS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_ANNOTATION_WRAPPERS = frozenset({"Optional", "Union", "Annotated"})


def _last_name(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def _weak_annotation(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Why a return annotation says nothing, or None when it names a type. Looks through Optional/Union/`|`/Annotated and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _weak_string_annotation(node.value, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _weak_annotation(node.left, aliases) or _weak_annotation(node.right, aliases)
    if isinstance(node, ast.Subscript):
        return _weak_subscript(node, aliases)
    dotted = _dotted(node, aliases) or ""
    name = _last_name(node)
    if dotted in _ANY_NAMES or dotted.split(".")[-1] == "Any":
        return f"returns {dotted.split('.')[-1]}"
    if name in _BARE_CONTAINERS or (name in _BARE_TYPING_CONTAINERS and dotted.startswith("typing")):
        return f"returns a bare {name.lower()}"
    return None


def _weak_string_annotation(text: str, aliases: dict[str, str]) -> str | None:
    try:
        return _weak_annotation(ast.parse(text, mode="eval").body, aliases)
    except SyntaxError:
        return None


def _weak_subscript(node: ast.Subscript, aliases: dict[str, str]) -> str | None:
    name = _last_name(node.value)
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if name in _ANNOTATION_WRAPPERS:
        # Annotated's first item is the type; the rest are metadata
        return next(filter(None, (_weak_annotation(item, aliases) for item in (items[:1] if name == "Annotated" else items))), None)
    if name in _STRING_KEYED_MAPPINGS and len(items) == 2 and _last_name(items[0]) == "str":
        value = _dotted(items[1], aliases)
        if value is not None and value.split(".")[-1] == "Any":
            return f"returns {name}[str, Any]"
    return None


def _weak_port_returns(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str, str], ...]:
    """Methods of ``typing.Protocol`` classes (module-level or nested) whose return annotation is missing, Any, object,
    a bare dict/list/tuple/set or ``Mapping[str, Any]`` (#140). ``__init__`` is skipped: it returns None by definition."""
    found: list[tuple[int, str, str]] = []
    for cls in (n for n in nodes if isinstance(n, ast.ClassDef)):
        if not any(_dotted(base.value if isinstance(base, ast.Subscript) else base, aliases) in _PROTOCOL_NAMES for base in cls.bases):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) or stmt.name == "__init__":
                continue
            why = "has no return annotation" if stmt.returns is None else _weak_annotation(stmt.returns, aliases)
            if why is not None:
                found.append((stmt.lineno, f"{cls.name}.{stmt.name}", why))
    return tuple(sorted(found))


_ARBOR_LEGAL_KEYS = frozenset({"ring", "role", "ring_reason"})
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


def _arbor_reason(annotation: dict[str, object]) -> tuple[str | None, str | None]:
    """``(reason, error)`` for ``ring_reason``: a non-blank string without NUL characters, kept as written apart from trimming."""
    if "ring_reason" not in annotation:
        return None, None
    reason = annotation["ring_reason"]
    if not isinstance(reason, str) or "\x00" in reason or not reason.strip():
        return None, '__arbor__."ring_reason" must be a non-blank string'
    return reason.strip(), None


def _arbor_fields(annotation: dict[str, object]) -> tuple[str | None, str | None, str | None, str | None]:
    reason, reason_error = _arbor_reason(annotation)
    if reason_error is not None:
        return None, reason_error, None, None
    if "role" in annotation and annotation.get("role") != DEV_TOOL_ROLE:
        return None, f'__arbor__."role" must be "{DEV_TOOL_ROLE}"', None, None
    role = DEV_TOOL_ROLE if "role" in annotation else None
    if "ring" not in annotation:
        return None, (None if role is not None else '__arbor__ dictionary is missing "ring"'), role, None
    ring = annotation["ring"]
    if not isinstance(ring, str):
        return None, '__arbor__."ring" must be a string', None, None
    return ring, None, role, reason


def _arbor_ring(tree: ast.Module) -> tuple[str | None, str | None, str | None, str | None]:
    """``(ring, error, role, ring_reason)`` from the module's ``__arbor__`` assignment."""
    found = _find_arbor_assignments(list(tree.body), False)
    if not found:
        return None, None, None, None
    annotation = _arbor_dictionary(found)
    if isinstance(annotation, str):
        return None, annotation, None, None
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


def _purity_helpers(aliases: dict[str, str]) -> Helpers:
    def is_stub(body: list[ast.stmt]) -> bool:
        return _is_stub_body(body, aliases)

    def is_abstract(node: ast.ClassDef) -> bool:
        return _is_abstract_class(node, aliases)

    return Helpers(_dotted, is_stub, is_abstract)


def extract(source: str, filename: str = "<source>") -> SourceFacts:
    tree = ast.parse(source, filename=filename)
    imports: list[RawImport] = []
    _walk(tree, False, False, imports)
    arbor_ring, arbor_error, arbor_role, arbor_reason = _arbor_ring(tree)
    suppressions, deferred, untokenizable = _comment_facts(source)
    nodes = list(ast.walk(tree))  # one walk, shared by every fact below
    aliases = _aliases(nodes)  # computed once: each fact below reads it
    call_aliases = _call_aliases(nodes, aliases)
    return SourceFacts(
        tuple(imports),
        _effect_uses(tree, nodes, aliases),
        _script_deps(source),
        suppressions,
        _discard_call_lines(nodes),
        _top_level_defs(tree, aliases),
        arbor_ring,
        arbor_error,
        arbor_role,
        _file_ref_lines(nodes),
        _main_guard_line(tree),
        _string_literals(nodes),
        forwarders(tree),
        _weak_port_returns(nodes, aliases),
        *_surface(tree),
        deferred,
        _call_sites(tree, nodes, call_aliases),
        _result_defs(tree),
        _bare_calls(nodes, call_aliases),
        _function_calls(tree, aliases),
        ast.get_docstring(tree),
        arbor_reason,
        _dynamic_imports(nodes, aliases),
        purity_facts(tree, nodes, aliases, _purity_helpers(aliases)),
        untokenizable,
    )


def _bound_names(stmt: ast.stmt) -> list[tuple[str, tuple[int, str, str] | None]]:
    """Names a top-level statement binds; an import carries (level, module, imported name) so a re-export can be resolved."""
    if isinstance(stmt, ast.Import):
        return [(a.asname or a.name.split(".")[0], (0, a.name if a.asname is not None else a.name.split(".")[0], "")) for a in stmt.names]
    if isinstance(stmt, ast.ImportFrom):
        if stmt.module == "__future__":
            return []
        return [(a.asname or a.name, (stmt.level, stmt.module or "", a.name)) for a in stmt.names if a.name != "*"]
    if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return [(stmt.name, None)]
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
    return [(t.id, None) for t in targets if isinstance(t, ast.Name)]


def _static_all(tree: ast.Module) -> tuple[int, list[str]] | None:
    """(line, names) of a `__all__` built only from string-literal lists/tuples; None when absent or dynamic."""
    line = 0
    names: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            hit = any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
            hit = isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__"
        else:
            continue
        if not hit:
            continue
        value = stmt.value
        if not isinstance(value, ast.List | ast.Tuple) or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            return None
        if not isinstance(stmt, ast.AugAssign):
            names = []
        line = line or stmt.lineno
        names += [str(e.value) for e in value.elts if isinstance(e, ast.Constant)]
    return (line, names) if line else None


def _surface(tree: ast.AST) -> tuple[int, int, tuple[tuple[str, int, str, str], ...]]:
    """(size, line, re-exports) of a module's public surface (#136): a static `__all__`, else public top-level bindings."""
    if not isinstance(tree, ast.Module):
        return 0, 0, ()
    bound: dict[str, tuple[int, str, str] | None] = {}
    first = 0
    for stmt in tree.body:
        for name, source in _bound_names(stmt):
            bound[name] = source
            if not name.startswith("_") and not first:
                first = stmt.lineno
    declared = _static_all(tree)
    names = list(dict.fromkeys(declared[1])) if declared is not None else [n for n in bound if not n.startswith("_")]
    line = declared[0] if declared is not None else first
    return len(names), line, tuple((n, *source) for n in names if (source := bound.get(n)) is not None)


def _call_aliases(nodes: Sequence[ast.AST], imported: dict[str, str]) -> dict[str, str]:
    """Like ``_aliases`` but a relative import keeps its leading dots (``from ..a import B`` -> ``B: "..a.B"``) for the rule to resolve."""
    aliases = dict(imported)
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.level:
            prefix = "." * node.level + (node.module or "")
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{prefix}{'.' if node.module is not None else ''}{item.name}"
    return aliases


_DYNAMIC_IMPORT_CALLS = frozenset({"importlib.import_module", "importlib.__import__", "__import__", "builtins.__import__"})


def _dynamic_imports(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    """Every ``importlib.import_module(...)`` / ``__import__(...)`` call: its literal absolute target, or ``""`` when it cannot be read statically (#161)."""
    out: set[tuple[int, str]] = set()
    for node in nodes:
        if not isinstance(node, ast.Call) or _dotted(node.func, aliases) not in _DYNAMIC_IMPORT_CALLS:
            continue
        target = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        literal = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) else ""
        out.add((node.lineno, literal if literal and not literal.startswith(".") and "\x00" not in literal else ""))
    return tuple(sorted(out))


def _call_sites(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    """Calls whose head name is imported or defined at module level: what a construction rule can resolve; a method on a value is not."""
    heads = set(aliases) | {stmt.name for stmt in tree.body if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    out: set[tuple[int, str]] = set()
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        head = node.func
        while isinstance(head, ast.Attribute):
            head = head.value
        if isinstance(head, ast.Name) and head.id in heads and (name := _dotted(node.func, aliases)) is not None:
            out.add((node.lineno, name))
    return tuple(sorted(out))


_RESULT_SUFFIXES = ("Result", "Outcome")


def _annotation_names(node: ast.expr | None) -> Iterator[str]:
    """The names a return annotation may stand for: ``X``, ``m.X``, ``"X"``, ``X | None``, ``Optional[X]``; never ``list[X]``."""
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            yield from _annotation_names(ast.parse(node.value.strip(), mode="eval").body)
        except SyntaxError:
            return
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        yield from _annotation_names(node.left)
        yield from _annotation_names(node.right)
    elif isinstance(node, ast.Subscript) and next(_annotation_names(node.value), "") == "Optional":
        yield from _annotation_names(node.slice)


def _result_defs(tree: ast.AST) -> tuple[str, ...]:
    body = tree.body if isinstance(tree, ast.Module) else []
    return tuple(
        stmt.name for stmt in body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(name.endswith(_RESULT_SUFFIXES) and name not in _RESULT_SUFFIXES for name in _annotation_names(stmt.returns))
    )


def _bare_calls(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    found: list[tuple[int, str]] = []
    for node in nodes:
        if not isinstance(node, ast.Expr):
            continue
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        target = _dotted(value.func, aliases) if isinstance(value, ast.Call) else None
        if target is not None:
            found.append((node.lineno, target))
    return tuple(sorted(found))


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _imported_calls(func: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> tuple[str, ...]:
    calls: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _root_name(node.func) in aliases and (target := _dotted(node.func, aliases)) is not None:
            calls.add(target)
    return tuple(sorted(calls))


def _function_calls(tree: ast.Module, aliases: dict[str, str]) -> tuple[FunctionCalls, ...]:
    out: list[FunctionCalls] = []

    def visit(func: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str) -> None:
        if calls := _imported_calls(func, aliases):
            out.append(FunctionCalls(qualified, func.lineno, calls))

    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            visit(stmt, stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit(member, f"{stmt.name}.{member.name}")
    return tuple(out)


_LITERAL_NAME = re.compile(r"[A-Za-z_][\w.\-]{0,79}")
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _string_literals(nodes: Sequence[ast.AST]) -> tuple[tuple[int, str], ...]:
    """Identifier-like string constants (an environment variable, a key, a protocol name); a docstring is prose, not a spelling."""
    docstrings = {
        id(node.body[0].value) for node in nodes
        if isinstance(node, _DOCSTRING_OWNERS) and node.body and isinstance(node.body[0], ast.Expr)
    }
    return tuple(sorted({
        (node.lineno, node.value) for node in nodes
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


def _file_ref_lines(nodes: Sequence[ast.AST]) -> tuple[int, ...]:
    return tuple(sorted(node.lineno for node in nodes if isinstance(node, ast.Name) and node.id == "__file__"))


def absolute_callee(module: str, is_package: bool, callee: str) -> str | None:
    """A call recorded by ``extract`` as a dotted name: relative imports keep their dots, a bare name is defined in this module."""
    if not callee.startswith("."):
        return callee if "." in callee else f"{module}.{callee}"
    level = len(callee) - len(callee.lstrip("."))
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    if level - 1 > len(package):
        return None
    return ".".join([*package[: len(package) - (level - 1)], callee[level:]])


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
