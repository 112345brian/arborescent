"""Terminal presentation for Arborescent check reports."""

from __future__ import annotations

import argparse

from . import baseline as ratchet_mod
from .domain import Policy
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


def _print_report(args: argparse.Namespace, report: CheckReport, setup_command: str | None = None) -> None:
    for failure in report.profile_failures:
        print(f"PROFILE FAIL {failure}")
    _print_consistency(report)
    if report.profile is not None and report.ok:
        print(f"CERTIFIED meets Arborescent minimum profile {report.profile} (Ruff 0.17.2, Tach 0.35.3)")
    for line in report.drift:
        print(f"DRIFT  {line}")
    _print_hints(report)
    _print_inventories(report)
    shown = report.ratchet.new if report.ratchet and not args.no_baseline else report.violations
    for v in shown:
        print(f"FAIL   {v.render()}")
    if report.ratchet:
        for key in report.ratchet.stale:
            print(f"STALE  baseline entry no longer occurs (delete it: `arborescent baseline`): {key}")
    for e in report.engines:
        print(f"{e.status.upper():7}{e.name} ({e.seconds:.2f}s) {' | '.join(e.lines[-1:]) if e.status != 'fail' else ''}")
        if e.status == "fail":
            print("\n".join("        " + ln for ln in e.lines))
    _print_setup_hint(report, setup_command)
    _print_summary(report)


def _print_hints(report: CheckReport) -> None:
    """Report-only lines: size suggestions and informational findings. Neither fails a check."""
    for suggestion in report.suggestions:
        print(f"SUGGEST {suggestion.message}")
    for note in report.notes:
        print(f"NOTE   {note.render()}")


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
    print(
        f"arborescent check: {'OK' if report.ok else 'FAILED'} in {report.seconds:.2f}s "
        f"({len(report.violations)} violation(s), {tolerated} baselined, {report.parsed} file(s) parsed"
        f"{', partial: --changed' if report.partial else ''})"
    )


