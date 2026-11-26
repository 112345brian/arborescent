"""Effect categories: which canonical dotted API names are effectful, and in which category.

The one table ``effect-*``, ``effect-via-import`` and ``taint`` all read, so a call to ``print`` or ``open`` is
recognised consistently everywhere. Third-party network clients are matched by explicit call name, never by prefix.

``dotted_name`` is how a syntax node becomes such a canonical name."""

from __future__ import annotations

import ast
from collections.abc import Mapping

__all__ = ["EFFECT_CATEGORIES", "INJECTABLE_EFFECTS", "MODULE_STATE_CATEGORIES", "OS_BRANCH_ATTRS", "dotted_name", "effect_category_of"]


# The categories a policy may name in `forbid`. Four were added in 0.8.0 (`state`, `monotonic`, `host`, `fs-query`); they are valid
# in every `forbid` list but in no default one (`DEFAULT_FORBID_CATEGORIES`), so a policy that forbids nothing new keeps its findings.
EFFECT_CATEGORIES: frozenset[str] = frozenset(
    {
        "clock", "env", "identity", "randomness", "sleep", "threads", "process", "database", "network", "filesystem", "dynamic-import", "patch",
        "console", "global", "state", "monotonic", "host", "fs-query", "registry", "cache",
    }
)

# #194: shared module state that is not I/O. `registry` is a dict/list/set of a module or class written from inside a function; `cache` is
# a `functools.cache`/`lru_cache` loader or such a container with a cache-like name. Both report as `effect-state`,
# are in no default `forbid`, taint seed or `must_be_pure`, and are skipped by the readers that describe a module's I/O.
MODULE_STATE_CATEGORIES: frozenset[str] = frozenset({"registry", "cache"})

OS_BRANCH_ATTRS = frozenset({"os.name", "sys.platform", "platform.system"})


_DATABASE_PACKAGES = ("sqlite3.", "psycopg.", "psycopg2.", "asyncpg.", "pymysql.", "mysql.connector.", "sqlalchemy.", "pymongo.", "redis.", "aiosqlite.")
# Real network I/O only: `urllib.parse` and `http.HTTPStatus` are not (#129).
_NETWORK_PREFIXES = ("urllib.request.", "http.client.", "http.server.", "socket.")
_NOT_NETWORK = frozenset({"urllib.request.Request"})  # under a network prefix, but only builds an object: nothing is sent
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
    "fcntl.flock", "fcntl.lockf", "msvcrt.locking",  # a cross-process file lock is an operation on a file
    "tempfile.NamedTemporaryFile", "tempfile.TemporaryFile", "tempfile.TemporaryDirectory", "tempfile.SpooledTemporaryFile",
    "tempfile.mkstemp", "tempfile.mkdtemp", "tempfile.mktemp",
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


def _storage_category(target: str) -> str | None:
    if target.startswith(_DATABASE_PACKAGES):
        return "database"
    if target in _FILESYSTEM_APIS:
        return "filesystem"
    if target in _NETWORK_APIS or (target.startswith(_NETWORK_PREFIXES) and target not in _NOT_NETWORK):
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


_PROCESS_APIS = _PROCESS_OS_APIS | _PROCESS_POOL_APIS


def _concurrency_category(target: str) -> str | None:
    if target.startswith("threading."):
        return "state" if target.removeprefix("threading.") in _STATE_PRIMITIVES else "threads"
    if target in _THREAD_POOL_APIS:
        return "threads"
    if target.startswith(("multiprocessing.", "subprocess.", "asyncio.create_subprocess_")) or target in _PROCESS_APIS:
        return "process"
    return None


def _import_category(target: str) -> str | None:
    if target in _DYNAMIC_IMPORT_APIS or target.endswith(".exec_module"):
        return "dynamic-import"
    return None


INJECTABLE_EFFECTS = frozenset({"clock", "env", "identity", "randomness", "sleep", "threads", "process", "monotonic", "host"})


def dotted_name(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    """``a.b.c`` for attributes on a plain name, the name resolved through ``aliases``; ``None`` for anything else."""
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):  # a loop: generated code chains thousands of attributes
        attributes.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([aliases.get(node.id, node.id), *reversed(attributes)])
