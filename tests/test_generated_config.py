"""import-linter reads the config ``generate`` writes for it, but never overrides one the project wrote itself.

``generate`` only creates missing files and ``drift`` only inspects files that carry the generated header. The read side
follows the same rule: a project's own import-linter configuration is intentional and always wins; Arborescent's file is
used only when it is Arborescent's (it carries the header) and nothing else is configured. That removes the need to keep
a hand-written copy of the contracts beside the generated ones, without ignoring a copy someone chose to keep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent.adapters import LocalFileSystem
from arborescent.engine import Options, run_check, write_generated
from arborescent.engines import ImportLinter
from arborescent.generated_config import (
    GENERATED_MARKER,
    HEADER,
    has_own_importlinter_config,
    is_generated,
)
from arborescent.testing import FakeToolRunner

FS = LocalFileSystem()
GENERATED = "arborescent-importlinter.toml"
TOOLS = {"lint-imports", "tach", "tach-external", "ruff", "ty"}


def lint_imports_calls(runner: FakeToolRunner) -> list[list[str]]:
    return [call for call in runner.calls if call[0] == "lint-imports"]


def run(repo: Path) -> FakeToolRunner:
    runner = FakeToolRunner(set(TOOLS))
    run_check(mini_policy(), repo, services(runner), Options())
    return runner


def test_the_marker_is_the_start_of_the_header_generate_writes() -> None:
    assert HEADER.startswith(GENERATED_MARKER)


# -- is it ours --------------------------------------------------------------------------------------------------


def test_only_a_file_with_the_generated_header_is_ours(tmp_path: Path) -> None:
    write_files(tmp_path, {"ours.toml": HEADER + "[tool.importlinter]\n", "theirs.toml": "[tool.importlinter]\n", "empty.toml": ""})
    assert is_generated(FS, tmp_path / "ours.toml")
    assert not is_generated(FS, tmp_path / "theirs.toml")
    assert not is_generated(FS, tmp_path / "empty.toml")
    assert not is_generated(FS, tmp_path / "missing.toml")


# -- does the project have import-linter config of its own --------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({}, False),
        ({".importlinter": "[importlinter]\nroot_package = x\n"}, True),
        ({"pyproject.toml": "[tool.importlinter]\nroot_packages = ['x']\n"}, True),
        ({"pyproject.toml": "[tool.importlinter.contracts]\n"}, True),
        ({"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}, False),  # a pyproject with no import-linter table is not config
        ({"setup.cfg": "[importlinter]\nroot_package = x\n"}, True),
        ({"setup.cfg": "[flake8]\nmax-line-length = 100\n"}, False),
        ({"tox.ini": "[importlinter]\nroot_package = x\n"}, True),
        ({GENERATED: HEADER + "[tool.importlinter]\n"}, False),  # the generated file is not the project's own config
    ],
)
def test_the_projects_own_import_linter_config_is_found_wherever_import_linter_looks(
    tmp_path: Path, files: dict[str, str], expected: bool
) -> None:
    write_files(tmp_path, files)
    assert has_own_importlinter_config(FS, tmp_path) is expected


# -- what check runs ----------------------------------------------------------------------------------------------


def test_the_generated_file_is_used_when_it_is_ours_and_nothing_else_is_configured(repo: Path) -> None:
    write_generated(mini_policy(), repo, services(FakeToolRunner(set(TOOLS))))
    assert (repo / GENERATED).read_text(encoding="utf-8").startswith(GENERATED_MARKER)
    assert lint_imports_calls(run(repo)) == [["lint-imports", "--config", GENERATED]]


@pytest.mark.parametrize(
    "own",
    [
        {".importlinter": "[importlinter]\nroot_package = app\n"},
        {"pyproject.toml": "[tool.importlinter]\nroot_packages = ['app']\n"},
        {"setup.cfg": "[importlinter]\nroot_package = app\n"},
        {"tox.ini": "[importlinter]\nroot_package = app\n"},
    ],
)
def test_a_project_that_configured_import_linter_itself_keeps_its_config(repo: Path, own: dict[str, str]) -> None:
    write_generated(mini_policy(), repo, services(FakeToolRunner(set(TOOLS))))
    write_files(repo, own)
    assert lint_imports_calls(run(repo)) == [["lint-imports"]]  # no --config: import-linter reads the project's own


def test_a_hand_written_file_with_the_generated_name_is_never_adopted(repo: Path) -> None:
    write_files(repo, {GENERATED: "[tool.importlinter]\nroot_packages = ['app']\n"})  # no header: someone's own
    assert lint_imports_calls(run(repo)) == [["lint-imports"]]


def test_without_a_generated_file_the_command_is_unchanged(repo: Path) -> None:
    assert lint_imports_calls(run(repo)) == [["lint-imports"]]


def test_generate_never_rewrites_an_existing_file_of_that_name(repo: Path) -> None:
    write_files(repo, {GENERATED: "# mine\n"})
    written = write_generated(mini_policy(), repo, services(FakeToolRunner(set(TOOLS))))
    assert GENERATED not in written
    assert (repo / GENERATED).read_text(encoding="utf-8") == "# mine\n"


def test_the_adapter_alone_does_not_change_its_plain_command() -> None:
    assert ImportLinter().command([]) == ["lint-imports"]
