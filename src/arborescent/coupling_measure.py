"""Pure measurement for ``arborescent coupling`` (#148): one scan's fan-in, closures and ratchet counts, and the JSON report.

Split out of ``cli_coupling`` so the command module holds only the effectful steps (printing, saving the baseline); this
module computes values from a scan and reads the baseline through the injected ``FileSystem``, matching ``cycles_report``.
"""

from __future__ import annotations

from pathlib import Path

from . import coupling_ratchet as ratchet
from .coupling import (
    Closure,
    FanIn,
    Hub,
    ImportEdges,
    blame,
    entrypoint_closure,
    fan_in,
    import_chain,
    import_edges,
    with_parent_packages,
)
from .domain import Policy, PolicyError
from .ports import FileSystem, ScanResult

__all__ = ["Measure", "blame_lines", "document", "eager_mounts", "load_baseline", "startup_failures"]

BLAME_LINES = 5


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


def blame_lines(measure: Measure, entrypoint: str, top: int = BLAME_LINES) -> list[str]:
    """The ``top`` heaviest imports of an entrypoint's closure. A module several others import needs every edge cut."""
    found = blame(measure.edges.runtime, entrypoint)
    return [
        f"{', '.join(item.importers)} -> {item.target} pulls {item.modules} module(s)"
        + (" (every one of these edges must go)" if len(item.importers) > 1 else "")
        for item in found[:top]
    ]


def eager_mounts(measure: Measure) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(mount name, entrypoint, import chain)`` for every lazy mount an entrypoint loads at import time.

    The chain counts parent packages. A mounted module does not count against itself or its own submodules."""
    mounts = measure.policy.coupling.lazy_mounts
    if not mounts:
        return []
    graph = with_parent_packages(measure.edges.runtime, measure.scan.known)
    return [
        (mount.name, entrypoint, chain)
        for mount in mounts
        for entrypoint in measure.policy.coupling.entrypoints
        if entrypoint != mount.module and not entrypoint.startswith(mount.module + ".")
        if (chain := import_chain(graph, entrypoint, mount.module)) is not None
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
