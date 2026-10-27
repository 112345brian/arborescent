from __future__ import annotations

from pathlib import Path

from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.domain import ToolResult
from arborescent.engine import Services, run_own
from arborescent.engines import default_engines
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers


class GitFiles(FakeToolRunner):
    def __init__(self, files: list[str]) -> None:
        super().__init__({"git"})
        self.files = files

    def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if argv[0] == "git" and "ls-files" in argv:
            return ToolResult(0, "\0".join(self.files), "")
        return super().run(argv, cwd, timeout)


def _services(root: Path, files: list[str]) -> Services:
    runner = GitFiles(files)
    return Services(
        FsScanner(runner=runner), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines()
    )


def _vendor_key(root: Path, files: list[str], policy=None) -> str | None:  # type: ignore[no-untyped-def]
    findings, _ = run_own(policy or mini_policy(), root, _services(root, files), practices=False)
    return next((v.key for v in findings if v.rule == "vendor-folder"), None)


def test_vendor_tree_fingerprint_covers_non_python_files_and_content(repo: Path) -> None:
    write_files(repo, {"vendor/pkg.py": "VALUE = 1\n", "vendor/data.json": '{"v":1}\n'})
    paths = ["app/__init__.py", "app/domain.py", "vendor/pkg.py", "vendor/data.json"]

    before = _vendor_key(repo, paths)
    assert before is not None and before.startswith("vendor-folder|vendor|")

    write_files(repo, {"vendor/data.json": '{"v":2}\n'})
    changed = _vendor_key(repo, paths)
    assert changed is not None and changed != before

    write_files(repo, {"vendor/new.txt": "new file\n"})
    with_new_file = _vendor_key(repo, [*paths, "vendor/new.txt"])
    assert with_new_file is not None and with_new_file != changed

    (repo / "vendor/pkg.py").unlink()
    removed = _vendor_key(repo, ["app/__init__.py", "app/domain.py", "vendor/data.json", "vendor/new.txt"])
    assert removed is not None and removed != with_new_file


def test_explicit_exclude_takes_precedence_over_vendor_convention(repo: Path) -> None:
    write_files(repo, {"vendor/pkg.py": "VALUE = 1\n"})
    policy = mini_policy(exclude=["vendor"])

    findings, scan = run_own(policy, repo, _services(repo, ["app/__init__.py", "app/domain.py", "vendor/pkg.py"]), practices=False)

    assert not any(v.rule == "vendor-folder" for v in findings)
    assert scan.excluded == {"vendor": 1}


def test_vendor_named_folder_inside_noise_directory_is_not_auto_excluded(repo: Path) -> None:
    write_files(repo, {".venv/vendor/copied.py": "VALUE = 1\n"})

    findings, scan = run_own(
        mini_policy(), repo, _services(repo, ["app/__init__.py", "app/domain.py", ".venv/vendor/copied.py"]), practices=False
    )

    assert not any(v.rule == "vendor-folder" for v in findings)
    assert any(v.rule == "noise-dir-code" and v.subject == ".venv/vendor/copied.py" for v in findings)
    assert not any(path.startswith(".venv/vendor") for path in scan.paths.values())


def test_each_vendor_convention_name_is_fingerprinted(repo: Path) -> None:
    for folder in ("vendor", "vendored", "third_party", "site-packages"):
        write_files(repo, {f"{folder}/pkg.py": "VALUE = 1\n"})
        key = _vendor_key(repo, ["app/__init__.py", "app/domain.py", f"{folder}/pkg.py"])
        assert key is not None and key.startswith(f"vendor-folder|{folder}|")


def test_check_lists_auto_excluded_vendor_folders(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    write_files(repo, {"vendor/pkg.py": "VALUE = 1\n"})
    config = repo / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain", "composition"]\n'
        '[ring.domain]\nexternal = []\n[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
        '[classify]\n"app" = "domain"\n',
        encoding="utf-8",
    )
    services = _services(repo, ["app/__init__.py", "app/domain.py", "vendor/pkg.py"])

    assert main(["--config", str(config), "check", "--no-tools", "--no-baseline"], services) == 1

    output = capsys.readouterr().out
    assert "auto-excluded vendor folders:" in output
    assert "  vendor/" in output
