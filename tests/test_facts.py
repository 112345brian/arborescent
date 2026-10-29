from __future__ import annotations

from pathlib import Path

from arborescent_helpers import MINI_FILES, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.facts import extract


def test_effect_facts_resolve_module_and_from_import_aliases() -> None:
    facts = extract(
        """\
import os as operating_system
import time as timer
from datetime import datetime as moment
from uuid import uuid4 as new_id
from random import random as draw
from subprocess import run as launch
from importlib import import_module as load_module

operating_system.getenv("MODE")
timer.time()
moment.now()
new_id()
draw()
timer.sleep(1)
launch(["tool"])
load_module("example")
"""
    )
    assert {(use.category, use.target) for use in facts.effect_uses} == {
        ("env", "os.getenv"),
        ("clock", "time.time"),
        ("clock", "datetime.datetime.now"),
        ("identity", "uuid.uuid4"),
        ("randomness", "random.random"),
        ("sleep", "time.sleep"),
        ("process", "subprocess.run"),
        ("dynamic-import", "importlib.import_module"),
    }


def test_os_branch_facts_resolve_aliases_and_from_imports() -> None:
    facts = extract("import os as operating_system\nfrom sys import platform as current_platform\noperating_system.name\ncurrent_platform\n")
    assert {(line, target) for line, target in facts.os_uses} == {
        (3, "os.name"),
        (2, "sys.platform"),
    }


def test_patch_facts_keep_only_static_targets() -> None:
    facts = extract(
        """\
from unittest.mock import patch as mock_patch
mock_patch("example.service.now")
monkeypatch.setattr("example.service.time.time", lambda: 1)
"""
    )
    assert {(use.category, use.target) for use in facts.effect_uses} == {
        ("patch", "example.service.now"),
        ("patch", "example.service.time.time"),
    }


def test_effect_facts_include_os_process_launch_and_executor_construction() -> None:
    facts = extract(
        """\
import os
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
os.fork()
os.execv("worker", ["worker"])
ThreadPoolExecutor()
ProcessPoolExecutor()
"""
    )

    assert {(use.category, use.target) for use in facts.effect_uses} >= {
        ("process", "os.fork"),
        ("process", "os.execv"),
        ("threads", "concurrent.futures.ThreadPoolExecutor"),
        ("process", "concurrent.futures.ProcessPoolExecutor"),
    }


def test_suppression_facts_classify_comment_directives_without_matching_strings() -> None:
    facts = extract(
        '''\
# noqa
import os  # noqa: F401, E501
# ruff: noqa
# ruff: noqa: PLR0912
# type: ignore
value: int = "bad"  # type: ignore[assignment]
text = "# noqa # type: ignore"
'''
    )

    assert facts.suppressions == (
        (1, "noqa-blanket"),
        (2, "noqa-coded"),
        (3, "ruff-noqa-blanket"),
        (4, "ruff-noqa-coded"),
        (5, "type-ignore-blanket"),
        (6, "type-ignore-coded"),
    )


def test_discard_facts_count_only_reasoned_discard_helper_calls() -> None:
    facts = extract(
        '''\
discard(run(), reason="best effort")
result.discard(value, reason="legacy")
items.discard(value)
discard(run())
'''
    )

    assert facts.discard_calls == (1, 2)


def test_arbor_ring_facts_extract_literal_ring() -> None:
    facts = extract('__arbor__ = {"ring": "domain"}\n')

    assert facts.arbor_ring == "domain"
    assert facts.arbor_error is None


def test_arbor_ring_facts_report_malformed_annotations() -> None:
    sources = (
        '__arbor__ = {}\n',
        '__arbor__ = {"ring": 1}\n',
        '__arbor__ = {"ring": make_ring()}\n',
        '__arbor__ = []\n',
    )

    for source in sources:
        facts = extract(source)
        assert facts.arbor_ring is None
        assert facts.arbor_error is not None


def test_arbor_ring_literal_error_is_stable_across_extractions() -> None:
    source = '__arbor__ = {"ring": make_ring()}\n'

    first = extract(source).arbor_error
    second = extract(source).arbor_error

    assert first == second == "__arbor__ must be a literal dictionary"


def test_arbor_ring_facts_ignore_nested_annotations() -> None:
    facts = extract(
        '''\
def function():
    __arbor__ = {"ring": "function"}

class Example:
    __arbor__ = {"ring": "class"}

if True:
    __arbor__ = {"ring": "conditional"}
'''
    )

    assert facts.arbor_ring is None
    assert facts.arbor_error is None


def test_arbor_ring_facts_use_last_module_binding() -> None:
    facts = extract(
        '''\
__arbor__ = {"ring": "first"}
__arbor__: dict[str, str] = {"ring": "last"}
'''
    )

    assert facts.arbor_ring == "last"
    assert facts.arbor_error is None


def test_arbor_ring_facts_default_to_absent() -> None:
    facts = extract("value = 1\n")

    assert facts.arbor_ring is None
    assert facts.arbor_error is None


def test_fact_cache_round_trips_suppressions_and_discard_calls(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            **MINI_FILES,
            "app/domain.py": 'import os  # noqa: F401\ndiscard(task(), reason="legacy")\n',
        },
    )
    cache = tmp_path / ".cache"
    scanner = FsScanner(cache)

    first = scanner.scan(mini_policy(), tmp_path)
    warm = scanner.scan(mini_policy(), tmp_path)

    assert first.parsed > 0 and warm.parsed == 0
    assert warm.modules["app.domain"].facts.suppressions == ((1, "noqa-coded"),)
    assert warm.modules["app.domain"].facts.discard_calls == (2,)
