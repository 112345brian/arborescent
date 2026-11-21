"""``run_check``: the one effectful step of ``check`` (#173) — everything else it calls lives in ``engine`` and reaches
effects only through injected ports, which the mixed-purity rule doesn't count as a direct effect. This module exists
because ``run_check`` itself directly constructs and manages a ``ThreadPoolExecutor``, a real, literal effect.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from . import baseline as ratchet_mod
from .certification import PROFILE, certify, version_warnings
from .domain import INFORMATIONAL_RULES, Policy
from .engine import (
    CheckReport,
    EngineRun,
    Options,
    Services,
    apply_modularity,
    apply_ratchet,
    changed_files,
    child_arbor_runs,
    dev_tool_counts,
    drift_report,
    nested_exclusions,
    plan_engines,
    ratchet_runs,
    record_inventories,
    run_own,
    run_tool,
)
from .suggestions import file_size_suggestions

__all__ = ["run_check"]


def run_check(policy: Policy, root: Path, services: Services, opts: Options | None = None) -> CheckReport:
    opts = opts if opts is not None else Options()
    if opts.require_profile and not opts.require_tools:
        opts = replace(opts, require_tools=True)
    started = services.clock.now()
    report = CheckReport(tool_checks_enabled=opts.tools)
    only = changed_files(services.runner, root, opts.changed) if opts.changed is not None else None
    report.partial = only is not None
    pool = ThreadPoolExecutor(max_workers=max(1, len(services.engines) + 1))
    try:
        files = sorted(path for path in only if services.fs.is_file(root / path)) if only is not None else []
        planned = plan_engines(policy, services, opts, files if only is not None else None)
        futures: list[Future[EngineRun] | EngineRun] = [
            item if isinstance(item, EngineRun)
            else pool.submit(run_tool, services, item, files, root / getattr(item, "working_directory", "."), opts)
            for item in planned
        ]
        t0 = services.clock.now()
        found, scan = run_own(policy, root, services, only)
        report.engines.append(EngineRun("arborescent", "ok", services.clock.now() - t0, [f"{len(scan.modules)} modules, {scan.parsed} parsed"]))
        report.parsed = scan.parsed
        record_inventories(report, scan.modules)
        report.excluded, report.excluded_nested = dict(scan.excluded), nested_exclusions(scan.excluded, policy.exclude)
        report.vendor_folders = tuple(scan.vendor_folders)
        report.dev_tools = dev_tool_counts(policy, scan)
        report.suggestions = file_size_suggestions(
            services.fs,
            root,
            {module.name: module.path for module in scan.modules.values()},
            threshold=policy.suggestion_threshold,
        )
        report.drift = drift_report(policy, root, services, scan, opts)
        engine_runs = [f.result() if isinstance(f, Future) else f for f in futures]
        report.engines += [*engine_runs, *ratchet_runs(policy, root, services, scan, found)]
        report.notes = [v for v in found if v.rule in INFORMATIONAL_RULES]
        report.violations = apply_modularity(policy, report, found, opts, only is None) + [v for run in engine_runs for v in run.violations]
        base: set[str] = ratchet_mod.load(services.fs, root / policy.baseline) if opts.use_baseline else set()
        ruff_skipped = any(run.name == "ruff" and run.status in {"skipped", "disabled"} for run in engine_runs)
        if not opts.tools or ruff_skipped:
            # Ruff is the only external engine whose findings enter the baseline; absent output cannot prove them stale.
            base = {key for key in base if not key.split("|", 1)[0].startswith("ruff-")}
        report.ratchet = apply_ratchet(report, base, opts.rules, only is None)
        if opts.require_profile:
            report.profile = PROFILE
            report.profile_failures = certify(root, policy, scan, services.runner, services.fs)
            report.profile_warnings = version_warnings(root, services.runner, policy.disabled_tools)
        report.engines.extend(child_arbor_runs(policy, root, services, opts))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    report.seconds = services.clock.now() - started
    return report
