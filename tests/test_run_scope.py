"""The collector threshold is raised while the checks run and is always put back (``raised_gc_threshold``)."""

from __future__ import annotations

import ast
import gc
import pickle
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services

from arborescent.adapters import ProcessPoolParallel, raised_gc_threshold
from arborescent.check_runner import run_check
from arborescent.cli import default_services
from arborescent.engine import Options, Services, run_own
from arborescent.parallel_adapter import GC_THRESHOLD
from arborescent.scanner import FsScanner
from arborescent.selftest import selftest

SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"


@pytest.fixture(autouse=True)
def original() -> Generator[tuple[int, ...]]:
    """A known caller setting; read back, since an incremental collector reports its ignored third value as 0."""
    before, enabled = gc.get_threshold(), gc.isenabled()
    gc.enable()
    gc.set_threshold(701, 11, 12)
    try:
        yield gc.get_threshold()
    finally:
        gc.set_threshold(*before)
        (gc.enable if enabled else gc.disable)()


class SeeingScanner(FsScanner):
    """Records the threshold in force while a scan runs, and can fail there."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.seen: list[int] = []
        self.fail = fail

    def scan(self, policy, root, only=None):  # type: ignore[no-untyped-def]
        self.seen.append(gc.get_threshold()[0])
        if self.fail:
            raise RuntimeError("scan failed")
        return super().scan(policy, root, only)


def scoped(scanner: FsScanner) -> Services:
    return replace(services(), scanner=scanner, run_scope=raised_gc_threshold)


def run(name: str, repo: Path, svc: Services) -> None:
    if name == "run_own":
        _ = run_own(mini_policy(), repo, svc, practices=False)
    else:
        _ = run_check(mini_policy(), repo, svc, Options(tools=False))


def test_raises_inside_and_restores_afterwards(original: tuple[int, ...]) -> None:
    with raised_gc_threshold():
        assert gc.get_threshold() == (GC_THRESHOLD, *original[1:])
    assert gc.get_threshold() == original


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_restores_after_an_exception(original: tuple[int, ...], error: type[BaseException]) -> None:
    with pytest.raises(error), raised_gc_threshold():
        raise error
    assert gc.get_threshold() == original


def test_nested_runs_restore_only_at_the_outermost_exit(original: tuple[int, ...]) -> None:
    with raised_gc_threshold():
        with pytest.raises(RuntimeError), raised_gc_threshold():
            raise RuntimeError
        assert gc.get_threshold()[0] == GC_THRESHOLD
    assert gc.get_threshold() == original


@pytest.mark.parametrize("threshold", [0, GC_THRESHOLD, GC_THRESHOLD * 2])
def test_leaves_a_zero_or_already_high_threshold_alone(threshold: int) -> None:
    gc.set_threshold(threshold, 11, 12)
    before = gc.get_threshold()
    with raised_gc_threshold():
        assert gc.get_threshold() == before
    assert gc.get_threshold() == before


def test_leaves_a_disabled_collector_alone(original: tuple[int, ...]) -> None:
    gc.disable()
    with raised_gc_threshold():
        assert gc.get_threshold() == original
        assert not gc.isenabled()
    assert gc.get_threshold() == original
    assert not gc.isenabled()


@pytest.mark.parametrize("threshold", [0, 123, GC_THRESHOLD * 2])
def test_keeps_a_threshold_set_by_someone_else_during_the_run(threshold: int) -> None:
    with raised_gc_threshold():
        gc.set_threshold(threshold)
    assert gc.get_threshold()[0] == threshold


def test_overlapping_scopes_that_end_out_of_order_restore_the_original(original: tuple[int, ...]) -> None:
    first, second = raised_gc_threshold(), raised_gc_threshold()
    first.__enter__()
    second.__enter__()
    _ = first.__exit__(None, None, None)
    assert gc.get_threshold() == original
    _ = second.__exit__(None, None, None)
    assert gc.get_threshold() == original


def test_concurrent_threads_restore_the_original(original: tuple[int, ...]) -> None:
    barrier = threading.Barrier(8)
    finished: list[int] = []

    def work(number: int) -> None:
        _ = barrier.wait()
        for _ in range(500):
            with raised_gc_threshold():
                pass
        finished.append(number)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(finished) == 8
    assert gc.get_threshold() == original


@pytest.mark.parametrize("name", ["run_own", "run_check"])
def test_the_checks_run_raised_and_restore(repo: Path, original: tuple[int, ...], name: str) -> None:
    scanner = SeeingScanner()
    run(name, repo, scoped(scanner))
    assert scanner.seen == [GC_THRESHOLD]
    assert gc.get_threshold() == original


@pytest.mark.parametrize("name", ["run_own", "run_check"])
def test_a_failing_run_restores(repo: Path, original: tuple[int, ...], name: str) -> None:
    svc = scoped(SeeingScanner(fail=True))
    for _ in range(2):  # and again after the first failure
        with pytest.raises(RuntimeError, match="scan failed"):
            run(name, repo, svc)
        assert gc.get_threshold() == original


def test_the_scope_does_not_change_what_a_run_returns(repo: Path) -> None:
    _ = (repo / "app" / "domain.py").write_text("from . import adapters\n", encoding="utf-8")
    with_scope = replace(services(), run_scope=raised_gc_threshold)
    found, scan = run_own(mini_policy(), repo, with_scope)
    plain_found, plain_scan = run_own(mini_policy(), repo, services())
    assert found and found == plain_found
    assert scan.known == plain_scan.known
    report = run_check(mini_policy(), repo, with_scope, Options(tools=False))
    plain = run_check(mini_policy(), repo, services(), Options(tools=False))
    assert (report.ok, report.violations) == (plain.ok, plain.violations)


@pytest.mark.parametrize("name", ["run_own", "run_check"])
def test_services_without_a_scope_leave_the_collector_alone(repo: Path, original: tuple[int, ...], name: str) -> None:
    scanner = SeeingScanner()
    run(name, repo, replace(services(), scanner=scanner))
    assert scanner.seen == [original[0]]


def test_a_check_enters_the_scope_around_the_whole_run(repo: Path) -> None:
    entered: list[str] = []

    @contextmanager
    def recording() -> Generator[None]:
        entered.append("in")
        try:
            yield
        finally:
            entered.append("out")

    run("run_check", repo, replace(services(), run_scope=recording))
    assert entered == ["in", "in", "out", "out"]  # run_check, and the run_own inside it


class Watching(FsScanner):
    """Picklable, so a worker process can use it: fails a scan that runs at any other threshold."""

    def scan(self, policy, root, only=None):  # type: ignore[no-untyped-def]
        if gc.get_threshold()[0] != GC_THRESHOLD:
            raise AssertionError(f"scanned at {gc.get_threshold()}")
        return super().scan(policy, root, only)

    def uncached(self) -> Watching:
        return Watching()


@pytest.mark.parametrize("min_jobs", [1, 10_000], ids=["worker processes", "in process"])
def test_selftest_scans_raised_in_workers_and_in_process(repo: Path, original: tuple[int, ...], min_jobs: int) -> None:
    svc = replace(scoped(Watching()), parallel=ProcessPoolParallel(min_jobs))
    assert selftest(mini_policy(), repo, svc).ok
    assert gc.get_threshold() == original


def test_the_command_line_services_use_it_and_can_cross_to_a_worker(tmp_path: Path) -> None:
    svc = default_services(tmp_path)
    assert svc.run_scope is raised_gc_threshold
    assert pickle.loads(pickle.dumps(svc)).run_scope is raised_gc_threshold


def test_every_services_built_by_the_package_names_a_run_scope() -> None:
    """Leaving ``run_scope`` out fails nothing at run time, it is only slower, so the constructors are scanned."""
    calls = [
        (path.name, node)
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Services"
    ]
    assert [name for name, _ in calls] == ["cli.py"]
    for name, node in calls:
        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        assert any(isinstance(value, ast.Name) and value.id == "raised_gc_threshold" for value in values), name
