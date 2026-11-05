from __future__ import annotations

from pathlib import Path

from arborescent_helpers import MINI_FILES, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.facts import RawImport, extract, resolve_targets


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


def test_arbor_ring_facts_ignore_function_and_class_scoped_annotations() -> None:
    facts = extract(
        '''\
def function():
    __arbor__ = {"ring": "function"}

class Example:
    __arbor__ = {"ring": "class"}
'''
    )

    assert facts.arbor_ring is None
    assert facts.arbor_error is None


def test_arbor_ring_facts_reject_conditional_module_level_annotation() -> None:
    facts = extract(
        '''\
if True:
    __arbor__ = {"ring": "conditional"}
'''
    )

    assert facts.arbor_ring is None
    assert facts.arbor_error == "__arbor__ must not be assigned inside conditional code"


def test_arbor_ring_facts_reject_conditional_annotation_even_with_an_unconditional_one() -> None:
    facts = extract(
        '''\
__arbor__ = {"ring": "domain"}

if True:
    __arbor__ = {"ring": "conditional"}
'''
    )

    assert facts.arbor_ring is None
    assert facts.arbor_error == "__arbor__ must not be assigned inside conditional code"


def test_arbor_ring_facts_reject_annotation_nested_in_try_for_while_with() -> None:
    sources = (
        'try:\n    __arbor__ = {"ring": "domain"}\nexcept Exception:\n    pass\n',
        'for _ in range(1):\n    __arbor__ = {"ring": "domain"}\n',
        'while False:\n    __arbor__ = {"ring": "domain"}\n',
        'with open("x") as f:\n    __arbor__ = {"ring": "domain"}\n',
    )

    for source in sources:
        facts = extract(source)
        assert facts.arbor_ring is None
        assert facts.arbor_error == "__arbor__ must not be assigned inside conditional code"


def test_arbor_ring_facts_reject_unknown_keys() -> None:
    facts = extract('__arbor__ = {"ring": "domain", "public": True}\n')

    assert facts.arbor_ring is None
    assert facts.arbor_error == "__arbor__ has unknown key(s): 'public'"


def test_arbor_ring_facts_reject_multiple_unknown_keys_sorted() -> None:
    facts = extract('__arbor__ = {"ring": "domain", "zeta": 1, "alpha": 2}\n')

    assert facts.arbor_ring is None
    assert facts.arbor_error == "__arbor__ has unknown key(s): 'alpha', 'zeta'"


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


def _clock_targets(source: str) -> set[tuple[int, str]]:
    return {(use.line, use.target) for use in extract(source).effect_uses if use.category == "clock" and use.seam == 0}


def test_parameter_default_naming_an_effect_is_an_injection_seam_not_a_use() -> None:
    assert _clock_targets("from datetime import datetime\ndef f(*, now=datetime.now):\n    return now()\n") == set()
    assert _clock_targets("import uuid\nclass C:\n    def __init__(self, new_id=uuid.uuid4): ...\n") == set()
    assert _clock_targets("import time\nasync def f(clock_time=time.time): ...\n") == set()
    assert _clock_targets("import time\ng = lambda t=time.time: t()\n") == set()


def test_effect_outside_a_bare_parameter_default_still_counts() -> None:
    now = "datetime.datetime.now"
    assert _clock_targets("import datetime\ndef f():\n    return datetime.datetime.now()\n") == {(3, now)}
    assert _clock_targets("import datetime\nNOW = datetime.datetime.now\n") == {(2, now)}
    assert _clock_targets("import datetime\ndef f(*, now=lambda: datetime.datetime.now()): ...\n") == {(2, now)}
    assert _clock_targets("import datetime\ndef f(now=datetime.datetime.now):\n    return datetime.datetime.now()\n") == {(3, now)}


def test_a_dotted_parameter_default_is_a_seam_all_the_way_down() -> None:
    facts = extract("import os\n\ndef f(get=os.environ.get):\n    return get('X')\n\ndef g():\n    return os.environ.get('X')\n", "m.py")

    assert [(use.category, use.line) for use in facts.effect_uses if use.seam == 0] == [("env", 7)]
    assert {use.line for use in facts.effect_uses if use.seam} == {3}


SEAMS = (
    "import datetime\nfrom typing import Callable\n\n"
    "def typed(now: Callable[[], object] = datetime.datetime.now): return now()\n"
    "def untyped(now=datetime.datetime.now): return now()\n"
    "def wrong(now: int = datetime.datetime.now): return now\n"
)


def test_parameter_defaults_record_whether_their_annotation_is_a_callable() -> None:
    kinds = {use.line: use.seam for use in extract(SEAMS, "m.py").effect_uses}

    assert kinds == {4: 1, 5: 2, 6: 2}


def test_effect_defaults_modes_decide_which_seams_count() -> None:
    modes = {mode: [mini_policy(effect_defaults=mode).counts_effect_seam(s) for s in (0, 1, 2)] for mode in ("injection", "typed", "count")}

    assert modes == {"injection": [True, False, False], "typed": [True, False, True], "count": [True, True, True]}


def _resolve(imp_module: str, names: tuple[str, ...], known: set[str]) -> list[str]:
    internal, _, _ = resolve_targets("app.user", False, RawImport(0, imp_module, names, 1, False, False), known, {"ingest", "app"})
    return internal


KNOWN = {"ingest", "ingest.rules", "ingest.other", "app", "app.user"}


def test_from_package_import_submodule_is_an_edge_to_the_submodule_only() -> None:
    assert _resolve("ingest", ("rules",), KNOWN) == ["ingest.rules"]  # #109: not also the package __init__


def test_from_package_import_several_submodules_has_no_package_edge() -> None:
    assert _resolve("ingest", ("rules", "other"), KNOWN) == ["ingest.rules", "ingest.other"]


def test_from_package_import_attribute_is_an_edge_to_the_package() -> None:
    assert _resolve("ingest", ("Hints",), KNOWN) == ["ingest"]


def test_from_package_import_submodule_and_attribute_keeps_both_edges() -> None:
    assert _resolve("ingest", ("rules", "Hints"), KNOWN) == ["ingest", "ingest.rules"]


def test_from_package_import_star_keeps_the_package_edge() -> None:
    assert _resolve("ingest", ("*",), KNOWN) == ["ingest"]


def test_from_submodule_import_name_is_an_edge_to_the_submodule() -> None:
    assert _resolve("ingest.rules", ("Hints",), KNOWN) == ["ingest.rules"]
