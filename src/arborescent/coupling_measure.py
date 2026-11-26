"""Pure measurement for ``arborescent coupling`` (#148): one scan's fan-in, closures and ratchet counts, and the JSON report.

Split out of ``cli_coupling`` so the command module holds only the effectful steps (printing, saving the baseline, running
the measurement); this module computes values from a scan and reads files through the injected ``FileSystem``, matching
``cycles_report``. For the heavy-import report (#216) it says which third-party packages an entrypoint's startup path
imports and which commands use each one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import coupling_ratchet as ratchet
from .command_imports import command_references, registrations_by_module
from .coupling import (
    IMPORTTIME_MARKER,
    LOADED_MARKER,
    Closure,
    FanIn,
    Hub,
    ImportEdges,
    ImportRecord,
    blame,
    entrypoint_closure,
    fan_in,
    import_chain,
    import_edges,
    package_costs,
    parse_importtime,
    parse_loaded_modules,
    reach,
)
from .coupling_policy import EagerCommands
from .domain import Policy, PolicyError, ToolResult
from .ports import FileSystem, ScanResult, ToolRunner

__all__ = [
    "HEAVY_MS", "TIMEOUT_SECONDS", "ImportRun", "Measure", "MeasureSettings", "Measuring", "blame_lines", "command_users", "document", "eager_mounts",
    "importtime_code", "importtime_records", "load_baseline", "measure_settings", "measured_lines", "run_importtime",
    "startup_failures", "startup_packages",
]

BLAME_LINES = 5
HEAVY_MS = 50.0  # a third-party package that takes at least this long to import is heavy
TIMEOUT_SECONDS = 120.0  # per measured entrypoint
_NAMED = 5  # importers and commands listed per heavy package


class Measure:
    """Everything one scan says about coupling; built once per command."""

    def __init__(self, policy: Policy, scan: ScanResult) -> None:
        self.policy = policy
        self.scan = scan
        self.edges: ImportEdges = import_edges(scan.modules, scan.known)
        self.rows: list[FanIn] = fan_in(self.edges, scan.modules)
        missing = [name for name in policy.coupling.entrypoints if name not in scan.known]
        if missing:
            raise PolicyError(f"`coupling.entrypoints` names modules that do not exist: {missing}")
        self.closures: list[Closure] = [entrypoint_closure(self.edges, name) for name in policy.coupling.entrypoints]
        absent = [mount.module for mount in policy.coupling.lazy_mounts if mount.module not in scan.known]
        if absent:
            raise PolicyError(f"`coupling.lazy_mounts` names modules that do not exist: {absent}")

    def ring(self, module: str) -> str | None:
        scope = self.scan.scope_by_module.get(module)
        local = self.policy.scope(scope).policy if scope is not None and self.policy.scopes else self.policy
        return local.resolve_ring(self.scan.arbor_index, module)

    def over_cap(self, row: FanIn) -> int | None:
        cap = dict(self.policy.coupling.fan_in_max).get(self.ring(row.module) or "")
        return cap if cap is not None and row.direct > cap else None

    def counts(self) -> dict[str, dict[str, int]]:
        return ratchet.measured(self.policy.coupling, self.rows, self.closures)


def blame_lines(
    measure: Measure, entrypoint: str, top: int = BLAME_LINES, times: Mapping[str, int] | None = None
) -> list[str]:
    """The ``top`` heaviest imports of an entrypoint's closure. A module several others import needs every edge cut.

    ``times`` (module -> measured cumulative microseconds) adds what importing each target took."""
    found = blame(measure.edges.runtime, entrypoint, measure.edges.parents)
    return [
        f"{', '.join(item.importers)} -> {item.target} pulls {item.modules} module(s)"
        + (f", {times[item.target] / 1000:.1f} ms" if times is not None and item.target in times else "")
        + (" (every one of these edges must go)" if len(item.importers) > 1 else "")
        for item in found[:top]
    ]


def eager_mounts(measure: Measure) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(mount name, entrypoint, import chain)`` for every lazy mount an entrypoint loads at import time.

    The chain counts parent packages. A mounted module does not count against itself or its own submodules."""
    mounts = measure.policy.coupling.lazy_mounts
    if not mounts:
        return []
    return [
        (mount.name, entrypoint, chain)
        for mount in mounts
        for entrypoint in measure.policy.coupling.entrypoints
        if entrypoint != mount.module and not entrypoint.startswith(mount.module + ".")
        if (chain := import_chain(measure.edges.loading, entrypoint, mount.module)) is not None
    ]


def startup_failures(measure: Measure) -> list[str]:
    """Exceeded ``closure_max`` budgets (with the imports to blame) and lazy mounts that load eagerly. No baseline excuses these."""
    sizes = {closure.entrypoint: len(closure.modules) for closure in measure.closures}
    over = [
        "\n         ".join([f"startup budget: {name} loads {sizes[name]} project modules, budget {cap}", *blame_lines(measure, name)])
        for name, cap in measure.policy.coupling.closure_max
        if sizes[name] > cap
    ]
    return over + [
        f"lazy mount `{mount}`: {entrypoint} loads {chain[-1]} at import time: {' -> '.join(chain)}"
        for mount, entrypoint, chain in eager_mounts(measure)
    ]


def document(measure: Measure, hubs: list[Hub]) -> dict[str, object]:
    return {
        "fan_in": [
            {"module": r.module, "direct": r.direct, "module_level": r.runtime, "transitive": r.transitive, "ring": measure.ring(r.module)}
            for r in measure.rows
        ],
        "closures": [
            {"entrypoint": c.entrypoint, "modules": len(c.modules), "function_level_only": len(c.lazy_only), "type_only": len(c.type_only)}
            for c in measure.closures
        ],
        "hubs": [
            {"module": h.module, "importers": h.direct, "accessors": list(h.accessors), "public": h.public}
            for h in hubs
        ],
    }


def load_baseline(fs: FileSystem, path: Path) -> tuple[ratchet.Counts, list[object]] | None:
    """Read the ratchet's counts and growth log, or return None for an absent file."""
    try:
        return ratchet.parse(fs.read_json(path)) if fs.exists(path) else None
    except ValueError as exc:
        raise PolicyError(f"{path.name}: {exc}") from exc


def importtime_code(measure: Measure, root: Path, entrypoint: str) -> str:
    """The program ``python -X importtime -c`` runs: put the source roots on the path, mark the start, import the entrypoint
    and list what is then in ``sys.modules``.

    The list goes to file descriptor 2, so an entrypoint that replaces or closes ``sys.stderr`` does not lose it, and is
    written in ``finally``, so an import that raises ``SystemExit(0)`` still has one. ``__main__`` is this program itself."""
    parts = [scope.policy for scope in measure.policy.scopes] or [measure.policy]
    roots = list(dict.fromkeys(str((root / folder).absolute()) for part in parts for folder in part.source_roots))
    return (
        f"import os, sys\nsys.path[:0] = {roots!r}\nsys.stderr.write({IMPORTTIME_MARKER + chr(10)!r})\nsys.stderr.flush()\n"
        f"try:\n    __import__({entrypoint!r})\nfinally:\n"
        "    names = [name for name, module in list(sys.modules.items()) if module is not None and name != '__main__']\n"
        f"    os.write(2, ' '.join([{chr(10) + LOADED_MARKER!r}, *names, {chr(10)!r}]).encode('utf-8', 'replace'))\n"
    )


def _tops(measure: Measure) -> frozenset[str]:
    return frozenset(name.partition(".")[0] for name in measure.scan.known)


def _third_party(measure: Measure, modules: Iterable[str], lazy: bool) -> dict[str, list[str]]:
    """Third-party top-level package -> the ``modules`` that import it, at module level (``lazy``: in a function too)."""
    tops, stdlib = _tops(measure), measure.policy.stdlib
    found: dict[str, list[str]] = {}
    for name in sorted(modules):
        info = measure.scan.modules.get(name)
        for imp in () if info is None else info.facts.imports:
            top = "" if imp.level or imp.type_only or (imp.lazy and not lazy) else imp.module.partition(".")[0]
            if top and top not in tops and top not in stdlib and name not in found.setdefault(top, []):
                found[top].append(name)
    return found


def _startup_modules(measure: Measure, entrypoint: str) -> frozenset[str]:
    return reach(measure.edges.loading, [entrypoint])


def startup_packages(measure: Measure, entrypoint: str) -> dict[str, list[str]]:
    """Third-party packages the entrypoint's startup path imports itself, with the project modules that import each."""
    return _third_party(measure, _startup_modules(measure, entrypoint), lazy=False)


def _reached_by(measure: Measure, graph: Mapping[str, frozenset[str]], target: str) -> Iterable[str]:
    """Third-party packages a dotted name a command reads stands for: itself, or what its project module loads."""
    top = target.partition(".")[0]
    if top not in _tops(measure):
        return () if top in measure.policy.stdlib else (top,)
    parts = target.split(".")
    owner = next((name for end in range(len(parts), 0, -1) if (name := ".".join(parts[:end])) in measure.scan.known), None)
    return () if owner is None else _third_party(measure, reach(graph, [owner]), lazy=False)


def command_users(measure: Measure, read: Callable[[str], str | None], entrypoint: str) -> dict[str, list[str]]:
    """Third-party package -> the commands that use it, a static approximation.

    A lazy mount uses what the modules it loads import. A command function on the startup path (decorated or registered,
    as for ``entrypoint-eager-commands``) uses what it imports or reads in its body: the package itself, or a project
    module that loads it at module level. A use through a helper in the command's own module is not followed."""
    graph = measure.edges.loading
    users: dict[str, list[str]] = {}
    mounts = [mount for mount in measure.policy.coupling.lazy_mounts if mount.module != entrypoint]
    for mount in mounts:
        for package in _third_party(measure, reach(graph, [mount.module]), lazy=True):
            users.setdefault(package, []).append(mount.name)
    decorators = (measure.policy.coupling.eager_commands or EagerCommands()).decorators
    modules = {name: (info.path, info.is_package) for name, info in measure.scan.modules.items()}
    registered = registrations_by_module(modules, read, decorators)
    mounted = tuple(mount.module for mount in mounts)
    for name in sorted(_startup_modules(measure, entrypoint)):
        if name not in modules or name in mounted or name.startswith(tuple(module + "." for module in mounted)):
            continue
        path, is_package = modules[name]
        source = read(path)
        commands = () if source is None else command_references(source, name, is_package, decorators, registered.get(name, frozenset()))
        for function, targets in commands:
            for package in sorted({package for target in targets for package in _reached_by(measure, graph, target)}):
                users.setdefault(package, []).append(f"{name}:{function}")
    return {package: sorted(names) for package, names in users.items()}


@dataclass(frozen=True)
class ImportRun:
    """One successful measured import."""

    records: list[ImportRecord]  # what ``-X importtime`` timed: no line for a module ``importlib.import_module`` loaded
    loaded: frozenset[str]  # every name in ``sys.modules`` afterwards


def importtime_records(result: ToolResult) -> ImportRun | str:
    """What one ``-X importtime`` run measured, or why it measured nothing. A run that did not exit 0 gives no numbers."""
    if result.returncode != 0:
        said = [
            line.strip() for line in result.stderr.replace("\x00", "").splitlines()
            if line.strip() and not line.startswith(("import time:", IMPORTTIME_MARKER, LOADED_MARKER))
        ]
        return said[-1] if said else f"exit code {result.returncode}"
    records = parse_importtime(result.stderr)
    if not records:
        return "no import-time output"
    loaded = parse_loaded_modules(result.stderr)
    if loaded is None:
        return "no list of loaded modules: the process ended during the import"
    return ImportRun(records, loaded)


def _listed(names: list[str]) -> str:
    return ", ".join(names[:_NAMED]) + (f" and {len(names) - _NAMED} more" if len(names) > _NAMED else "")


def _used_by(commands: list[str]) -> str:
    if not commands:
        return "used by no command found"
    if len(commands) == 1:
        return f"only used by {commands[0]}: import it where that command runs"
    return f"used by {len(commands)} commands: {_listed(commands)}"


def measured_lines(
    measure: Measure, read: Callable[[str], str | None], entrypoint: str, run: ImportRun, threshold_ms: float
) -> list[str]:
    """The measured time of one entrypoint and its heavy third-party imports with the commands that use each (#216).

    A package is listed when the startup path imports it itself and the run spent at least ``threshold_ms`` importing it."""
    records = run.records
    total = sum(record.cumulative_us for record in records if not record.ancestors)
    loaded = len(run.loaded & measure.scan.known)
    first = f"{entrypoint}: measured {total / 1000:.1f} ms, {loaded} project modules loaded"
    costs = package_costs(records)
    heavy = sorted(
        (
            (costs[package], package, importers)
            for package, importers in startup_packages(measure, entrypoint).items()
            if package in costs and costs[package] >= threshold_ms * 1000
        ),
        key=lambda item: (-item[0], item[1]),
    )
    title = f"  heavy third-party imports (>= {threshold_ms:g} ms):"
    if not heavy:
        return [first, f"{title} none"]
    users = command_users(measure, read, entrypoint)
    return [first, title] + [
        f"    {package} {cost / 1000:.1f} ms: imported by {_listed(importers)}; {_used_by(users.get(package, []))}"
        for cost, package, importers in heavy
    ]


@dataclass(frozen=True)
class MeasureSettings:
    """The settings of one ``coupling startup --measure`` run."""

    python: str
    timeout: float = TIMEOUT_SECONDS
    threshold_ms: float = HEAVY_MS


def measure_settings(
    measure: bool, defaults: MeasureSettings, python: str | None, timeout: float | None, threshold_ms: float | None
) -> MeasureSettings | None:
    """The settings of a measured run, or None without ``--measure``; its options without it are an error, not ignored."""
    if not measure:
        if python is not None or timeout is not None or threshold_ms is not None:
            raise PolicyError("--python, --timeout and --threshold-ms need --measure")
        return None
    return replace(
        defaults,
        python=defaults.python if python is None else python,
        timeout=defaults.timeout if timeout is None else timeout,
        threshold_ms=defaults.threshold_ms if threshold_ms is None else threshold_ms,
    )


@dataclass(frozen=True)
class Measuring:
    """What one ``--measure`` run works with: its settings, the process runner and the project's files."""

    settings: MeasureSettings
    runner: ToolRunner
    root: Path
    fs: FileSystem

    def read(self, path: str) -> str | None:
        try:
            return self.fs.read_text(self.root / path)
        except (OSError, UnicodeDecodeError):
            return None


def run_importtime(
    measure: Measure, runner: ToolRunner, root: Path, settings: MeasureSettings, entrypoint: str
) -> ImportRun | str:
    """Import ``entrypoint`` in a subprocess under ``-X importtime``: what it measured, or why it measured nothing.

    This runs the project's import-time code, so only the explicit reporting command calls it."""
    argv = [settings.python, "-X", "importtime", "-c", importtime_code(measure, root, entrypoint)]
    try:
        result = runner.run(argv, root, settings.timeout)
    except OSError as exc:
        return str(exc)
    return importtime_records(result)
