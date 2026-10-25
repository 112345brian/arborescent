"""ArchLint checks itself, the generated configs are accepted by the real engines (when installed), and the part of the
speed budget ArchLint controls (its own checks on a small repo, warm) is asserted. Everything else is measured by
``archlint bench`` and reported, never asserted: external engines' speed is not ours."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from archlint_helpers import mini_policy, write_files

from archlint.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from archlint.config import load_policy
from archlint.engine import Options, Services, run_check, run_own
from archlint.generate import generate
from archlint.typecheckers import all_checkers

HERE = Path(__file__).resolve().parents[1]
CHECKERS = list(all_checkers().values())


def own_services(cache: Path | None = None) -> Services:
    return Services(FsScanner(cache), SubprocessToolRunner(), CHECKERS, LocalFileSystem())


def test_archlint_passes_its_own_policy_and_its_generated_files_are_current() -> None:
    policy, root = load_policy(LocalFileSystem(), HERE / "pyproject.toml")
    report = run_check(policy, root, own_services(), Options(tools=False, use_baseline=False))
    assert report.violations == [], [v.render() for v in report.violations]
    assert report.drift == []


def test_warm_own_checks_on_a_small_repo_stay_within_budget(tmp_path: Path) -> None:
    """Budget for the code we control: ~20 files, content-hash cache warm. Generous (CI machines are slow) but a real
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
    assert min(runs) < 0.10, f"warm own checks took {min(runs) * 1000:.0f} ms (budget 100 ms)"


def test_cold_own_checks_on_a_small_repo_stay_within_budget(tmp_path: Path) -> None:
    policy, root = load_policy(LocalFileSystem(), HERE / "pyproject.toml")
    t0 = time.perf_counter()
    _, scan = run_own(policy, root, own_services(tmp_path / "cache"), practices=False)
    cold = time.perf_counter() - t0
    assert scan.parsed == len(scan.known) and cold < 1.0, f"cold {cold * 1000:.0f} ms for {scan.parsed} files"


def test_no_file_is_parsed_twice_in_one_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from archlint import adapters

    seen: list[str] = []
    real = adapters.extract
    monkeypatch.setattr(adapters, "extract", lambda src, name="": (seen.append(name), real(src, name))[1])
    _ = run_check(mini_policy(), repo, Services(FsScanner(), SubprocessToolRunner(), CHECKERS, LocalFileSystem()), Options(tools=False))
    assert len(seen) == len(set(seen)) == 5


# --- the real engines accept what ArchLint generates --------------------------------------------------------------


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
    ok = runner.run(["lint-imports", "--config", "archlint-importlinter.toml"], repo)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    broken = runner.run(["lint-imports", "--config", "archlint-importlinter.toml"], repo)
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
