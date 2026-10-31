"""#67: the driver's application ring turns one package's migration arguments into the library's request, through a port.

The library is an in-memory fake (``arborescent.testing.FakeMigrationLibrary``); nothing here patches a module path.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_migration_arguments import CATALOG, build, package

from arborescent.domain import PolicyError
from arborescent.driver_plan import BLOCKED, READY, STAYS, plan_supply, supply
from arborescent.driver_request import MigrationOutcome, MigrationSite
from arborescent.migration_arguments import Gap
from arborescent.testing import FakeMigrationLibrary

SITE = MigrationSite(Path("/mono"), Path("/out/core"), "main")


def core_plan(library: FakeMigrationLibrary, text: str = CATALOG):
    return plan_supply(build(text), "core", library, SITE)


def test_a_ready_package_becomes_a_request_made_only_of_its_migration_arguments() -> None:
    library = FakeMigrationLibrary()
    result = core_plan(library)
    core = package(build(), "core")
    assert result.status == READY
    assert result.request is not None
    assert (result.request.package, result.request.source, result.request.target, result.request.branch) == ("core", SITE.source, SITE.target, SITE.branch)
    assert result.request.path_renames == core.path_renames
    assert result.request.module_renames == core.module_renames
    assert result.arguments == library.arguments(result.request)  # what a dry run prints is what the library would receive


def test_planning_never_calls_the_library() -> None:
    library = FakeMigrationLibrary()
    _ = core_plan(library)
    assert library.supplied == []


def test_supplying_calls_the_library_once_with_the_planned_request() -> None:
    library = FakeMigrationLibrary(outcome=MigrationOutcome(True, "done"))
    result = core_plan(library)
    outcome = supply(result, library)
    assert library.supplied == [result.request]
    assert outcome == MigrationOutcome(True, "done")


def test_a_library_failure_is_returned_not_swallowed() -> None:
    library = FakeMigrationLibrary(outcome=MigrationOutcome(False, "boom"))
    assert supply(core_plan(library), library) == MigrationOutcome(False, "boom")


def test_the_arguments_the_library_cannot_apply_are_reported_with_the_plan() -> None:
    library = FakeMigrationLibrary(unapplied=("module renames",))
    assert core_plan(library).unapplied == ("module renames",)


def test_an_open_error_gap_of_the_package_blocks_the_hand_over() -> None:
    library = FakeMigrationLibrary()
    result = core_plan(library, CATALOG.replace('externals = ["pydantic"]\n', ""))  # core imports pydantic, now undeclared
    assert result.status == BLOCKED
    assert result.request is None
    assert [gap.code for gap in result.gaps] == ["package-undeclared-external"]
    assert supply(result, library).ok is False
    assert library.supplied == []


def test_a_gap_of_another_package_does_not_block_this_one() -> None:
    library = FakeMigrationLibrary()
    result = core_plan(library, CATALOG.replace('externals = ["flask"]\n', ""))  # web, not core, has the gap
    assert any(gap.package == "web" and gap.severity == "error" for gap in build(CATALOG.replace('externals = ["flask"]\n', "")).gaps)
    assert result.status == READY


def test_an_error_gap_that_belongs_to_no_package_blocks_every_package() -> None:
    library = FakeMigrationLibrary()
    arguments = build()
    arguments = replace(arguments, gaps=(*arguments.gaps, Gap("package-unassigned-module", "app.x", "owned by no package", "error")))
    result = plan_supply(arguments, "core", library, SITE)
    assert result.status == BLOCKED


def test_an_info_gap_does_not_block() -> None:
    result = plan_supply(build(), "web", FakeMigrationLibrary(), SITE)
    assert result.status != BLOCKED  # web only carries the informational package-spans-rings gap


def test_a_workspace_member_stays_in_this_repository_and_nothing_is_supplied() -> None:
    library = FakeMigrationLibrary()
    result = plan_supply(build(), "web", library, SITE)
    assert result.status == STAYS
    assert "packages/web" in result.reason
    assert result.request is None
    assert supply(result, library).ok is False
    assert library.supplied == []


def test_an_unknown_package_is_a_policy_error() -> None:
    with pytest.raises(PolicyError, match="nope"):
        _ = plan_supply(build(), "nope", FakeMigrationLibrary(), SITE)


def test_a_missing_library_is_a_failed_outcome_not_a_call() -> None:
    library = FakeMigrationLibrary(installed=False)
    outcome = supply(core_plan(library), library)
    assert outcome.ok is False
    assert library.supplied == []
