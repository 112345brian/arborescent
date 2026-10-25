"""Engine hardening: checker-name validation, no leaked worker pool, and import-linter/tach behind the Engine port."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from archlint_helpers import mini_policy, write_files

from archlint.adapters import FsScanner, LocalFileSystem, SystemClock
from archlint.cli import main
from archlint.domain import PolicyError, ToolResult
from archlint.engine import Options, Services, run_check
from archlint.engines import ImportLinter, Tach, default_engines
from archlint.generate import IMPORT_LINTER_FILE, generate
from archlint.testing import FakeToolRunner
from archlint.typecheckers import all_checkers, by_name

CHECKERS = list(all_checkers().values())
ALL_TOOLS = {"lint-imports", "tach", "pyright"}


def services(runner: FakeToolRunner | None = None, scanner: object | None = None, engines: Sequence[object] | None = None) -> Services:
    return Services(scanner or FsScanner(), runner or FakeToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), engines=default_engines() if engines is None else engines)  # type: ignore[arg-type]


def commit_generated(repo: Path) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    for rel, text in generate(policy, sorted(scan.known), by_name(["pyright"])).items():
        write_files(repo, {rel: text})


# --- 1. unknown type checker -> PolicyError ----------------------------------------------------------------------


def test_unknown_type_checker_in_the_policy_is_a_policy_error_naming_the_valid_ones(repo: Path) -> None:
    with pytest.raises(PolicyError) as err:
        run_check(mini_policy(type_checkers=["pyrite"]), repo, services(), Options(tools=False))
    assert "pyrite" in str(err.value) and "pyright" in str(err.value) and "pyrefly" in str(err.value)


def test_unknown_second_policy_checker_is_also_rejected(repo: Path) -> None:
    with pytest.raises(PolicyError, match="pyrite"):
        run_check(mini_policy(type_checkers=["pyright", "pyrite"]), repo, services(), Options(tools=False))


def test_unknown_override_is_a_policy_error(repo: Path) -> None:
    with pytest.raises(PolicyError, match="pyrite"):
        run_check(mini_policy(), repo, services(), Options(type_checker="pyrite", tools=False))


def test_cli_reports_an_unknown_policy_checker_with_exit_code_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy_text = (
        'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\ntype_checkers = ["pyrite"]\n'
        '[ring.domain]\nexternal = ["__future__"]\n[ring.ports]\nmay_import = ["domain"]\nexternal = ["__future__"]\n'
        '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["os", "__future__"]\n'
        '[ring.composition]\nmay_import = ["*"]\nexternal = ["sys"]\n'
        '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.ports" = "ports"\n"app.adapters" = "adapters"\n"app.main" = "composition"\n'
    )
    write_files(repo, {"archlint.toml": policy_text})
    cfg = str(repo / "archlint.toml")
    assert main(["--config", cfg, "check", "--no-tools"], services()) == 2
    assert "pyrite" in capsys.readouterr().err
    assert main(["--config", cfg, "generate"], services()) == 2


def test_cli_rejects_an_unknown_type_checker_flag_and_bench_checkers() -> None:
    with pytest.raises(SystemExit) as a:
        main(["check", "--type-checker", "pyrite"])
    with pytest.raises(SystemExit) as b:
        main(["bench", ".", "--checkers", "pyrite"])
    assert a.value.code == 2 and b.value.code == 2


def test_bench_with_an_unknown_checker_is_a_policy_error(tmp_path: Path) -> None:
    from archlint.bench import bench_one

    write_files(tmp_path, {"archlint.toml": 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n'})
    write_files(tmp_path, {"app/__init__.py": ""})
    with pytest.raises(PolicyError, match="pyrite"):
        bench_one(tmp_path, ["pyrite"], lambda root: services())


# --- 2. the worker pool is shut down when the scan raises ----------------------------------------------------------


class _Boom(RuntimeError):
    pass


class _RaisingScanner:
    def scan(self, policy: object, root: Path, only: object = None) -> object:
        raise _Boom("scanner failed")

    def uncached(self) -> _RaisingScanner:
        return self


@dataclass
class _Tracking(FakeToolRunner):
    running: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        with self.lock:
            self.running += 1
        try:
            time.sleep(0.3)
            return super().run(argv, cwd, timeout)
        finally:
            with self.lock:
                self.running -= 1


def _pool_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ThreadPoolExecutor")]


def test_a_raising_scanner_does_not_leak_the_pool_or_running_engines(repo: Path) -> None:
    runner = _Tracking(set(ALL_TOOLS))
    before = len(_pool_threads())
    with pytest.raises(_Boom):
        run_check(mini_policy(), repo, services(runner, _RaisingScanner()), Options())
    assert runner.running == 0, "an engine subprocess was still running after run_check raised"
    deadline = time.perf_counter() + 2
    while len(_pool_threads()) > before and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert len(_pool_threads()) <= before, "worker threads outlived run_check"


# --- 3. import-linter and tach are Engine adapters ------------------------------------------------------------------


def test_default_engines_are_import_linter_then_tach() -> None:
    engines = default_engines()
    assert [e.name for e in engines] == ["import-linter", "tach"]
    assert engines[0].command([]) == ["lint-imports", "--config", IMPORT_LINTER_FILE]
    assert engines[1].command(["a.py"]) == ["tach", "check"]  # these two check the whole graph whatever changed


def test_engine_pass_criteria() -> None:
    for engine in (ImportLinter(), Tach()):
        assert engine.passed(ToolResult(0, "", ""))
        assert not engine.passed(ToolResult(1, "boom", ""))
        assert not engine.passed(ToolResult(None, "", "missing"))


def test_run_check_runs_whatever_engines_the_services_carry(repo: Path) -> None:
    @dataclass(frozen=True)
    class Extra:
        name: str = "extra"
        executable: str = "extra-tool"

        def command(self, files: Sequence[str]) -> list[str]:
            return ["extra-tool", "--files", *files]

        def passed(self, result: ToolResult) -> bool:
            return "GOOD" in result.stdout

    commit_generated(repo)
    runner = FakeToolRunner({"extra-tool", "pyright"}, {"extra-tool": ToolResult(0, "meh", "")})
    report = run_check(mini_policy(), repo, services(runner, engines=[Extra()]), Options())
    by_name_ = {e.name: e.status for e in report.engines}
    assert by_name_ == {"archlint": "ok", "extra": "fail", "pyright": "ok"} and not report.ok
    assert ["extra-tool", "--files"] in runner.calls


def test_no_engines_means_only_the_type_checker_runs(repo: Path) -> None:
    commit_generated(repo)
    runner = FakeToolRunner({"pyright"})
    report = run_check(mini_policy(), repo, services(runner, engines=[]), Options())
    assert [e.name for e in report.engines] == ["archlint", "pyright"] and report.ok


def test_engine_order_in_the_report_is_stable(repo: Path) -> None:
    commit_generated(repo)
    report = run_check(mini_policy(), repo, services(FakeToolRunner(set(ALL_TOOLS))), Options())
    assert [e.name for e in report.engines] == ["archlint", "import-linter", "tach", "pyright"]


def test_changed_files_reach_the_type_checker_only(repo: Path) -> None:
    import subprocess

    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    write_files(repo, {"app/domain.py": "from __future__ import annotations\n\nVALUE: int = 2\n"})
    runner = FakeToolRunner(set(ALL_TOOLS))
    from archlint.adapters import SubprocessToolRunner

    class Recording(SubprocessToolRunner):
        def available(self, executable: str) -> bool:
            return executable in ALL_TOOLS or super().available(executable)

        def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
            if argv[0] in ALL_TOOLS:
                return runner.run(argv, cwd, timeout)
            return super().run(argv, cwd, timeout)

    svc = Services(FsScanner(), Recording(), CHECKERS, LocalFileSystem(), SystemClock(), engines=default_engines())
    run_check(mini_policy(), repo, svc, Options(changed="HEAD"))
    assert ["pyright", "app/domain.py"] in runner.calls and ["tach", "check"] in runner.calls
