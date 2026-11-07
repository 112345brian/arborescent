"""Arborescent checks itself, the generated configs are accepted by the real engines (when installed), and the part of the
speed budget Arborescent controls (its own checks on a small repo, warm) is asserted. Everything else is measured by
``arborescent bench`` and reported, never asserted: external engines' speed is not ours."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from arborescent_helpers import generate, mini_policy, write_files

from arborescent import scanner
from arborescent.adapters import (
    FsScanner,
    LocalFileSystem,
    SubprocessToolRunner,
    SystemClock,
)
from arborescent.config import load_policy
from arborescent.engine import Options, Services, run_check, run_own
from arborescent.engines import default_engines
from arborescent.package_checks import AnnotationPackageChecks
from arborescent.typecheckers import all_checkers

HERE = Path(__file__).resolve().parents[1]
CHECKERS = list(all_checkers().values())


def own_services(cache: Path | None = None) -> Services:
    return Services(FsScanner(cache), SubprocessToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), default_engines(), AnnotationPackageChecks())


def test_arborescent_passes_its_own_policy_and_reports_generated_preset_drift_advisorially() -> None:
    policy, root = load_policy(LocalFileSystem(), HERE / "pyproject.toml")
    report = run_check(policy, root, own_services(), Options(tools=False, use_baseline=False))
    assert report.violations == [], [v.render() for v in report.violations]
    assert report.ok  # existing generated files may drift; that signal is informational


# Both speed budgets are per file, not absolute: they were set when this repository had 32 modules (1.0 s cold, 0.10 s warm), and
# the work is linear in the number of files. A fixed total turned the repository growing into a failure (on a slow runner 66
# modules took 1.12 s cold and 101 ms warm), while a per-file slowdown of the scanner or the cache still trips them.
BUDGET_ANCHOR_FILES = 32
COLD_BUDGET_SECONDS_PER_FILE = 1.0 / BUDGET_ANCHOR_FILES
WARM_BUDGET_SECONDS_PER_FILE = 0.15 / BUDGET_ANCHOR_FILES  # Windows CI measured 3.2 ms/file warm against 3.1; a re-parse costs ~31 ms/file


def test_warm_own_checks_on_a_small_repo_stay_within_budget(tmp_path: Path) -> None:
    """Budget for the code we control: content-hash cache warm, scaled per file (see BUDGET_ANCHOR_FILES). Generous (CI machines are slow) but a real
    regression such as parsing twice or re-reading the tree per rule would blow through it."""
    policy, root = load_policy(LocalFileSystem(), HERE / "pyproject.toml")
    svc = own_services(tmp_path / "cache")
    _ = run_own(policy, root, svc, practices=False)  # cold
    runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        _, scan = run_own(policy, root, svc, practices=False)
        runs.append(time.perf_counter() - t0)
        assert scan.parsed == 0
    budget = max(0.15, WARM_BUDGET_SECONDS_PER_FILE * len(scan.known))
    assert min(runs) < budget, f"warm own checks took {min(runs) * 1000:.0f} ms for {len(scan.known)} files (budget {budget * 1000:.0f} ms)"


def test_cold_own_checks_on_a_small_repo_stay_within_budget(tmp_path: Path) -> None:
    """A single cold run is one sample on a shared CI runner: a GC pause or scheduling blip fails it on noise alone.
    Take the min of a few fresh-cache runs, as the warm test above already does."""
    policy, root = load_policy(LocalFileSystem(), HERE / "pyproject.toml")
    best_cold = None
    for i in range(3):
        t0 = time.perf_counter()
        _, scan = run_own(policy, root, own_services(tmp_path / f"cache{i}"), practices=False)
        cold = time.perf_counter() - t0
        assert scan.parsed == len(scan.known)
        best_cold = cold if best_cold is None else min(best_cold, cold)
    budget = max(1.0, COLD_BUDGET_SECONDS_PER_FILE * scan.parsed)
    assert best_cold < budget, f"cold {best_cold * 1000:.0f} ms for {scan.parsed} files (budget {budget * 1000:.0f} ms)"


def test_no_file_is_parsed_twice_in_one_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = scanner.extract
    monkeypatch.setattr(scanner, "extract", lambda src, name="": (seen.append(name), real(src, name))[1])
    _ = run_check(mini_policy(), repo, Services(FsScanner(), SubprocessToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), default_engines(), AnnotationPackageChecks()), Options(tools=False))
    assert len(seen) == len(set(seen)) == 5


# --- the real engines accept what Arborescent generates --------------------------------------------------------------


def _write_generated(repo: Path, checker_names: list[str]) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    wanted = [c for c in CHECKERS if c.name in checker_names]
    for rel, text in generate(policy, sorted(scan.known), wanted).items():
        write_files(repo, {rel: text})


@pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed")
def test_import_linter_accepts_the_generated_contracts_and_catches_a_violation(repo: Path) -> None:
    _write_generated(repo, ["pyright"])
    runner = SubprocessToolRunner()
    ok = runner.run(["lint-imports", "--config", "arborescent-importlinter.toml"], repo)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    broken = runner.run(["lint-imports", "--config", "arborescent-importlinter.toml"], repo)
    assert broken.returncode != 0 and "BROKEN" in broken.stdout


@pytest.mark.skipif(shutil.which("tach") is None, reason="tach not installed")
def test_tach_accepts_the_generated_config_and_catches_a_violation(repo: Path) -> None:
    _write_generated(repo, ["pyright"])
    runner = SubprocessToolRunner()
    ok = runner.run(["tach", "check"], repo)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    assert runner.run(["tach", "check"], repo).returncode != 0


@pytest.mark.parametrize("name", ["pyright", "basedpyright", "ty", "pyrefly"])
def test_each_type_checker_accepts_its_generated_strict_config(repo: Path, name: str) -> None:
    checker = next(c for c in CHECKERS if c.name == name)
    if shutil.which(checker.executable) is None:
        pytest.skip(f"{name} not installed")
    _write_generated(repo, [name])
    runner = SubprocessToolRunner()
    clean = runner.run(checker.command([]), repo)
    assert checker.passed(clean), clean.stdout + clean.stderr
    write_files(repo, {"app/domain.py": "def f(x):\n    return x\n\nVALUE: int = 'text'\n"})
    assert not checker.passed(runner.run(checker.command([]), repo))


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright not installed")
def test_pyright_strict_config_ignores_type_ignore_comments(repo: Path) -> None:
    _write_generated(repo, ["pyright"])
    write_files(repo, {"app/domain.py": "VALUE: int = 'text'  # type: ignore\n"})
    assert not all_checkers()["pyright"].passed(SubprocessToolRunner().run(["pyright"], repo))


def test_generated_pyright_config_avoids_the_setting_that_made_pyright_100x_slower() -> None:
    """Measured: reportImportCycles took pyright from 11.5 s to >8 min on a 314-file repository (tach covers cycles)."""
    text = all_checkers()["pyright"].render_config(mini_policy(), ["app"])
    assert "reportImportCycles" not in text
