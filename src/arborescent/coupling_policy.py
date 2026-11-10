"""The ``[coupling]`` policy table (#148): which modules are entrypoints and where a hub starts."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BASELINE = "arborescent-coupling.json"
DEFAULT_HUB_FAN_IN = 8


@dataclass(frozen=True)
class CouplingPolicy:
    """``entrypoints`` are dotted module names whose import closure is measured. ``fan_in_max`` caps the number of distinct
    importers per ring (ring -> cap) and only flags in the report. ``hub_fan_in`` is the importer count from which a module
    is a hub: hubs and entrypoints are what the ratchet in ``baseline`` holds."""

    entrypoints: tuple[str, ...] = ()
    fan_in_max: tuple[tuple[str, int], ...] = ()
    hub_fan_in: int = DEFAULT_HUB_FAN_IN
    baseline: str = DEFAULT_BASELINE
