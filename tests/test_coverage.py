from __future__ import annotations

from pathlib import Path

from archlint_helpers import mini_policy, write_files

from archlint.adapters import FsScanner, LocalFileSystem, SystemClock
from archlint.baseline import save
from archlint.cli import main
from archlint.domain import ToolResult
from archlint.engine import Services, run_own
from archlint.engines import default_engines
from archlint.testing import FakeToolRunner
from archlint.typecheckers import all_checkers


class GitFiles(FakeToolRunner):
    def __init__(self, files: list[str] | None = None, *, available: bool = True) -> None:
        super().__init__({"git"} if available else set())
        self.files = files or []

    def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if argv[0] == "git" and "ls-files" in argv:
            return ToolResult(0, "\n".join(self.files), "")
        return super().run(argv, cwd, timeout)


def make_services(runner: GitFiles) -> Services:
    return Services(FsScanner(runner=runner), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def test_tracked_python_outside_sources_is_unclassified(repo: Path) -> None:
    write_files(repo, {"scripts/extra.py": "VALUE = 1\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "scripts/extra.py"])

    found, scan = run_own(mini_policy(), repo, make_services(runner), practices=False)

    assert "scripts.extra" in scan.known
    assert any(v.rule == "unclassified" and v.subject == "scripts.extra" for v in found)


def test_tracked_python_in_noise_directory_is_reported_without_parsing(repo: Path) -> None:
    write_files(repo, {".venv/lib/python3.13/site-packages/hidden.py": "this is not valid python !!!\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", ".venv/lib/python3.13/site-packages/hidden.py"])

    found, scan = run_own(mini_policy(), repo, make_services(runner), practices=False)

    assert any(v.rule == "noise-dir-code" and v.subject == ".venv/lib/python3.13/site-packages/hidden.py" for v in found)
    assert scan.parsed == 2


def test_git_ignored_file_is_outside_coverage(repo: Path) -> None:
    write_files(repo, {"build/generated.py": "VALUE = 1\n"})
    # `git ls-files --cached --others --exclude-standard` omits ignored build output.
    runner = GitFiles(["app/__init__.py", "app/domain.py"])

    found, scan = run_own(mini_policy(), repo, make_services(runner), practices=False)

    assert "build.generated" not in scan.known
    assert not any(v.rule == "unclassified" and v.subject.startswith("build") for v in found)


def test_no_git_falls_back_to_a_noise_pruned_walk(repo: Path) -> None:
    write_files(repo, {"scripts/extra.py": "VALUE = 1\n", ".tox/ignored.py": "invalid python !!!\n"})
    runner = GitFiles(available=False)

    found, scan = run_own(mini_policy(), repo, make_services(runner), practices=False)

    assert "scripts.extra" in scan.known
    assert not any(".tox" in name for name in scan.known)
    assert any(v.rule == "unclassified" and v.subject == "scripts.extra" for v in found)


def test_discover_groups_unclassified_files_and_suggests_outer_ring(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    write_files(repo, {"scripts/one.py": "VALUE = 1\n", "scripts/two.py": "VALUE = 2\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "scripts/one.py", "scripts/two.py"])
    config = repo / "archlint.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain", "composition"]\n'
        '[ring.domain]\nexternal = []\n[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
        '[classify]\n"app" = "domain"\n"app.*" = "domain"\n',
        encoding="utf-8",
    )
    services = Services(FsScanner(runner=runner), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())

    assert main(["--config", str(config), "discover"], services) == 0

    output = capsys.readouterr().out
    assert "scripts/ (2 file(s))" in output
    assert "scripts/one.py" in output and "scripts/two.py" in output
    assert "suggested ring: composition" in output


def test_check_prints_excluded_file_counts(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    write_files(repo, {"vendor/copied.py": "VALUE = 1\n", "generated/skip.py": "VALUE = 2\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "vendor/copied.py", "generated/skip.py"])
    config = repo / "archlint.toml"
    config.write_text(
        'sources = ["app"]\nexclude = ["generated", "vendor"]\nrings = ["domain"]\n'
        '[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n',
        encoding="utf-8",
    )
    services = Services(FsScanner(runner=runner), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())

    _ = main(["--config", str(config), "check", "--no-tools"], services)

    output = capsys.readouterr().out
    assert "excluded folders" in output
    assert "vendor/ (1 file(s))" in output
    assert "generated/ (1 file(s))" in output


def test_coverage_debt_can_be_adopted_as_a_family(repo: Path) -> None:
    write_files(repo, {"scripts/extra.py": "VALUE = 1\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "scripts/extra.py"])
    config = repo / "archlint.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = ["__future__"]\n'
        '[classify]\n"app" = "domain"\n"app.*" = "domain"\n',
        encoding="utf-8",
    )
    fs = LocalFileSystem()
    services = Services(FsScanner(runner=runner), runner, list(all_checkers().values()), fs, SystemClock(), default_engines())
    save(fs, repo / "archlint-baseline.json", [])

    assert main(["--config", str(config), "baseline", "--adopt", "coverage"], services) == 0

    assert "unclassified|scripts.extra|" in fs.read_text(repo / "archlint-baseline.json")
