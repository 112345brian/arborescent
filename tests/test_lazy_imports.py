"""Function-level imports as a finding category with a ``# deferred: <reason>`` convention (#149)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_baseline_ratchet import write_policy

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.engine import Options, Services, run_check
from arborescent.engines import default_engines
from arborescent.facts import ModuleInfo, SourceFacts, extract
from arborescent.lazy_imports import (
    LazyImportCount,
    lazy_import_counts,
    lazy_import_violations,
)
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers


def _info(source: str) -> ModuleInfo:
    return ModuleInfo("app.sample", "app/sample.py", False, extract(source))


def _deferred(source: str) -> dict[int, str]:
    return dict(extract(source).deferred)


def test_a_trailing_comment_justifies_its_own_line() -> None:
    source = "def f() -> None:\n    import json  # deferred: startup cost\n"
    assert _deferred(source) == {2: "startup cost"}


def test_a_comment_alone_above_justifies_the_next_code_line() -> None:
    source = "def f() -> None:\n    # deferred: the registry imports this module\n    import json\n"
    assert _deferred(source) == {3: "the registry imports this module"}


def test_continuation_comment_lines_keep_the_justification_and_a_blank_line_ends_it() -> None:
    kept = "def f() -> None:\n    # deferred: a long reason\n    # that goes on\n    import json\n"
    assert _deferred(kept) == {4: "a long reason"}
    ended = "def f() -> None:\n    # deferred: a reason\n\n    import json\n"
    assert _deferred(ended) == {}


def test_an_empty_reason_justifies_nothing() -> None:
    assert _deferred("def f() -> None:\n    import json  # deferred:\n") == {}
    assert _deferred("def f() -> None:\n    import json  # deferred:   \n") == {}


def test_a_trailing_comment_on_the_previous_statement_does_not_leak_to_the_import() -> None:
    source = "def f() -> None:\n    x = 1  # deferred: about x\n    import json\n"
    assert _deferred(source) == {2: "about x"}
    assert lazy_import_violations(_info(source)) != []


def test_the_reason_may_follow_another_comment() -> None:
    assert _deferred("def f() -> None:\n    import json  # noqa: PLC0415  # deferred: slow\n") == {2: "slow"}


def test_a_string_that_looks_like_the_comment_is_not_one() -> None:
    assert extract('X = "# deferred: nope"\n').deferred == ()


def test_an_unjustified_function_level_import_is_a_finding_keyed_by_target_and_ordinal() -> None:
    source = "import os\n\n\ndef f() -> None:\n    import json\n    import json\n    from . import sibling\n"
    keys = [v.key for v in lazy_import_violations(_info(source))]
    assert keys == ["lazy-import|app.sample|json#1", "lazy-import|app.sample|json#2", "lazy-import|app.sample|.#1"]


def test_the_key_does_not_move_with_the_line() -> None:
    before = lazy_import_violations(_info("def f() -> None:\n    import json\n"))
    after = lazy_import_violations(_info("\n\n\ndef f() -> None:\n    import json\n"))
    assert [v.key for v in before] == [v.key for v in after]
    assert before[0].line != after[0].line


def test_a_justified_import_is_counted_but_not_a_finding() -> None:
    source = "def f() -> None:\n    import json  # deferred: why\n    import re\n"
    info = _info(source)
    assert [v.detail for v in lazy_import_violations(info)] == ["re#1"]
    assert lazy_import_counts(info) == LazyImportCount(total=2, deferred=1)


def test_type_checking_and_module_level_imports_are_not_function_level() -> None:
    source = (
        "from typing import TYPE_CHECKING\nimport os\n\nif TYPE_CHECKING:\n    import json\n\n\n"
        "def f() -> None:\n    if TYPE_CHECKING:\n        import re\n"
    )
    info = _info(source)
    assert lazy_import_violations(info) == []
    assert lazy_import_counts(info) == LazyImportCount(0, 0)


def test_a_lambda_and_a_method_count_as_function_level() -> None:
    source = "class C:\n    def m(self) -> None:\n        import json\n\n\nF = lambda: __import__('json')\n"
    assert [v.detail for v in lazy_import_violations(_info(source))] == ["json#1"]


def test_a_module_with_no_imports_is_empty() -> None:
    info = ModuleInfo("app.empty", "app/empty.py", False, SourceFacts((), (), None))
    assert lazy_import_violations(info) == []
    assert lazy_import_counts(info) == LazyImportCount(0, 0)


def test_deferred_survives_a_warm_cache_scan(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/a.py": "def f() -> None:\n    import json  # deferred: cached\n"})
    scanner = FsScanner(tmp_path / ".arborescent")
    cold = scanner.scan(mini_policy(), tmp_path)
    warm = scanner.scan(mini_policy(), tmp_path)
    assert (cold.parsed, warm.parsed) == (1, 0)
    assert warm.modules["app.a"].facts.deferred == cold.modules["app.a"].facts.deferred == ((2, "cached"),)


def test_check_reports_unjustified_imports_and_counts_every_one(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "def f() -> None:\n    import json\n    import re  # deferred: why\n"})
    report = run_check(mini_policy(), repo, services(), Options(tools=False))
    assert [v.key for v in report.violations if v.rule == "lazy-import"] == ["lazy-import|app.adapters|json#1"]
    assert report.lazy_import_counts == {"app.adapters": LazyImportCount(total=2, deferred=1)}
    assert not report.ok


def _svc() -> Services:
    return Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def test_a_baselined_import_is_tolerated_and_a_new_one_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    write_files(repo, {"app/adapters.py": "def f() -> None:\n    import os\n"})
    svc = _svc()
    assert main(["--config", str(config), "baseline", "--allow-growth", "--reason", "existing", "--approved-by", "t"], svc) == 0
    _ = capsys.readouterr()
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 0
    assert "function-level imports by module:\n  app.adapters: 1 (0 with `# deferred:`)" in capsys.readouterr().out

    write_files(repo, {"app/adapters.py": "def f() -> None:\n    import os\n    import psutil\n"})
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 1
    assert "[lazy-import] psutil#1" in capsys.readouterr().out

    write_files(repo, {"app/adapters.py": "def f() -> None:\n    import os\n    import psutil  # deferred: needed lazily\n"})
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 0


def test_a_check_with_no_function_level_imports_says_none(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    assert main(["--config", str(config), "check", "--no-tools"], _svc()) == 0
    assert "function-level imports by module:\n  none" in capsys.readouterr().out
