"""#194: mutation of module-level containers (`registry`) and `functools.cache` loaders (`cache`) as `effect-state` uses.

Both categories are detected but are in no default `forbid`, no taint seed and no `must_be_pure`, so a policy that does not
name them keeps its findings. The first block pins what every consumer of `effect_uses` did before the categories existed
(each test holds the same module and must pass before and after the change); the rest pins the new behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.classify_audit import _evidence  # pyright: ignore[reportPrivateUsage]
from arborescent.config import policy_from_mapping
from arborescent.coupling import accessor_surface
from arborescent.domain import EFFECT_RULES, PolicyError, Rule
from arborescent.facts import ModuleInfo, extract
from arborescent.package_constraint_rules import forbidden_by
from arborescent.package_constraints import PackageConstraints
from arborescent.presets import (
    DEFAULT_FORBID_CATEGORIES,
    EFFECT_CATEGORIES,
    MODULE_STATE_CATEGORIES,
    TAINT_SEED_CATEGORIES,
)
from arborescent.purity import assess
from arborescent.rules import check_modules

ROOT = Path(__file__).resolve().parent.parent

STATE_MODULE = '''\
from __future__ import annotations

import functools

_REGISTRY: dict[str, int] = {}
_NAMES: list[str] = []
_cache: dict[str, int] = {}


def register(name: str) -> None:
    _REGISTRY[name] = len(_REGISTRY)
    _NAMES.append(name)


def remember(key: str) -> int:
    return _cache.setdefault(key, 1)


@functools.lru_cache(maxsize=None)
def load(path: str) -> str:
    return path.upper()


def total(values: list[int]) -> int:
    acc = 0
    for value in values:
        acc += value
    return acc


def doubled(values: list[int]) -> list[int]:
    out = []
    for value in values:
        out.append(value * 2)
    return out
'''


def seen(source: str) -> set[tuple[str, str]]:
    return {(u.category, u.target) for u in extract(source, "m.py").effect_uses if u.seam == 0}


def effect_rules(root: Path, **overrides: Any) -> list[tuple[str, str]]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return sorted((f.rule, f.subject) for f in found if f.rule.startswith("effect-"))


# --- consumers of `effect_uses` keep their output (each caller's use of the current behaviour) -------------------------------


def test_default_check_output_is_unchanged(repo: Path) -> None:
    write_files(repo, {"app/domain.py": STATE_MODULE, "app/adapters.py": "from . import domain\n\n\ndef go() -> None:\n    domain.register('x')\n"})
    assert effect_rules(repo) == []


def test_effect_via_import_default_is_unchanged(repo: Path) -> None:
    write_files(repo, {"app/ports.py": STATE_MODULE, "app/adapters.py": "from . import ports\n\n\ndef go() -> None:\n    ports.register('x')\n"})
    rings = ring_with(adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["console", "global"]})
    assert effect_rules(repo, ring=rings) == []


def test_taint_seeds_and_must_be_pure_do_not_include_them() -> None:
    assert not MODULE_STATE_CATEGORIES & (DEFAULT_FORBID_CATEGORIES | TAINT_SEED_CATEGORIES)
    with pytest.raises(PolicyError):
        policy_from_mapping({**_mini(), "taint_seeds": ["registry"]})


def _mini() -> dict[str, Any]:
    return dict(MINI)


def test_coupling_accessor_surface_ignores_container_mutation() -> None:
    accessors, public = accessor_surface(ModuleInfo("m", "m.py", False, extract(STATE_MODULE)))
    assert accessors == () and public == 5


def test_classify_evidence_ignores_container_mutation() -> None:
    info = ModuleInfo("m", "m.py", False, extract(STATE_MODULE))
    assert _evidence(info, {"m"}, "m").endswith("effects none")


def test_mixed_purity_still_sees_the_same_kinds() -> None:
    report = assess(ModuleInfo("m", "m.py", False, extract(STATE_MODULE)), mini_policy())
    assert report is not None and set(report.kinds) == {"state", "memo"}


def test_package_constraints_must_be_pure_does_not_forbid_them() -> None:
    assert not MODULE_STATE_CATEGORIES & forbidden_by(PackageConstraints(must_be_pure=True))
    assert forbidden_by(PackageConstraints(forbid_effects=("registry", "cache"))) >= MODULE_STATE_CATEGORIES


# --- detection --------------------------------------------------------------------------------------------------------------


def test_the_gap_fixture_is_now_seen() -> None:
    assert seen(STATE_MODULE) == {
        ("registry", "register._REGISTRY"), ("registry", "register._NAMES"), ("cache", "remember._cache"), ("cache", "functools.lru_cache"),
    }


@pytest.mark.parametrize("body", [
    "REG[k] = v",
    "REG[k] += 1",
    "del REG[k]",
    "REG.update(other)",
    "REG.setdefault(k, 1)",
    "REG.pop(k, None)",
    "REG.clear()",
])
def test_every_mutation_form_of_a_module_dict_is_a_registry_use(body: str) -> None:
    source = f"REG = {{}}\n\n\ndef f(k, v, other):\n    {body}\n"
    assert seen(source) == {("registry", "f.REG")}


@pytest.mark.parametrize("binding", ["REG = {}", "REG = []", "REG = set()", "REG: dict[str, int] = dict()", "REG = defaultdict(list)", "REG = {k: 1 for k in 'ab'}"])
def test_module_level_containers_by_literal_and_constructor(binding: str) -> None:
    assert seen(f"from collections import defaultdict\n{binding}\n\n\ndef f(k):\n    REG.clear()\n") == {("registry", "f.REG")}


def test_a_method_or_nested_function_that_writes_the_registry_counts() -> None:
    source = "REG = {}\n\n\nclass A:\n    def add(self, k):\n        REG[k] = 1\n\n\ndef outer():\n    def inner(k):\n        REG[k] = 2\n    return inner\n"
    assert seen(source) == {("registry", "add.REG"), ("registry", "inner.REG")}


@pytest.mark.parametrize("source", [
    "REG = {}\n\n\ndef f():\n    REG = {}\n    REG['a'] = 1\n    return REG\n",  # a local of the same name
    "REG = {}\n\n\ndef f(REG):\n    REG['a'] = 1\n",  # a parameter of the same name
    "REG = {}\n\n\ndef outer():\n    REG = {}\n\n    def inner():\n        REG['a'] = 1\n    return inner\n",  # a closure over a local
    "def f():\n    d = {}\n    d['a'] = 1\n    return d\n",  # never a module name
    "REG = {}\nREG['a'] = 1\n\n\ndef f():\n    return REG['a']\n",  # import-time fill, and a read
    "REG = {}\n\n\ndef f(x):\n    return REG.get(x), list(REG), REG.copy(), x in REG\n",  # reads only
    "REG = ()\n\n\ndef f():\n    return REG + (1,)\n",  # not a mutable container
    "import functools\n\n\n@functools.cached_property\ndef f(self):\n    return 1\n",  # per instance, not shared
])
def test_locals_reads_and_import_time_fills_are_not_uses(source: str) -> None:
    assert seen(source) == set()


def test_a_global_declared_container_name_is_still_the_module_container() -> None:
    source = "REG = {}\n\n\ndef f():\n    global REG\n    REG['a'] = 1\n"
    assert ("registry", "f.REG") in seen(source)


@pytest.mark.parametrize("source", [
    "import functools\n\n\n@functools.cache\ndef f():\n    return 1\n",
    "import functools\n\n\n@functools.lru_cache(maxsize=8)\ndef f():\n    return 1\n",
    "from functools import lru_cache\n\n\n@lru_cache\ndef f():\n    return 1\n",
    "from functools import cache as memoize\n\n\n@memoize\ndef f():\n    return 1\n",
    "import functools as ft\n\n\nclass A:\n    @ft.lru_cache()\n    def f(self):\n        return 1\n",
])
def test_functools_cache_loaders_are_cache_uses(source: str) -> None:
    assert {category for category, _ in seen(source)} == {"cache"}


@pytest.mark.parametrize("source", ["", "\n", "REG = {}\n", "def f():\n    pass\n"])
def test_empty_input_has_no_uses(source: str) -> None:
    assert seen(source) == set()


def test_a_cache_named_container_is_a_cache_not_a_registry() -> None:
    assert seen("_MEMO = {}\n\n\ndef f(k):\n    _MEMO[k] = 1\n") == {("cache", "f._MEMO")}


# --- policy: opt in by listing the category ---------------------------------------------------------------------------------


def test_each_category_is_valid_in_forbid_and_maps_to_effect_state() -> None:
    assert frozenset({"registry", "cache"}) == MODULE_STATE_CATEGORIES
    assert MODULE_STATE_CATEGORIES <= EFFECT_CATEGORIES
    assert {EFFECT_RULES[c] for c in MODULE_STATE_CATEGORIES} == {Rule.EFFECT_STATE}


def test_forbidding_registry_fires_only_for_the_registry(repo: Path) -> None:
    write_files(repo, {"app/domain.py": STATE_MODULE})
    found = effect_rules(repo, ring=ring_with(domain={"external": ["functools"], "forbid": ["registry"]}))
    assert found == [("effect-state", "app.domain")] * 2  # _REGISTRY and _NAMES, not the cache or the lru_cache loader


def test_forbidding_cache_fires_only_for_the_cache(repo: Path) -> None:
    write_files(repo, {"app/domain.py": STATE_MODULE})
    found = effect_rules(repo, ring=ring_with(domain={"external": ["functools"], "forbid": ["cache"]}))
    assert found == [("effect-state", "app.domain")] * 2  # the `_cache` write and the `lru_cache` loader


def test_forbidding_the_lock_category_alone_keeps_its_old_meaning(repo: Path) -> None:
    write_files(repo, {"app/domain.py": STATE_MODULE})
    assert effect_rules(repo, ring=ring_with(domain={"external": ["functools"], "forbid": ["state"]})) == []


def test_effect_via_import_carries_an_opted_in_category(repo: Path) -> None:
    write_files(repo, {"app/ports.py": STATE_MODULE, "app/adapters.py": "from . import ports\n\n\ndef go() -> None:\n    ports.register('x')\n"})
    rings = ring_with(
        ports={"may_import": ["domain"], "external": ["functools"], "forbid": []},
        adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["registry"]},
    )
    assert ("effect-via-import", "app.adapters") in effect_rules(repo, ring=rings)


# --- the cache round trip, and a scan that fails when a consumer or the docs miss a category --------------------------------


def test_scanner_cache_round_trips_the_new_uses(repo: Path) -> None:
    write_files(repo, {"app/domain.py": STATE_MODULE})
    policy = mini_policy()
    first = FsScanner().scan(policy, repo).modules["app.domain"].facts.effect_uses
    second = FsScanner().scan(policy, repo).modules["app.domain"].facts.effect_uses
    assert first == second and {u.category for u in first} == MODULE_STATE_CATEGORIES


def test_every_category_has_a_rule_and_a_documented_row() -> None:
    assert set(EFFECT_RULES) == EFFECT_CATEGORIES
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    forbid_row = next(line for line in docs.splitlines() if line.startswith("| `forbid` |"))
    table = docs.split("## Effect categories and the `[effects]` table", 1)[1].split("###", 1)[0]
    for category in EFFECT_CATEGORIES:
        assert f"`{category}`" in forbid_row, f"`forbid` row does not list {category}"
        assert re.search(rf"^\| `{re.escape(category)}` \|", table, re.MULTILINE), f"category table has no row for {category}"
    assert "taint_seeds" in docs


def test_every_reader_of_effect_uses_is_known() -> None:
    """A new reader of `effect_uses` must decide what it does with the opt-in categories: it fails here until it is listed."""
    readers = sorted(
        path.name for path in (ROOT / "src" / "arborescent").glob("*.py") if re.search(r"\.effect_uses\b", path.read_text(encoding="utf-8"))
    )
    assert readers == [
        "classify_audit.py",  # skips MODULE_STATE_CATEGORIES: it describes a module's I/O
        "coupling.py",  # skips MODULE_STATE_CATEGORIES: accessors are I/O or ambient reads
        "facts.py",  # produces them
        "package_constraint_rules.py",  # forbidden set only, opt in through `forbid_effects`
        "purity.py",  # filtered by `mixed_purity.kinds`, which cannot name them
        "rules.py",  # filtered by the ring's `forbid`
        "scanner.py",  # serialises every category
        "selftest.py",  # filtered by the ring's `forbid`
        "tach_visibility.py",  # dynamic-import only
    ]
