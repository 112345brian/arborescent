"""``arborescent check --rule FAMILY`` (#137): run and exit on one rule family only."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from arborescent_helpers import write_files

from arborescent import baseline
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.config import load_policy
from arborescent.domain import Violation
from arborescent.engine import CheckReport, EngineRun, Options, Services, run_check
from arborescent.engines import default_engines
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers

POLICY = (
    'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\n'
    '[groups]\npure = ["__future__"]\n[ring.domain]\nexternal = ["@pure"]\n'
    '[ring.ports]\nmay_import = ["domain"]\nexternal = ["@pure"]\n'
    '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["@pure"]\n'
    '[ring.composition]\nmay_import = ["*"]\nexternal = ["@pure"]\n'
    '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.adapters" = "adapters"\n'
)


def _services() -> Services:
    return Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def _setup(repo: Path) -> Path:
    """One internal-import finding and one external-import finding, both baselined."""
    write_files(repo, {"arborescent.toml": POLICY, "app/domain.py": "import psutil\nfrom . import adapters\n", "app/adapters.py": "\n"})
    svc = _services()
    config = repo / "arborescent.toml"
    assert main(["--config", str(config), "baseline"], svc) == 0
    return config


def _rules(repo: Path) -> set[str]:
    document = baseline.document(LocalFileSystem(), repo / "arborescent-baseline.json")
    return {str(key).split("|", 1)[0] for key in cast("list[object]", document["violations"])}


def test_the_fixture_baselines_two_families(repo: Path) -> None:
    _setup(repo)
    assert {"internal-import", "external-import"} <= _rules(repo)


def test_unselected_new_findings_do_not_fail_a_selected_family(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _setup(repo)
    write_files(repo, {"app/domain.py": "import psutil\nimport sqlite3\nfrom . import adapters\n"})  # a new external-import
    base = ["--config", str(config), "check", "--no-tools"]
    assert main(base, _services()) == 1
    capsys.readouterr()
    assert main([*base, "--rule", "internal-import"], _services()) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "external-import" not in out.split("arborescent check:")[0].split("by rule:")[-1]


def test_selected_family_with_a_new_finding_fails(repo: Path) -> None:
    config = _setup(repo)
    write_files(repo, {"app/domain.py": "import psutil\nimport sqlite3\nfrom . import adapters\n"})
    assert main(["--config", str(config), "check", "--no-tools", "--rule", "external-import"], _services()) == 1


def test_rule_is_repeatable_and_unions_the_families(repo: Path) -> None:
    config = _setup(repo)
    write_files(repo, {"app/domain.py": "import psutil\nimport sqlite3\nfrom . import adapters\n"})
    args = ["--config", str(config), "check", "--no-tools", "--rule", "internal-import", "--rule", "external-import"]
    assert main(args, _services()) == 1


def test_a_typo_is_rejected_instead_of_selecting_nothing(repo: Path) -> None:
    config = _setup(repo)
    assert main(["--config", str(config), "check", "--no-tools", "--rule", "internal-imports"], _services()) == 2


def test_baseline_entries_outside_the_selection_are_not_stale(repo: Path) -> None:
    _setup(repo)
    policy = load_policy(LocalFileSystem(), repo / "arborescent.toml")[0]
    write_files(repo, {"app/domain.py": "from . import adapters\n"})  # the external-import finding is fixed
    whole = run_check(policy, repo, _services(), Options(tools=False))
    assert whole.ratchet is not None and whole.ratchet.stale
    selected = run_check(policy, repo, _services(), Options(tools=False, rules=("internal-import",)))
    assert selected.ratchet is not None and selected.ratchet.stale == []
    assert {v.rule for v in selected.violations} == {"internal-import"}


def test_stale_entries_inside_the_selection_still_count(repo: Path) -> None:
    _setup(repo)
    policy = load_policy(LocalFileSystem(), repo / "arborescent.toml")[0]
    write_files(repo, {"app/domain.py": "import psutil\n"})  # the internal-import finding is fixed
    selected = run_check(policy, repo, _services(), Options(tools=False, rules=("internal-import",)))
    assert selected.ratchet is not None and selected.ratchet.stale and not selected.ratchet.ok


def test_family_prefix_matches_like_adopt(repo: Path) -> None:
    config = _setup(repo)
    # `ruff` selects every `ruff-*` rule, exactly as `baseline --adopt ruff` does.
    assert baseline.in_family("ruff-BLE001|a|b", "ruff")
    assert not baseline.in_family("ruffle|a|b", "ruff")
    assert main(["--config", str(config), "check", "--no-tools", "--rule", "ruff"], _services()) == 0


def test_summary_splits_findings_by_rule_with_new_counts(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _setup(repo)
    write_files(repo, {"app/domain.py": "import psutil\nimport sqlite3\nfrom . import adapters\n"})
    main(["--config", str(config), "check", "--no-tools"], _services())
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("by rule:"))
    assert "external-import 2 (1 new)" in line
    assert "internal-import" in line and "(0 new)" in line


def test_summary_has_no_by_rule_line_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": POLICY, "app/__init__.py": "\n", "app/domain.py": "\n"})
    main(["--config", str(tmp_path / "arborescent.toml"), "check", "--no-tools"], _services())
    assert "by rule:" not in capsys.readouterr().out


def _report(rules: tuple[str, ...], runs: list[EngineRun]) -> CheckReport:
    return CheckReport(engines=runs, rules=rules)


def test_a_failing_engine_with_only_unselected_findings_does_not_fail_the_selection() -> None:
    unrelated = EngineRun("tach", "fail", 0.0, violations=[Violation("tach-unused-dependency", "a", "x")])
    assert _report(("internal-import",), [unrelated]).ok
    assert not _report((), [unrelated]).ok
    assert not _report(("tach",), [unrelated]).ok


def test_a_failing_engine_without_findings_still_fails_a_selection() -> None:
    crashed = EngineRun("tach", "fail", 0.0, lines=["boom"])
    assert not _report(("internal-import",), [crashed]).ok

