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


def test_top_level_def_facts_recognize_abc_and_protocol_classes() -> None:
    facts = extract(
        """\
from abc import ABC, ABCMeta
import abc
from typing import Protocol

class ViaBase(ABC):
    pass

class ViaDottedBase(abc.ABC):
    pass

class ViaProtocol(Protocol):
    def run(self) -> None: ...

class ViaMetaclass(metaclass=ABCMeta):
    pass

class Concrete:
    pass

class InheritsConcrete(Concrete):
    def run(self) -> None: ...
"""
    )
    by_name = {d.name: d for d in facts.top_level_defs}
    assert by_name["ViaBase"].is_abstract
    assert by_name["ViaDottedBase"].is_abstract
    assert by_name["ViaProtocol"].is_abstract
    assert by_name["ViaMetaclass"].is_abstract
    assert not by_name["Concrete"].is_abstract
    assert not by_name["InheritsConcrete"].is_abstract  # a concrete base makes it concrete, stub methods or not
    assert all(d.is_class for d in by_name.values())


def test_top_level_def_facts_recognize_stub_functions() -> None:
    facts = extract(
        '''\
def ellipsis_stub() -> None: ...

def pass_stub() -> None:
    pass

def docstring_only_stub() -> None:
    """Docs, no body."""
    ...

def raises_not_implemented() -> None:
    raise NotImplementedError

def concrete_function() -> int:
    return 1 + 1

def _private_concrete() -> int:
    return 2
'''
    )
    by_name = {d.name: d for d in facts.top_level_defs}
    assert by_name["ellipsis_stub"].is_abstract
    assert by_name["pass_stub"].is_abstract
    assert by_name["docstring_only_stub"].is_abstract
    assert by_name["raises_not_implemented"].is_abstract
    assert not by_name["concrete_function"].is_abstract
    assert not by_name["_private_concrete"].is_abstract
    assert all(not d.is_class for d in by_name.values())


def test_top_level_def_facts_ignore_nested_and_empty_modules() -> None:
    assert extract("X = 1\n").top_level_defs == ()
    facts = extract(
        """\
class Outer:
    class Inner:
        pass

    def method(self) -> None:
        def nested() -> None:
            return None
"""
    )
    assert [d.name for d in facts.top_level_defs] == ["Outer"]


def test_fact_cache_round_trips_suppressions_and_discard_calls(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            **MINI_FILES,
            "app/domain.py": 'import os  # noqa: F401\ndiscard(task(), reason="legacy")\n\n\nclass Port:\n    pass\n',
        },
    )
    cache = tmp_path / ".cache"
    scanner = FsScanner(cache)

    first = scanner.scan(mini_policy(), tmp_path)
    warm = scanner.scan(mini_policy(), tmp_path)

    assert first.parsed > 0 and warm.parsed == 0
    assert warm.modules["app.domain"].facts.suppressions == ((1, "noqa-coded"),)
    assert warm.modules["app.domain"].facts.discard_calls == (2,)
    assert warm.modules["app.domain"].facts.top_level_defs == first.modules["app.domain"].facts.top_level_defs
    assert [d.name for d in warm.modules["app.domain"].facts.top_level_defs] == ["Port"]
