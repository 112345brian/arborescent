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
    entrypoint_closure,
    fan_in,
    import_edges,
)
from .domain import Policy, PolicyError
from .ports import FileSystem, ScanResult

__all__ = ["Measure", "document", "load_baseline"]


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

    def ring(self, module: str) -> str | None:
        scope = self.scan.scope_by_module.get(module)
        local = self.policy.scope(scope).policy if scope is not None and self.policy.scopes else self.policy
        return local.resolve_ring(self.scan.arbor_index, module)

    def over_cap(self, row: FanIn) -> int | None:
        cap = dict(self.policy.coupling.fan_in_max).get(self.ring(row.module) or "")
        return cap if cap is not None and row.direct > cap else None

    def counts(self) -> dict[str, dict[str, int]]:
        return ratchet.measured(self.policy.coupling, self.rows, self.closures)


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
    try:
        return ratchet.load(fs, path)
    except ValueError as exc:
        raise PolicyError(f"{path.name}: {exc}") from exc
