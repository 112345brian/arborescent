"""#83: a moved module that builds paths from ``__file__`` can silently look in the wrong place after the move."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_driver_plan import CLEAN, SITE, build
from test_migration_arguments import (
    OWN_REPO,
    RENAME,
    RENAME_CATALOG,
    RENAME_FACTS,
    _destination,
)
from test_migration_export import CLI_CATALOG, _repo

from arborescent.adapters import FsScanner
from arborescent.cli import main
from arborescent.driver_plan import plan_supply
from arborescent.facts import extract
from arborescent.migration_arguments import GapCode, build_migration_arguments
from arborescent.package_parse import parse_catalog
from arborescent.testing import FakeMigrationLibrary, FakeToolRunner

CODE = "package-file-relative-path"


# -- the fact ------------------------------------------------------------------------------------------------------------

def test_every_use_of_dunder_file_is_a_line_fact() -> None:
    source = "from pathlib import Path\nHERE = Path(__file__).resolve().parent\n\ndef f() -> str:\n    return __file__\n"
    assert extract(source).file_refs == (2, 5)


@pytest.mark.parametrize("source", ["x = 1\n", "x = '__file__'\n", "# __file__\n", "x = obj.__file__\n"])
def test_a_string_a_comment_or_an_attribute_is_not_a_use(source: str) -> None:
    assert extract(source).file_refs == ()


def test_the_fact_survives_the_scan_cache(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/__init__.py": "", "app/domain.py": "from pathlib import Path\nP = Path(__file__).parent\n"})
    policy = mini_policy()
    cold = FsScanner(tmp_path / "cache").scan(policy, tmp_path)
    warm = FsScanner(tmp_path / "cache").scan(policy, tmp_path)
    assert warm.parsed == 0 and cold.parsed > 0
    assert warm.modules["app.domain"].facts.file_refs == cold.modules["app.domain"].facts.file_refs == (2,)


# -- the gap -------------------------------------------------------------------------------------------------------------

def _gaps(facts_refs: dict[str, int], text: str | None = None, base=RENAME_FACTS):
    text = text or RENAME_CATALOG.format(kind="repository", home='repository = "example/core"', renames=RENAME, destination=_destination("core"))
    arguments = build_migration_arguments(parse_catalog(tomllib.loads(text)), replace(base, file_refs=facts_refs))
    return arguments, [g for g in arguments.gaps if g.code == CODE]


def test_a_moved_module_that_uses_dunder_file_is_flagged_for_review_without_blocking() -> None:
    arguments, gaps = _gaps({"app.core.models": 2})
    assert [(g.subject, g.severity, g.package) for g in gaps] == [("app.core.models", "info", "core")]
    assert "2 " in gaps[0].detail and "app/core/models.py" in gaps[0].detail and "core/models.py" in gaps[0].detail
    assert not [g for g in arguments.gaps if g.code == CODE and g.severity == "error"]  # it never blocks a migration


def test_a_module_that_did_not_move_or_uses_nothing_is_not_flagged() -> None:
    unmoved = RENAME_CATALOG.format(kind="repository", home=OWN_REPO.split("\n", 1)[1], renames="", destination=_destination("app/core"))
    assert _gaps({"app.core.models": 3}, unmoved)[1] == []
    assert _gaps({})[1] == []
    assert _gaps({"app.core.models": 0})[1] == []


def test_a_module_no_owned_path_places_is_left_to_the_unplaced_rules() -> None:
    assert _gaps({"app.other": 1}, base=replace(RENAME_FACTS, modules={**RENAME_FACTS.modules, "app.other": "app/other.py"}))[1] == []


def test_the_gap_code_is_documented_and_listed() -> None:
    assert GapCode.FILE_RELATIVE_PATH == CODE and CODE in GapCode.ALL
    assert CODE in Path("docs/migration-export.md").read_text(encoding="utf-8")


# -- the plan the driver prints ------------------------------------------------------------------------------------------

def test_the_plan_carries_the_review_items_for_its_own_package_only() -> None:
    facts = replace(CLEAN, file_refs={"app.core.api": 1, "app.web.views": 4})
    plan = plan_supply(build(facts=facts), "core", FakeMigrationLibrary(), SITE)
    assert [g.subject for g in plan.reviews] == ["app.core.api"]


def test_the_dry_run_lists_what_to_review_before_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, CLI_CATALOG.replace('destination = "app"', 'destination = "moved"').replace("[[packages.paths]]", '[[packages.module_renames]]\nfrom = "app"\nto = "moved"\n[[packages.paths]]', 1))
    write_files(tmp_path, {"app/domain.py": "from pathlib import Path\nimport pydantic\nDATA = Path(__file__).parent / 'data'\n"})
    argv = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "core", "--target", str(tmp_path / "out")]
    assert main(argv, services(FakeToolRunner()), library=FakeMigrationLibrary()) == 0
    out = capsys.readouterr().out
    assert "review before --yes" in out and f"[{CODE}] app.domain" in out
