"""#59: run-time imports are edges tach cannot follow; they are reported, and `--require-visible` fails on them."""

from __future__ import annotations

from pathlib import Path

from arborescent_helpers import mini_policy, services, write_files

from arborescent.domain import INFORMATIONAL_RULES, TACH_EXCLUDED_FOLDERS, Rule
from arborescent.engine import (
    Options,
    blocking,
    child_arbor_command,
    run_check,
    run_own,
)
from arborescent.tach_visibility import TACH_EXCLUDED_PARTS

DYNAMIC = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n\n\ndef raw(name):\n    return __import__(name)\n"


def _findings(root: Path, **policy: object) -> list[tuple[str, str]]:
    found, _ = run_own(mini_policy(**policy), root, services(), practices=False)
    return [(v.subject, v.detail) for v in found if v.rule == Rule.TACH_INVISIBLE_IMPORT]


def test_run_time_imports_are_reported_per_module_with_their_lines(repo: Path) -> None:
    write_files(repo, {"app/loader.py": DYNAMIC, "app/static.py": "import os\n"})

    findings = _findings(repo)

    assert [subject for subject, _ in findings] == ["app.loader"]
    assert "2 run-time import call(s) (line 5, 9)" in findings[0][1]


def test_folders_tach_skips_are_not_reported(repo: Path) -> None:
    write_files(repo, {"tests/test_loader.py": DYNAMIC})

    assert _findings(repo) == []


def test_the_excluded_folders_are_the_shared_constant_not_a_second_copy() -> None:
    """#59's own excluded-folder set must stay the generated tach.toml `exclude` (engines.py): one shared list, not two."""
    assert frozenset(TACH_EXCLUDED_FOLDERS) == TACH_EXCLUDED_PARTS


def test_disabling_tach_silences_the_report(repo: Path) -> None:
    write_files(repo, {"app/loader.py": DYNAMIC})

    assert _findings(repo, disabled_tools=["tach"]) == []


def test_the_finding_is_informational_and_never_blocks_by_default(repo: Path) -> None:
    write_files(repo, {"app/loader.py": DYNAMIC})
    assert Rule.TACH_INVISIBLE_IMPORT in INFORMATIONAL_RULES

    report = run_check(mini_policy(), repo, services(), Options(tools=False, use_baseline=False))

    assert [v.subject for v in report.notes if v.rule == Rule.TACH_INVISIBLE_IMPORT] == ["app.loader"]
    assert not [v for v in blocking(report.notes) if v.rule == Rule.TACH_INVISIBLE_IMPORT]
    assert report.visibility_failures == ()


def test_require_visible_fails_the_check_and_names_each_module(repo: Path) -> None:
    write_files(repo, {"app/loader.py": DYNAMIC})

    report = run_check(mini_policy(), repo, services(), Options(tools=False, use_baseline=False, require_visible=True))

    assert len(report.visibility_failures) == 1 and "app.loader" in report.visibility_failures[0]
    assert not report.ok


def test_require_visible_passes_when_nothing_is_hidden(repo: Path) -> None:
    report = run_check(mini_policy(), repo, services(), Options(tools=False, use_baseline=False, require_visible=True))

    assert report.visibility_failures == ()


def test_the_flag_reaches_child_arbor_runs() -> None:
    argv = child_arbor_command(Path("child/arborescent.toml"), Options(require_visible=True))
    assert "--require-visible" in argv
