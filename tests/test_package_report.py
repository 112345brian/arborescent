"""``arborescent packages report``: the imports between declared packages, which the annotations do and do not allow.

A package is an extraction unit, and single responsibility implies it can leave the repository: its imports of other
packages are exactly its dependencies. The report states them with their evidence. An observed import that no
``depends_on`` allows is the cut list; packages that import each other cannot be split until it is empty. Nothing is
discovered or planned here: the groups are the ones the catalog declares, and the report only counts what crosses them.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent.adapters import LocalFileSystem
from arborescent.cli import main
from arborescent.graph_adapter import NetworkxCycles
from arborescent.package_dependencies import dependency_report
from arborescent.package_gaps import load_package_context
from arborescent.package_report import DependencyReport
from arborescent.testing import FakeCycleFinder

FS = LocalFileSystem()


def load_networkx() -> ModuleType | None:
    try:
        return importlib.import_module("networkx")
    except ImportError:
        return None


def package(pid: str, paths: list[str], depends: list[str]) -> str:
    owned = "".join(f'[[packages.paths]]\npath = "{p}"\nrole = "source"\n' for p in paths)
    return (
        f'[[packages]]\nid = "{pid}"\nname = "{pid}"\nkind = "library"\nrepository = "example/repo"\nroot = "app"\n'
        f'summary = "{pid}"\ndepends_on = {depends!r}\n{owned}\n'.replace("'", '"')
    )


# core imports api although it does not declare it (the back-edge); api and driver declare what they import.
CATALOG = (
    package("core", ["app/__init__.py", "app/core.py", "app/engine.py"], [])
    + package("annotations", ["app/api.py"], ["core"])
    + package("driver", ["app/shell.py"], ["annotations", "core"])
)
FILES = {
    "app/__init__.py": "",
    "app/core.py": "from app import api\n",
    "app/engine.py": "from app import core\n",
    "app/api.py": "from app import core\n",
    "app/shell.py": "from app import api, engine\n",
    "packages.toml": CATALOG,
}


def report_for(root: Path, files: dict[str, str] | None = None, finder: object = None) -> DependencyReport:
    write_files(root, files or FILES)
    policy = mini_policy(package_catalog="packages.toml")
    scan = services().scanner.scan(policy, root)
    context = load_package_context(policy, root, FS, scan)
    assert context is not None
    return dependency_report(context.catalog, context.facts, finder)  # type: ignore[arg-type]


def edge(report: DependencyReport, source: str, target: str):
    return next(e for e in report.edges if (e.source, e.target) == (source, target))


def test_every_import_between_declared_packages_is_an_edge_with_its_evidence(tmp_path: Path) -> None:
    report = report_for(tmp_path)
    assert [(e.source, e.target) for e in report.edges] == [
        ("annotations", "core"), ("core", "annotations"), ("driver", "annotations"), ("driver", "core"),
    ]
    assert [(u.module, u.imports) for u in edge(report, "core", "annotations").uses] == [("app.core", "app.api")]
    # importing app.engine also imports its parent package `app`, which core owns: a shared __init__ belongs to someone
    assert [(u.module, u.imports) for u in edge(report, "driver", "core").uses] == [("app.shell", "app"), ("app.shell", "app.engine")]


def test_an_import_inside_one_package_is_not_an_edge(tmp_path: Path) -> None:
    report = report_for(tmp_path)  # app.engine imports app.core: both are core's
    assert not any(e.source == e.target for e in report.edges)


def test_an_edge_is_declared_only_when_depends_on_allows_it(tmp_path: Path) -> None:
    report = report_for(tmp_path)
    assert {(e.source, e.target): e.declared for e in report.edges} == {
        ("annotations", "core"): True,
        ("core", "annotations"): False,
        ("driver", "annotations"): True,
        ("driver", "core"): True,
    }


def test_the_cut_list_is_the_undeclared_edges_with_the_module_pairs_to_remove(tmp_path: Path) -> None:
    report = report_for(tmp_path)
    assert [(e.source, e.target) for e in report.cut_list] == [("core", "annotations")]
    assert not report.clean


def test_packages_that_import_each_other_are_a_cycle_until_the_cut_list_is_empty(tmp_path: Path) -> None:
    pytest.importorskip("networkx")
    assert report_for(tmp_path, finder=NetworkxCycles(load_networkx)).cycles == (("annotations", "core"),)


def test_the_cycle_finder_is_asked_about_the_observed_edges_and_its_answer_is_reported(tmp_path: Path) -> None:
    finder = FakeCycleFinder(groups=(("core", "annotations"),))
    report = report_for(tmp_path, finder=finder)
    assert report.cycles == (("annotations", "core"),)
    assert finder.asked == [(("annotations", "core"), ("core", "annotations"), ("driver", "annotations"), ("driver", "core"))]


def test_without_a_graph_library_cycles_are_not_computed_not_guessed(tmp_path: Path) -> None:
    assert report_for(tmp_path).cycles is None  # no finder at all
    assert report_for(tmp_path, finder=FakeCycleFinder(installed=False)).cycles is None  # a finder whose library is missing


def test_with_the_back_edge_removed_there_is_no_cut_list_and_no_cycle(tmp_path: Path) -> None:
    report = report_for(tmp_path, {**FILES, "app/core.py": "x = 1\n"}, FakeCycleFinder())
    assert report.cut_list == () and report.cycles == () and report.clean


def test_a_declared_dependency_that_is_never_imported_is_reported_as_unused(tmp_path: Path) -> None:
    catalog = CATALOG.replace('depends_on = ["annotations", "core"]', 'depends_on = ["annotations", "core", "extras"]') + package("extras", ["app/extra.py"], [])
    report = report_for(tmp_path, {**FILES, "packages.toml": catalog, "app/extra.py": "x = 1\n"})
    assert report.unused == (("driver", "extras"),)


def test_modules_owned_by_no_package_or_by_several_are_left_out(tmp_path: Path) -> None:
    files = {**FILES, "app/loose.py": "from app import api\n"}  # claimed by no package
    report = report_for(tmp_path, files)
    assert not any(use.module == "app.loose" for e in report.edges for use in e.uses)


# -- the command ----------------------------------------------------------------------------------------------------


def with_finder(finder: FakeCycleFinder | None):
    return replace(services(), cycles=finder)


def run(
    tmp_path: Path, *argv: str, capsys: pytest.CaptureFixture[str], finder: FakeCycleFinder | None = None,
    files: dict[str, str] | None = None,
) -> tuple[int, str]:
    write_files(tmp_path, {**(files or FILES), "arborescent.toml": 'package_catalog = "packages.toml"\nsources = ["app"]\nsource_roots = ["."]\n'
                           'rings = ["domain"]\npreset = "strict"\n[ring.domain]\nexternal = ["*"]\n[classify]\n"app*" = "domain"\n'})
    code = main(["--config", str(tmp_path / "arborescent.toml"), "packages", "report", *argv], with_finder(finder), environ={})
    return code, capsys.readouterr().out


def test_the_text_report_lists_edges_the_cut_list_and_cycles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(tmp_path, capsys=capsys, finder=FakeCycleFinder(groups=(("annotations", "core"),)))
    assert code == 0  # a report, not a gate
    assert "core -> annotations" in out and "UNDECLARED" in out
    assert "app.core imports app.api" in out  # the evidence for the edge to cut
    assert "cycle" in out and "annotations, core" in out
    assert "app.shell imports app.engine" not in out  # declared edges show a count, not their evidence, unless asked


def test_the_text_report_says_cycles_were_not_computed_and_what_to_install(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out = run(tmp_path, capsys=capsys, finder=FakeCycleFinder(installed=False))
    assert "cycles: not computed" in out and "fake-graph>=1" in out


def test_all_shows_the_evidence_of_declared_edges_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out = run(tmp_path, "--all", capsys=capsys)
    assert "app.shell imports app.engine" in out


def test_check_exits_one_while_the_cut_list_is_not_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, "--check", capsys=capsys)[0] == 1


def test_check_exits_zero_when_the_annotations_allow_every_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run(tmp_path, "--check", capsys=capsys, files={**FILES, "app/core.py": "x = 1\n"})
    assert code == 0


def test_the_json_report_is_stable_and_complete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out = run(tmp_path, "--json", capsys=capsys, finder=FakeCycleFinder(groups=(("annotations", "core"),)))
    data = json.loads(out)
    assert data["version"] == 1
    assert [(e["source"], e["target"], e["declared"]) for e in data["edges"]][1] == ("core", "annotations", False)
    assert data["edges"][1]["uses"] == [{"module": "app.core", "imports": "app.api"}]
    assert data["cut_list"] == [{"source": "core", "target": "annotations"}]
    assert data["cycles"] == [["annotations", "core"]]
    assert out == json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_a_repository_without_a_catalog_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"app/__init__.py": "", "arborescent.toml": 'sources = ["app"]\nsource_roots = ["."]\nrings = ["domain"]\n[classify]\n"app*" = "domain"\n'})
    assert main(["--config", str(tmp_path / "arborescent.toml"), "packages", "report"], with_finder(None), environ={}) == 2
    assert "package_catalog" in capsys.readouterr().err


# -- cost ----------------------------------------------------------------------------------------------------------


def test_starting_arborescent_does_not_import_the_graph_library() -> None:
    """networkx takes about 115 ms to import. Only `packages report` ever needs it, and only when cycles are asked for."""
    probe = "import sys, arborescent.cli; print('networkx' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
