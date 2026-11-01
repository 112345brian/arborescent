"""The driver's import-rewriting step (#67): translate a plan into rewrite requests and call the second port.

Nothing is rewritten here and no tool's command line is known. The check runs on the monorepo's owned files (read
only) before anything is created; the rewrite runs on the new repository only.
"""

from __future__ import annotations

from .driver_plan import DriverPlan
from .driver_port import ImportRewriter
from .driver_request import (
    MigrationOutcome,
    MigrationSite,
    RewriteFinding,
    RewriteRequest,
)

__all__ = ["check_source", "rewrite_target"]


def check_source(plan: DriverPlan, rewriter: ImportRewriter, site: MigrationSite) -> tuple[RewriteFinding, ...]:
    """Statements in the package's own files the rewriter would corrupt; reads the monorepo, changes nothing."""
    if plan.rewrite is None:
        return ()
    return rewriter.unsupported(RewriteRequest(site.source, plan.rewrite.source_paths, plan.rewrite.renames))


def rewrite_target(plan: DriverPlan, rewriter: ImportRewriter, site: MigrationSite) -> MigrationOutcome:
    """Apply the module-name map to the new repository, never to the monorepo."""
    if plan.rewrite is None:
        return MigrationOutcome(True, "no module changes its name")
    return rewriter.apply(RewriteRequest(site.target, plan.rewrite.target_paths, plan.rewrite.renames))
