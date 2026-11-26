"""The command line's argument surface: the parser, and the checks and lookups on what it parsed, with no I/O of their own.

Split out of ``cli`` so the composition root holds only the effectful steps (dispatching, printing); every function here
returns a value or raises ``PolicyError``, matching how ``cycles_report``/``cli_cycles`` split.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Collection
from pathlib import Path

from .baseline import OWN_ADOPTION_FAMILIES
from .cli_admit import add_admit_parser
from .cli_baseline_report import add_report_parser
from .cli_coupling import add_coupling_parser
from .cli_migrate import add_migrate_parser
from .cli_ratchet import add_ratchet_parser
from .domain import PolicyError, Rule
from .typecheckers import all_checkers

__all__ = ["build_parser", "catalog_root", "core_mode_conflict", "growth_approver", "usage_error", "validate_rule_selection"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arborescent",
        description="One architecture policy -> import-linter, tach, a type checker.",
    )
    p.add_argument(
        "--config",
        type=Path,
        help="arborescent.toml or pyproject.toml (default: search upwards)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="create missing optional tool config presets")
    install = sub.add_parser(
        "install-tools",
        help="install missing check tools into the active Python environment",
    )
    install.add_argument(
        "--type-checker",
        choices=sorted(all_checkers()),
        help="install this type checker instead of the policy default",
    )
    install.add_argument(
        "--yes",
        action="store_true",
        help="install without prompting (explicitly mutates this environment)",
    )
    _add_check_parser(sub.add_parser)
    _add_maintenance_parsers(sub.add_parser)
    _add_packages_parser(sub.add_parser)
    _add_bench_parser(sub.add_parser)
    return p


def _add_check_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    c = add_parser(
        "check", help="drift, engines (parallel), own checks, baseline ratchet"
    )
    c.add_argument(
        "--changed", metavar="REF", help="check only files changed since this git ref"
    )
    c.add_argument(
        "--type-checker",
        choices=sorted(all_checkers()),
        help="override the policy's type checker",
    )
    c.add_argument(
        "--no-tools",
        action="store_true",
        help="skip import-linter, tach and the type checker",
    )
    c.add_argument(
        "--engines",
        choices=("all", "core"),
        default="all",
        help="`core` runs only Arborescent's own checks (the same as --no-tools): no import-linter, tach, ruff or type checker is invoked or installed",
    )
    c.add_argument(
        "--list-dev-tools",
        action="store_true",
        help="print every `dev_tools` path with the modules below it, and annotated dev tools outside them, then exit",
    )
    c.add_argument(
        "--require-tools",
        action="store_true",
        help="a missing engine fails instead of skipping",
    )
    c.add_argument(
        "--install-tools",
        action="store_true",
        help="prompt to install missing check tools before checking",
    )
    c.add_argument(
        "--yes",
        action="store_true",
        help="install missing check tools without prompting (requires --install-tools)",
    )
    c.add_argument(
        "--require-profile",
        action="store_true",
        help="require Arborescent minimum profile v1",
    )
    c.add_argument(
        "--require-modularity",
        action="store_true",
        help="fail unless `modularity` is \"ready\" with a package_catalog and no package gap is open",
    )
    c.add_argument(
        "--require-visible",
        action="store_true",
        help="fail on every run-time import (importlib, __import__) that tach cannot follow",
    )
    c.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore the baseline: report every violation",
    )
    c.add_argument(
        "--growth-against",
        metavar="REF",
        help="verify baseline growth entries against a git ref",
    )
    c.add_argument(
        "--admitted-against",
        metavar="REF",
        help="fail a module new since this git ref that was not validly admitted (explicit __arbor__ ring that `arborescent admit` would accept)",
    )
    c.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="FAMILY",
        help="run and exit on this rule family only (repeatable; the families `baseline --adopt` accepts, or any rule id)",
    )
    c.add_argument(
        "--by-root-cause", action="store_true",
        help="list findings grouped by root cause (widest fan-out first) instead of one line per finding (#201)",
    )
    c.add_argument("--no-cache", action="store_true")


def _add_maintenance_parsers(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    b = add_parser(
        "baseline", help="create or shrink the baseline; growth must be explicit"
    )
    b.add_argument("--adopt", metavar="FAMILY", help="adopt one new rule family once")
    b.add_argument(
        "--allow-growth",
        action="store_true",
        help="allow baseline growth with a required reason",
    )
    b.add_argument("--reason", help="explain why new baseline keys are being recorded")
    b.add_argument("--approved-by", metavar="WHO", help="who approved the growth: a name or issue URL (required with --allow-growth)")
    b.add_argument(
        "--by-root-cause", action="store_true",
        help="record findings that name a root cause (fan-out rules) as one shrink-only entry per cause, listing the modules it reaches (#201)",
    )
    b.add_argument("--prune", action="store_true", help="only remove stale entries; never adds, works while the tree has new violations")
    add_report_parser(b)
    discover = add_parser(
        "discover",
        help="list in-scope Python files that still need a ring classification",
    )
    discover.add_argument("--suggest", action="store_true", help="per module, print the tightest ring its imports and effects permit, with the evidence")
    annotate = add_parser(
        "annotate",
        help="migrate legacy classify matches into __arbor__ annotations (needs --legacy --against REF; new modules go through `admit`; dry-run by default)",
    )
    annotate.add_argument("--legacy", action="store_true", help="migrate rings a human already wrote into `classify`; records policy, not a validated placement")
    annotate.add_argument("--against", metavar="REF", help="git reference: modules added since its merge base with HEAD are refused and routed to `arborescent admit`")
    annotate.add_argument(
        "--yes",
        action="store_true",
        help="write the annotations instead of printing what would change",
    )
    add_admit_parser(add_parser)
    add_parser("selftest", help="mutation-test the rules")
    add_coupling_parser(add_parser)
    add_parser("cycles", help="report import cycles, the edges to cut and the function-level imports that only break a cycle")
    purity = add_parser("purity", help="rank the modules that mix substantial pure logic with effectful code by how many modules import them")
    purity.add_argument("--json", action="store_true", help="print the ranked modules as JSON")
    purity.add_argument("--top", type=_positive_int, metavar="N", help="list only the N most imported mixed modules (the findings of `check` are unchanged)")
    purity.add_argument("--path", action="extend", nargs="+", default=[], metavar="PATH", help="list only mixed modules in these files or folders (repeatable; importer counts still cover the whole scan)")
    purity.add_argument("--module", action="extend", nargs="+", default=[], metavar="MODULE", help="list only these modules, their submodules or glob matches (repeatable)")
    purity.add_argument("--explain", metavar="MODULE", help="print, for each definition of MODULE, the call that made it effectful or `no effect found`")
    add_ratchet_parser(add_parser)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _add_packages_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    packages = add_parser(
        "packages", help="validate, list or extract declared package metadata"
    )
    package_actions = packages.add_subparsers(dest="package_action", required=True)
    for action in ("validate", "list"):
        command = package_actions.add_parser(action)
        command.add_argument(
            "--catalog",
            default="packages.toml",
            help="package metadata file relative to repository root",
        )
    _ = package_actions.choices["validate"].add_argument(
        "--extracted",
        type=Path,
        help="validate an extracted package directory instead of the repository catalog",
    )
    export = package_actions.add_parser(
        "export", help="write the read-only migration export (JSON) for the policy's package_catalog"
    )
    export.add_argument("--output", type=Path, help="file to write (default: standard output)")
    export.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 1 when the export still has an annotation gap of severity error",
    )
    _ = package_actions.add_parser("schema", help="print the JSON Schema the export validates against")
    add_migrate_parser(package_actions.add_parser)


def _add_bench_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    b = add_parser(
        "bench", help="time every engine, cold and warm, on prepared codebases"
    )
    b.add_argument(
        "dirs",
        nargs="+",
        type=Path,
        help="directories that each contain an arborescent.toml",
    )
    b.add_argument(
        "--checkers",
        nargs="*",
        choices=sorted(all_checkers()),
        default=sorted(all_checkers()),
    )


def catalog_root(is_file: Callable[[Path], bool], start: Path, catalog_path: str) -> Path:
    candidate = Path(catalog_path)
    if candidate.is_absolute():
        if not is_file(candidate):
            raise PolicyError(f"package catalog does not exist: {candidate}")
        return candidate.parent
    for folder in (start, *start.parents):
        if is_file(folder / candidate):
            return folder
    raise PolicyError(f"no {catalog_path!r} found from {start}")


def usage_error(args: argparse.Namespace) -> str | None:
    """A flag combination argparse cannot express; the message, or ``None`` when the arguments are fine."""
    if args.command == "check" and args.yes and not args.install_tools:
        return "--yes requires --install-tools"
    if args.command == "purity" and args.explain and (args.path or args.module):
        return "--explain already names one module: it takes no --path or --module"
    if getattr(args, "with_dependencies", False):
        return "--with-dependencies was removed: a dependency is a separate package, so extract each one on its own"
    return None


def core_mode_conflict(args: argparse.Namespace) -> str | None:
    """Core mode runs no external engine, so an option that demands one is rejected rather than silently passing."""
    if not (args.no_tools or args.engines == "core"):
        return None
    demanded = [flag for flag, on in (("--require-tools", args.require_tools), ("--require-profile", args.require_profile), ("--install-tools", args.install_tools)) if on]
    if not demanded:
        return None
    return f"{', '.join(demanded)} cannot be combined with core mode (--no-tools / --engines core): no external engine runs"


def validate_rule_selection(rules: list[str], engine_names: Collection[str]) -> None:
    """A typo in `--rule` must fail, not select nothing and report green."""
    known = {rule.value for rule in Rule} | set(engine_names) | OWN_ADOPTION_FAMILIES
    unknown = sorted(r for r in rules if r not in known and not r.startswith("ruff-") and not any(k.startswith(f"{r}-") for k in known))
    if unknown:
        raise PolicyError(f"unknown rule family {unknown[0]!r}; choose from {sorted(known)}")


def growth_approver(args: argparse.Namespace) -> str | None:
    """Who is on record as approving growth: required for ``--allow-growth``; an adoption is attributed to itself."""
    raw = args.approved_by
    approver = raw if isinstance(raw, str) else None
    if approver is not None and not (approver.strip() and args.allow_growth):
        raise PolicyError("`--approved-by` needs a non-blank value and `--allow-growth`")
    if args.allow_growth and approver is None:
        raise PolicyError("`--allow-growth` requires `--approved-by` (a name or issue URL)")
    if args.adopt is not None:
        return f"arborescent baseline --adopt {args.adopt}"
    return approver
