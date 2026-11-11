"""The ``[effects]`` policy table (0.8.0): which modules are sanctioned capability seams.

A seam is the module that owns one effect on purpose (a file-read port, a process-environment reader, the logging setup)
so that everything else can take it as a port. ``effect-via-import`` used to report every importer of such a module,
which is the sanctioned path itself. Naming the seam here stops its *own* direct uses from propagating to importers;
the guard that makes that safe is ``effect-unsanctioned-import``: a module in a ring that forbids effects may import a
module of a provider ring only if the seam is listed. Without ``sanctioned`` the table is off and nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROVIDER_RINGS = ("effects",)


@dataclass(frozen=True)
class EffectsPolicy:
    """``sanctioned``: dotted names, prefixes or globs of seam modules (matched like ``classify`` globs). ``rings``: the rings that hold
    modules which exist to use effects; the guard applies to imports of their modules. ``configured`` is true when the policy sets
    ``sanctioned`` (an empty list still switches the guard on: nothing is sanctioned yet)."""

    sanctioned: tuple[str, ...] = ()
    rings: tuple[str, ...] = DEFAULT_PROVIDER_RINGS
    configured: bool = False
