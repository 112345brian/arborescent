from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from archlint_helpers import MINI_FILES, mini_policy, write_files

from archlint.adapters import FsScanner, LocalFileSystem, SystemClock
from archlint.engine import Options, Services, run_check, write_generated
from archlint.engines import default_engines
from archlint.suggestions import file_size_suggestions, logical_code_lines
from archlint.testing import FakeToolRunner
from archlint.typecheckers import all_checkers


def test_logical_lines_ignore_comments_docstrings_and_blank_lines() -> None:
    source = '''"""A module description.
that spans lines.
"""
# comment

def f():
    """A function description.
    still documentation.
    """
    # body comment
    return 1
'''
    assert logical_code_lines(source) == 2


def test_logical_lines_ignore_large_data_assignments_but_count_logic() -> None:
    table = "\n".join(f'    "key{i}": {i},' for i in range(30))
    source = (
        f"LOOKUP = {{\n{table}\n}}\n\ndef transform(value):\n    return value * 2\n"
    )
    assert logical_code_lines(source) == 2


def test_file_size_is_a_suggestion_and_threshold_is_configurable(
    tmp_path: Path,
) -> None:
    source = "\n".join(f"value_{i} = {i}" for i in range(12))
    path = tmp_path / "large.py"
    path.write_text(source, encoding="utf-8")

    suggestions = file_size_suggestions(
        LocalFileSystem(), tmp_path, {"large": "large.py"}, threshold=10
    )

    assert len(suggestions) == 1
    assert suggestions[0].path == "large.py"
    assert suggestions[0].logical_lines == 12
    assert (
        file_size_suggestions(
            LocalFileSystem(), tmp_path, {"large": "large.py"}, threshold=20
        )
        == []
    )


def test_check_reports_suggestion_without_failing_or_baselining_it(repo: Path) -> None:
    policy = replace(mini_policy(), suggestion_threshold=1)
    write_files(repo, MINI_FILES)
    fs = LocalFileSystem()
    scanner = FsScanner()
    services = Services(
        scanner,
        FakeToolRunner(),
        list(all_checkers().values()),
        fs,
        SystemClock(),
        default_engines(),
    )
    write_generated(policy, repo, services)

    report = run_check(policy, repo, services, Options(tools=False))

    assert report.ok
    assert report.suggestions
    assert report.ratchet is not None and report.ratchet.new == []
