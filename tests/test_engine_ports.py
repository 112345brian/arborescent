"""Engine hardening: checker-name validation, no leaked worker pool, and import-linter/tach behind the Engine port."""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.domain import PolicyError, ToolResult, Violation
from arborescent.engine import Options, Services, run_check
from arborescent.engines import ImportLinter, Tach, default_engines
from arborescent.generate import generate
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers, by_name

CHECKERS = list(all_checkers().values())
ALL_TOOLS = {"lint-imports", "tach", "pyright", "ruff"}


def services(runner: FakeToolRunner | None = None, scanner: object | None = None, engines: Sequence[object] | None = None) -> Services:
    return Services(scanner or FsScanner(), runner or FakeToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), engines=default_engines() if engines is None else engines)  # type: ignore[arg-type]


def commit_generated(repo: Path) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    for rel, text in generate(policy, sorted(scan.known), by_name(policy.type_checkers)).items():
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
    write_files(repo, {"arborescent.toml": policy_text})
    cfg = str(repo / "arborescent.toml")
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
    from arborescent.bench import bench_one

    write_files(tmp_path, {"arborescent.toml": 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n'})
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


def test_default_engines_include_graph_and_file_engines() -> None:
    engines = default_engines()
    assert [e.name for e in engines] == ["import-linter", "tach", "tach-external", "ruff"]
    assert engines[0].command([]) == ["lint-imports"]
    assert engines[1].command(["a.py"]) == ["tach", "check", "--dependencies"]
    assert engines[2].command([]) == ["tach", "check-external"]


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
    assert by_name_ == {"arborescent": "ok", "extra": "fail", "ty": "skipped"} and not report.ok
    assert ["extra-tool", "--files"] in runner.calls


def test_optional_engine_violations_are_collected_into_the_report_and_baseline(repo: Path) -> None:
    @dataclass(frozen=True)
    class Findings:
        name: str = "findings"
        executable: str = "findings-tool"

        def command(self, files: Sequence[str]) -> list[str]:
            return ["findings-tool"]

        def passed(self, result: ToolResult) -> bool:
            return result.returncode == 0

        def violations(self, result: ToolResult, root: Path, fs: object) -> list[Violation]:
            return [Violation("ruff-E722", "app/domain.py", "bare except | abc123")]

    commit_generated(repo)
    runner = FakeToolRunner({"findings-tool", "pyright"})
    report = run_check(mini_policy(), repo, services(runner, engines=[Findings()]), Options())

    key = "ruff-E722|app/domain.py|bare except | abc123"
    assert [v.key for v in report.violations] == [key]
    assert report.ratchet is not None and [v.key for v in report.ratchet.new] == [key]
    assert not report.ok


def test_optional_engine_violations_can_be_empty_or_partial(repo: Path) -> None:
    @dataclass(frozen=True)
    class PartialFindings:
        name: str = "partial"
        executable: str = "partial-tool"

        def command(self, files: Sequence[str]) -> list[str]:
            return ["partial-tool"]

        def passed(self, result: ToolResult) -> bool:
            return True

        def violations(self, result: ToolResult, root: Path, fs: object) -> list[Violation]:
            if result.stdout == "empty":
                return []
            return [Violation("ruff-E722", "one.py", "one finding | one")]

    commit_generated(repo)
    empty_runner = FakeToolRunner({"partial-tool", "pyright"}, {"partial-tool": ToolResult(0, "empty", "")})
    assert run_check(mini_policy(), repo, services(empty_runner, engines=[PartialFindings()]), Options()).violations == []

    partial_runner = FakeToolRunner({"partial-tool", "pyright"}, {"partial-tool": ToolResult(0, "partial", "")})
    report = run_check(mini_policy(), repo, services(partial_runner, engines=[PartialFindings()]), Options())
    assert [v.subject for v in report.violations] == ["one.py"]


def test_optional_engine_violations_failure_fails_closed(repo: Path) -> None:
    @dataclass(frozen=True)
    class BrokenFindings:
        name: str = "broken-findings"
        executable: str = "broken-findings-tool"

        def command(self, files: Sequence[str]) -> list[str]:
            return ["broken-findings-tool"]

        def passed(self, result: ToolResult) -> bool:
            return True

        def violations(self, result: ToolResult, root: Path, fs: object) -> list[Violation]:
            raise ValueError("second parse attempt failed")

    commit_generated(repo)
    runner = FakeToolRunner({"broken-findings-tool", "pyright"})
    report = run_check(mini_policy(), repo, services(runner, engines=[BrokenFindings()]), Options())
    engine = next(e for e in report.engines if e.name == "broken-findings")
    assert engine.status == "fail" and "second parse attempt failed" in " ".join(engine.lines)
    assert not report.ok


def test_engine_violation_results_have_a_consuming_call_site() -> None:
    from pathlib import Path

    source_dir = Path(__file__).parents[1] / "src" / "arborescent"
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in source_dir.rglob("*.py")}
    optional_method_lookups = [
        node
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "violations"
    ]
    assert len(optional_method_lookups) == 1, "all optional Engine.violations calls must flow through one collector"
    direct_engine_calls = [
        (path, node)
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "violations"
    ]
    assert len(direct_engine_calls) == 1 and direct_engine_calls[0][0].name == "engine.py"
    direct_call = direct_engine_calls[0][1]
    assert direct_call.lineno > 0

    engine_tree = trees[source_dir / "engine.py"]
    violation_collector = next(n for n in ast.walk(engine_tree) if isinstance(n, ast.FunctionDef) and n.name == "_engine_violations")
    assert direct_call in list(ast.walk(violation_collector)), "direct Engine.violations calls must stay in the collector"
    tool = next(n for n in ast.walk(engine_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_tool")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_engine_violations" for n in ast.walk(tool))
    run_check_node = next(n for n in ast.walk(engine_tree) if isinstance(n, ast.FunctionDef) and n.name == "run_check")
    report_assignment = next(
        n
        for n in ast.walk(run_check_node)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "violations" and isinstance(t.value, ast.Name) and t.value.id == "report" for t in n.targets)
    )
    assert any(
        isinstance(n, ast.Attribute)
        and n.attr == "violations"
        and isinstance(n.value, ast.Name)
        and n.value.id == "run"
        for n in ast.walk(report_assignment.value)
    ), "EngineRun violations must be added to the report's baseline input"


def test_no_engines_means_only_the_type_checker_runs(repo: Path) -> None:
    commit_generated(repo)
    runner = FakeToolRunner({"ty"})
    report = run_check(mini_policy(), repo, services(runner, engines=[]), Options())
    assert [e.name for e in report.engines] == ["arborescent", "ty"] and report.ok


def test_engine_order_in_the_report_is_stable(repo: Path) -> None:
    commit_generated(repo)
    report = run_check(mini_policy(), repo, services(FakeToolRunner(set(ALL_TOOLS))), Options())
    assert [e.name for e in report.engines] == ["arborescent", "import-linter", "tach", "tach-external", "ruff", "ty"]


def test_changed_files_reach_the_type_checker_only(repo: Path) -> None:
    import subprocess

    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    write_files(repo, {"app/domain.py": "from __future__ import annotations\n\nVALUE: int = 2\n"})
    runner = FakeToolRunner(set(ALL_TOOLS))
    from arborescent.adapters import SubprocessToolRunner

    class Recording(SubprocessToolRunner):
        def available(self, executable: str) -> bool:
            return executable in ALL_TOOLS or super().available(executable)

        def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
            if argv[0] in ALL_TOOLS:
                return runner.run(argv, cwd, timeout)
            return super().run(argv, cwd, timeout)

    svc = Services(FsScanner(), Recording(), CHECKERS, LocalFileSystem(), SystemClock(), engines=default_engines())
    run_check(mini_policy(), repo, svc, Options(changed="HEAD"))
    assert ["tach", "check"] not in runner.calls and ["tach", "check-external"] not in runner.calls and not any(call[0] == "lint-imports" for call in runner.calls) and any(call[0] == "ruff" for call in runner.calls)
