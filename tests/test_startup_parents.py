"""#216: importing a module loads its parent packages, so every startup number counts them.

The flat project (no packages) pins what each user of the closure does with it, which parent packages must not change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from arborescent_helpers import services, write_files

from arborescent.cli import main

FLAT = {
    "cli.py": "import hub\n\n\ndef f() -> None:\n    import later\n",
    "hub.py": "import dep\n",
    "dep.py": "",
    "later.py": "",
    "command.py": "import hub\nimport heavy\n",
    "heavy.py": "",
}
FLAT_EXTRA = '[coupling.closure_max]\ncli = 2\n[coupling.lazy_mounts]\nrun = "command:app"\n'


def repo(tmp_path: Path, files: Mapping[str, str], source: str, entrypoint: str, extra: str = "") -> list[str]:
    write_files(tmp_path, files)
    config = tmp_path / "arborescent.toml"
    config.write_text(
        f'sources = ["{source}"]\nrings = ["domain"]\n[ring.domain]\nexternal = ["*"]\n[classify]\n"*" = "domain"\n'
        f'[coupling]\nentrypoints = ["{entrypoint}"]\n' + extra,
        encoding="utf-8",
    )
    return ["--config", str(config), "coupling"]


def output(base: list[str], capsys: pytest.CaptureFixture[str], *args: str, code: int = 0) -> str:
    _ = capsys.readouterr()
    assert main([*base, *args], services()) == code
    return capsys.readouterr().out


def closures(base: list[str], capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(output(base, capsys, "report", "--json"))["closures"]


def flat(tmp_path: Path, extra: str = "") -> list[str]:
    return repo(tmp_path, FLAT, ".", "cli", extra)


def test_the_ratchet_records_and_checks_the_closure_size(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = flat(tmp_path)
    _ = output(base, capsys, "baseline")
    assert json.loads((tmp_path / "arborescent-coupling.json").read_text(encoding="utf-8"))["closure"] == {"cli": 3}
    assert "OK" in output(base, capsys, "check")
    (tmp_path / "hub.py").write_text("import dep\nimport heavy\n", encoding="utf-8")
    assert "closure: cli grew from 3 to 4" in output(base, capsys, "check", code=1)


def test_the_budget_failure_gives_the_size_and_blame(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = flat(tmp_path, FLAT_EXTRA)
    _ = output(base, capsys, "baseline")
    out = output(base, capsys, "check", code=1)
    assert "startup budget: cli loads 3 project modules, budget 2" in out
    assert "cli -> hub pulls 2 module(s)" in out


def test_report_startup_json_and_cost_agree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = flat(tmp_path, FLAT_EXTRA)
    assert closures(base, capsys) == [{"entrypoint": "cli", "modules": 3, "function_level_only": 1, "type_only": 0}]
    assert "cli: 3 (function-level only +1, type-only +0)" in output(base, capsys, "report")
    out = output(base, capsys, "startup")
    assert "cli: 3 project modules (budget 2, OVER)" in out
    assert "lazy mount run -> command: +2 module(s) when run" in out
    assert "cli: closure 3 -> 5 (+2)" in output(base, capsys, "cost", "hub", "--modules", "1", "--imports", "heavy")
    assert "no declared entrypoint" in output(base, capsys, "cost", "heavy")


# `app/__init__` loads `app.heavy` whenever any submodule is imported, though no import in `app.cli` says so.
PACKAGED = {
    "app/__init__.py": "import app.heavy\n",
    "app/cli.py": "",
    "app/heavy.py": "",
}


def packaged(tmp_path: Path, files: Mapping[str, str] | None = None, extra: str = "") -> list[str]:
    return repo(tmp_path, {**PACKAGED, **(files or {})}, "app", "app.cli", extra)


def test_the_closure_counts_parent_packages_and_what_they_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = packaged(tmp_path, extra='[coupling.closure_max]\n"app.cli" = 1\n')
    assert closures(base, capsys) == [{"entrypoint": "app.cli", "modules": 3, "function_level_only": 0, "type_only": 0}]
    _ = output(base, capsys, "baseline")
    assert json.loads((tmp_path / "arborescent-coupling.json").read_text(encoding="utf-8"))["closure"] == {"app.cli": 3}
    out = output(base, capsys, "check", code=1)
    assert "startup budget: app.cli loads 3 project modules, budget 1" in out
    assert "app -> app.heavy pulls 1 module(s)" in out
    assert "app.cli -> app " not in out  # a parent package is not an import that can be cut
    assert "app.cli: 3 project modules (budget 1, OVER)" in output(base, capsys, "startup")


def test_a_baseline_recorded_without_parents_now_reports_growth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = packaged(tmp_path)
    (tmp_path / "arborescent-coupling.json").write_text('{"fan-in": {}, "closure": {"app.cli": 1}, "growth": []}', encoding="utf-8")
    assert "closure: app.cli grew from 1 to 3" in output(base, capsys, "check", code=1)


def test_every_ancestor_counts_and_a_folder_without_init_loads_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {"app/cli.py": "import app.a.b.c\nimport app.ns.leaf\n", "app/a/__init__.py": "", "app/a/b/__init__.py": "",
             "app/a/b/c.py": "", "app/ns/leaf.py": ""}
    assert closures(packaged(tmp_path, files), capsys) == [
        {"entrypoint": "app.cli", "modules": 7, "function_level_only": 0, "type_only": 0}  # app, heavy, cli, a, a.b, c, leaf
    ]


def test_a_loaded_parent_is_never_counted_as_lazy_or_type_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/__init__.py": "def f() -> None:\n    import app.later\n",
        "app/cli.py": (
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import app\n    import app.types\n\n\n"
            "def f() -> None:\n    import app\n    from app.sub import leaf\n"
        ),
        "app/later.py": "", "app/types.py": "", "app/sub/__init__.py": "", "app/sub/leaf.py": "",
    }
    # lazy: app.later (the package's own function-level import) and app.sub.leaf, as written; `app` is already loaded
    assert closures(packaged(tmp_path, files), capsys) == [
        {"entrypoint": "app.cli", "modules": 2, "function_level_only": 2, "type_only": 1}
    ]


def test_blame_lists_only_imports_whose_removal_shrinks_the_closure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/__init__.py": "",
        "app/cli.py": "import app.sub.one\nimport app.sub.two\n",
        "app/sub/__init__.py": "import app.heavy\n",
        "app/sub/one.py": "from app import sub\n",  # importing its own package: cutting it removes nothing
        "app/sub/two.py": "",
    }
    out = output(packaged(tmp_path, files), capsys, "startup")
    assert "app.cli: 6 project modules" in out
    assert "app.sub -> app.heavy pulls 1 module(s)" in out
    assert "app.cli -> app.sub.one pulls 1 module(s)" in out  # not the package, which `two` still needs
    assert "-> app.sub pulls" not in out


def test_cutting_the_only_import_into_a_package_removes_the_package_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {"app/__init__.py": "", "app/cli.py": "import app.sub.one\n", "app/sub/__init__.py": "import app.heavy\n", "app/sub/one.py": ""}
    assert "app.cli -> app.sub.one pulls 3 module(s)" in output(packaged(tmp_path, files), capsys, "startup")


def test_mounts_and_new_module_cost_agree_with_the_closure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/__init__.py": "import app.heavy\n",
        "app/cmd/__init__.py": "import app.cmd.shared\n",
        "app/cmd/run.py": "",
        "app/cmd/shared.py": "",
    }
    base = packaged(tmp_path, files, '[coupling.lazy_mounts]\nrun = "app.cmd.run:app"\n')
    assert "lazy mount run -> app.cmd.run: +3 module(s) when run" in output(base, capsys, "startup")  # cmd, shared, run
    # `app` is loaded by the entrypoint, so modules added behind it count, with the packages of what they import
    assert "app.cli: closure 3 -> 7 (+4)" in output(base, capsys, "cost", "app", "--modules", "1", "--imports", "app.cmd.run")


def test_a_mount_loaded_only_by_a_parent_package_is_eager(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = packaged(tmp_path, extra='[coupling.lazy_mounts]\nheavy = "app.heavy:app"\n')
    assert "heavy -> app.heavy: EAGER" in output(base, capsys, "startup")
    _ = output(base, capsys, "baseline")
    assert "app.cli -> app -> app.heavy" in output(base, capsys, "check", code=1)
