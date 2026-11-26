"""The ``[coupling]`` policy table (#148): which modules are entrypoints, where a hub starts, startup budgets and lazy mounts."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BASELINE = "arborescent-coupling.json"
DEFAULT_HUB_FAN_IN = 8
DEFAULT_COMMAND_DECORATORS = ("command", "callback", "group")


@dataclass(frozen=True)
class EagerCommands:
    """``[coupling.eager_commands]`` (#215): when a module's function-level imports count as eager CLI command modules.

    ``decorators`` register a command (a plain name matches the last segment, ``app.command`` matches exactly). A module is
    reported when at least ``min_imports`` of its function-level imports, and at least ``min_share`` of them, sit inside at
    least ``min_commands`` command bodies."""

    decorators: tuple[str, ...] = DEFAULT_COMMAND_DECORATORS
    min_imports: int = 3
    min_share: float = 0.5
    min_commands: int = 2


@dataclass(frozen=True)
class LazyMount:
    """One ``[coupling.lazy_mounts]`` entry (#216): the command ``name`` loads ``module`` only when it runs."""

    name: str
    module: str
    attr: str


@dataclass(frozen=True)
class CouplingPolicy:
    """``entrypoints`` are dotted module names whose import closure is measured. ``fan_in_max`` caps the number of distinct
    importers per ring (ring -> cap) and only flags in the report. ``hub_fan_in`` is the importer count from which a module
    is a hub: hubs and entrypoints are what the ratchet in ``baseline`` holds. ``closure_max`` is a hard cap per entrypoint,
    ``lazy_mounts`` are modules no entrypoint may load at import time, ``eager_commands`` opts into the #215 report."""

    entrypoints: tuple[str, ...] = ()
    fan_in_max: tuple[tuple[str, int], ...] = ()
    hub_fan_in: int = DEFAULT_HUB_FAN_IN
    baseline: str = DEFAULT_BASELINE
    closure_max: tuple[tuple[str, int], ...] = ()
    lazy_mounts: tuple[LazyMount, ...] = ()
    eager_commands: EagerCommands | None = None

    def __post_init__(self) -> None:
        if self.hub_fan_in < 1:
            raise ValueError("`coupling.hub_fan_in` must be a positive integer")
        if not self.baseline:
            raise ValueError("`coupling.baseline` must be a file path")
        if any(cap < 1 for _, cap in self.fan_in_max):
            raise ValueError("`coupling.fan_in_max` must map ring names to positive integers")
        if undeclared := sorted(name for name, _ in self.closure_max if name not in self.entrypoints):
            raise ValueError(f"`coupling.closure_max` names modules that are not in `coupling.entrypoints`: {undeclared}")
        if self.lazy_mounts and not self.entrypoints:
            raise ValueError("`coupling.lazy_mounts` needs `coupling.entrypoints`: nothing would be checked against them")
