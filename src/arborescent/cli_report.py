"""Terminal presentation for Arborescent check reports."""

from __future__ import annotations

import argparse

from . import baseline as ratchet_mod
from .domain import Policy, Violation
from .engine import CheckReport


def _print_rule_label(policy: Policy) -> None:
    if policy.customization_labels:
        print(f"customised rules: {', '.join(policy.customization_labels)}")
    else:
        print(f"standard rules (baseline v{ratchet_mod.VERSION})")


def _print_excluded(report: CheckReport) -> None:
    vendor_folders = report.vendor_folders
    excluded = report.excluded
    print("auto-excluded vendor folders:")
    if vendor_folders:
        for folder, _ in vendor_folders:
            print(f"  {folder}/")
    else:
        print("  none")
    print("excluded folders:")
    if not excluded:
        print("  none")
    else:
        for folder, count in sorted(excluded.items()):
            print(f"  {folder}/ ({count} file(s))")
        _print_nested_exclusions(report.excluded_nested)
    if report.dev_tools:
        print("dev tools (no ring, may import product code, never imported by it):")
        for entry, count in sorted(report.dev_tools.items()):
            print(f"  {entry}/ ({count} module(s))")


def _print_nested_exclusions(nested: dict[str, str]) -> None:
    """One line per bare ``exclude`` name that reached below the repository root: it may have been meant as ``/name``."""
    for pattern in sorted(set(nested.values())):
        folders = sorted(folder for folder, name in nested.items() if name == pattern)
        print(f"  note: the bare entry {pattern!r} also hid {len(folders)} folder(s) below the root ({folders[0]}/ ...); write '/{pattern}' to exclude only the top-level one")


def _print_report(args: argparse.Namespace, report: CheckReport, setup_command: str | None = None) -> None:
    for warning in report.profile_warnings:
        print(f"PROFILE WARN {warning}")
    for failure in report.profile_failures:
        print(f"PROFILE FAIL {failure}")
    _print_modularity(report)
    _print_consistency(report)
    if report.profile is not None and report.ok:
        print(f"CERTIFIED meets Arborescent minimum profile {report.profile} (Ruff 0.17.2, Tach 0.35.3)")
    for line in report.drift:
        print(f"DRIFT  {line}")
    _print_hints(report)
    _print_inventories(report)
    _print_findings(args, report)
    _print_engines(report)
    _print_setup_hint(report, setup_command)
    _print_summary(report)


def _print_findings(args: argparse.Namespace, report: CheckReport) -> None:
    shown = report.ratchet.new if report.ratchet and not args.no_baseline else report.violations
    if getattr(args, "by_root_cause", False):
        shown = _print_root_causes(shown)
    for v in shown:
        print(f"FAIL   {v.render()}")
    if report.ratchet:
        for key in report.ratchet.stale:
            print(f"STALE  baseline entry no longer occurs (delete it: `arborescent baseline --prune`): {key}")


_SAMPLE = 5


def _print_root_causes(shown: list[Violation]) -> list[Violation]:
    """One line per root cause, widest fan-out first, with a sample of the modules it reaches (#201); returns the findings that name none."""
    for cause, members in ratchet_mod.root_cause_groups(shown):
        subjects = sorted({v.subject for v in members})
        more = f", ... +{len(subjects) - _SAMPLE}" if len(subjects) > _SAMPLE else ""
        print(f"CAUSE  {len(subjects):>5} module(s), {len(members)} finding(s)  {cause}  e.g. {', '.join(subjects[:_SAMPLE])}{more}")
        if members[0].hint:
            print(f"         {members[0].hint.strip()}")
    return [v for v in shown if not v.root_cause]


def _print_engines(report: CheckReport) -> None:
    for e in report.engines:
        print(f"{e.status.upper():9}{e.name} ({e.seconds:.2f}s) {' | '.join(e.lines[-1:]) if e.status != 'fail' else ''}")
        if e.status == "fail":
            print("\n".join("        " + ln for ln in e.lines))


def _print_hints(report: CheckReport) -> None:
    """Report-only lines: size suggestions and informational findings. Neither fails a check."""
    for suggestion in report.suggestions:
        print(f"SUGGEST {suggestion.message}")
    for note in report.notes:
        print(f"NOTE   {note.render()}")


def _print_modularity(report: CheckReport) -> None:
    for failure in report.visibility_failures:
        print(f"VISIBILITY FAIL {failure}")
    for failure in report.modularity_failures:
        print(f"MODULARITY FAIL {failure}")
    if report.modularity_certified and report.ok:
        print("CERTIFIED modularity ready")


def _print_consistency(report: CheckReport) -> None:
    if report.configured_ok:
        print("CONSISTENT configured Arborescent checks pass")


def _print_inventories(report: CheckReport) -> None:
    print("suppression counts by module:")
    grouped: dict[str, dict[str, int]] = {}
    for (module, kind), count in report.suppression_counts.items():
        grouped.setdefault(module, {})[kind] = count
    if grouped:
        for module, kinds in sorted(grouped.items()):
            detail = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
            print(f"  {module}: {detail}")
    else:
        print("  none")
    print("discard calls by module:")
    if report.discard_counts:
        for module, count in sorted(report.discard_counts.items()):
            print(f"  {module}: {count}")
    else:
        print("  none")
    print("function-level imports by module:")
    if report.lazy_import_counts:
        for module, lazy in sorted(report.lazy_import_counts.items()):
            print(f"  {module}: {lazy.total} ({lazy.deferred} with `# deferred:`)")
    else:
        print("  none")


def _print_setup_hint(report: CheckReport, setup_command: str | None = None) -> None:
    missing = sorted({
        run.name
        for run in report.engines
        if run.status in {"skipped", "fail"} and any("is not installed" in line for line in run.lines)
    })
    if missing:
        command = setup_command or "arborescent install-tools"
        hint = f"SETUP  missing checker(s): {', '.join(missing)}; install with `{command}`"
        if setup_command is not None:
            hint += " or run `arborescent check --install-tools` for guided setup"
        print(hint)


def _print_summary(report: CheckReport) -> None:
    tolerated = report.ratchet.tolerated if report.ratchet else 0
    if report.ratchet is not None:
        new = {v.key for v in report.ratchet.new}
        summary = ratchet_mod.internal_import_summary(v for v in report.violations if v.key not in new)
        if summary is not None:
            print(summary)
        by_rule = ratchet_mod.rule_summary(report.violations, report.ratchet.new)
        if by_rule is not None:
            print(by_rule)
    causes = ratchet_mod.cause_count(report.violations)
    shared = f" / {causes} root cause(s)" if causes != len(report.violations) else ""
    print(
        f"arborescent check: {'OK' if report.ok else 'FAILED'} in {report.seconds:.2f}s "
        f"({len(report.violations)} violation(s){shared}, {tolerated} baselined, {report.parsed} file(s) parsed"
        f"{', partial: --changed' if report.partial else ''})"
    )


