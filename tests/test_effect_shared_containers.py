"""#194, the remainder: writes to containers imported from another module, class-attribute registries, ``self.attr`` writes to
a class's container and writes through an alias, as ``registry``/``cache`` uses. Opt-in like the rest of the two categories.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.domain import Rule
from arborescent.facts import ModuleInfo, extract, link_shared_state
from arborescent.purity import assess
from arborescent.rules import check_modules
from arborescent.scan_cache_codec import facts_from_json, facts_to_json

SHARED = '''\
REG: dict[str, int] = {}
_cache: dict[str, int] = {}
VALUE = 1


class Plugins:
    items: list[str] = []
'''


def seen(source: str) -> set[tuple[str, str]]:
    return {(u.category, u.target) for u in extract(source, "m.py").effect_uses if u.is_module_state}


# --- one module: class containers, `self.attr` and aliases ------------------------------------------------------------------


@pytest.mark.parametrize(("source", "expected"), [
    ("class R:\n    items = []\n    def add(self, x):\n        self.items.append(x)\n", "add.R.items"),
    ("class R:\n    items = {}\n    def add(self, x):\n        self.items[x] = 1\n", "add.R.items"),
    ("class R:\n    items = {}\n    def drop(self, x):\n        del self.items[x]\n", "drop.R.items"),
    ("class R:\n    items: dict = {}\n    @classmethod\n    def add(cls, x):\n        cls.items[x] = 1\n", "add.R.items"),
    ("class R:\n    items = {}\n    def __init_subclass__(cls):\n        cls.items[cls.__name__] = cls\n", "__init_subclass__.R.items"),
    ("class R:\n    items = {}\n    def __new__(cls, x):\n        cls.items[x] = 1\n", "__new__.R.items"),
    ("class R:\n    items = set()\n    def add(self, x):\n        type(self).items.add(x)\n", "add.R.items"),
    ("class R:\n    items = set()\n    def add(self, x):\n        self.__class__.items.add(x)\n", "add.R.items"),
    ("class R:\n    items = []\n\ndef register(x):\n    R.items.append(x)\n", "register.R.items"),
    ("class R:\n    items = []\n    @staticmethod\n    def add(x):\n        R.items.append(x)\n", "add.R.items"),
    ("class Base:\n    items = []\n\nclass R(Base):\n    def add(self, x):\n        self.items.append(x)\n", "add.Base.items"),
    ("from typing import ClassVar\n\n@dataclass\nclass R:\n    items: ClassVar[list] = []\n    def add(self, x):\n        self.items.append(x)\n", "add.R.items"),
    ("class R(Base):\n    items: list = []\n    @classmethod\n    def add(cls, x):\n        cls.items.append(x)\n", "add.R.items"),
    ("class R:\n    def __init__(self):\n        pass\n    items = []\n    def add(me, x):\n        me.items.append(x)\n", "add.R.items"),
    ("class R:\n    items = []\n    def add(self):\n        def inner(x):\n            self.items.append(x)\n        return inner\n", "inner.R.items"),
])
def test_a_write_to_a_class_container_is_a_registry_use(source: str, expected: str) -> None:
    assert seen(source) == {("registry", expected)}


def test_a_cache_named_class_container_is_a_cache() -> None:
    assert seen("class R:\n    _memo = {}\n    def get(self, k):\n        self._memo[k] = 1\n") == {("cache", "get.R._memo")}


@pytest.mark.parametrize("source", [
    "class R:\n    def __init__(self):\n        self.items = []\n    def add(self, x):\n        self.items.append(x)\n",  # per instance
    "class R:\n    items = []\n    def __init__(self):\n        self.items = []\n    def add(self, x):\n        self.items.append(x)\n",  # rebound
    "class R:\n    items = None\n    def add(self, x):\n        self.items.append(x)\n",  # not a container
    "class Base:\n    def __init__(self):\n        self.items = []\n\nclass R(Base):\n    items = []\n    def add(self, x):\n        self.items.append(x)\n",
    "class R(BaseModel):\n    items: list = []\n    def add(self, x):\n        self.items.append(x)\n",  # may be a per-instance field
    "@attrs.define\nclass R:\n    items: list = []\n    def add(self, x):\n        self.items.append(x)\n",
    "class R:\n    items = []\n    def read(self):\n        return list(self.items), self.items.copy()\n",
    "class R:\n    items = []\n    items.append(1)\n",  # a class-body fill
    "class R:\n    items = []\nR.items.append(1)\n",  # an import-time fill
    "class R:\n    items = []\n\ndef f(R):\n    R.items.append(1)\n",  # a parameter, not the class
    "class R:\n    items = []\n    def add(self, other):\n        other.items.append(1)\n",  # some other object
    "class R:\n    items = []\n    @staticmethod\n    def add(self):\n        self.items.append(1)\n",  # no receiver
    "class R:\n    items = []\n    def add(self, x):\n        self = x\n        self.items.append(1)\n",
])
def test_instance_state_and_import_time_fills_are_not_uses(source: str) -> None:
    assert seen(source) == set()


@pytest.mark.parametrize(("source", "expected"), [
    ("REG = {}\n\ndef f(k):\n    r = REG\n    r[k] = 1\n", "f.REG"),
    ("REG = []\n\ndef f(k):\n    r: list = REG\n    r.append(k)\n", "f.REG"),
    ("REG = {}\nALIAS = REG\n\ndef f(k):\n    ALIAS[k] = 1\n", "f.REG"),
    ("class R:\n    items = []\n    def add(self, x):\n        items = self.items\n        items.append(x)\n", "add.R.items"),
])
def test_a_write_through_an_alias_is_a_use(source: str, expected: str) -> None:
    assert seen(source) == {("registry", expected)}


@pytest.mark.parametrize("source", [
    "REG = {}\n\ndef f(k):\n    r = REG.copy()\n    r[k] = 1\n",
    "REG = {}\n\ndef f(k):\n    r = REG\n    r = {}\n    r[k] = 1\n",  # bound twice: unknown
    "REG = {}\n\ndef f(k, r):\n    r = REG\n    r[k] = 1\n",
    "REG = {}\n\ndef f(k):\n    r = REG\n\ndef g(r):\n    r['a'] = 1\n",  # another function's name
    "REG = {}\nALIAS = REG\nALIAS = {}\n\ndef f(k):\n    return ALIAS\n",
    "REG = {}\n\ndef f(REG, k):\n    r = REG\n    r[k] = 1\n",
])
def test_a_copy_or_an_uncertain_alias_is_not_a_use(source: str) -> None:
    assert seen(source) == set()


@pytest.mark.parametrize("source", [
    "", "class R:\n    pass\n", "def f(a):\n    a.b.c.append(1)\n", "def f():\n    x = y\n    y = x\n    x.append(1)\n",
    "def f():\n    x = x\n    x.append(1)\n", "import a\n\ndef f():\n    a[0]()[1] = 2\n", "REG = {}\n\ndef f():\n    '\\x00'\n",
])
def test_odd_input_has_no_uses_and_does_not_crash(source: str) -> None:
    assert seen(source) == set()


def test_a_long_run_of_writes_is_extracted_in_reasonable_time() -> None:
    source = "class R:\n    items = []\n" + "".join(f"    def f{i}(self):\n        self.items.append({i})\n" for i in range(3000))
    assert len(seen(source)) == 3000


def test_mixed_purity_and_the_unguarded_rule_facts_are_unchanged() -> None:
    source = "class R:\n    items = []\n    def add(self, x):\n        self.items.append(x)\n\nREG = {}\n\ndef f(k):\n    r = REG\n    r[k] = 1\n"
    facts = extract(source)
    assert facts.state_writes == ()
    report = assess(ModuleInfo("m", "m.py", False, facts), mini_policy(), mini_policy().mixed_purity)
    assert report is None or "state" not in report.kinds


# --- across modules ---------------------------------------------------------------------------------------------------------


def module(name: str, source: str, is_package: bool = False) -> ModuleInfo:
    return ModuleInfo(name, name.replace(".", "/") + ".py", is_package, extract(source))


def linked(source: str, **others: str) -> set[tuple[str, str]]:
    modules = {"app.shared": module("app.shared", SHARED), "app.user": module("app.user", source)}
    modules.update({name.replace("__", "."): module(name.replace("__", "."), text) for name, text in others.items()})
    return {(u.category, u.target) for u in link_shared_state(modules)["app.user"].facts.effect_uses if u.is_module_state}


@pytest.mark.parametrize(("source", "expected"), [
    ("from app.shared import REG\n\ndef f(k):\n    REG[k] = 1\n", ("registry", "f.app.shared.REG")),
    ("from .shared import REG as R\n\ndef f(k):\n    R.update(k)\n", ("registry", "f.app.shared.REG")),
    ("from . import shared\n\ndef f(k):\n    shared.REG[k] += 1\n", ("registry", "f.app.shared.REG")),
    ("import app.shared\n\ndef f(k):\n    del app.shared.REG[k]\n", ("registry", "f.app.shared.REG")),
    ("import app.shared as s\n\ndef f(k):\n    s._cache[k] = 1\n", ("cache", "f.app.shared._cache")),
    ("def f(k):\n    from app.shared import REG\n    REG[k] = 1\n", ("registry", "f.app.shared.REG")),
    ("from app.shared import Plugins\n\ndef f(k):\n    Plugins.items.append(k)\n", ("registry", "f.app.shared.Plugins.items")),
    ("from app import shared\n\ndef f(k):\n    shared.Plugins.items.append(k)\n", ("registry", "f.app.shared.Plugins.items")),
    ("from app import shared\n\ndef f(k):\n    r = shared.REG\n    r[k] = 1\n", ("registry", "f.app.shared.REG")),
])
def test_a_write_to_an_imported_container_is_a_use(source: str, expected: tuple[str, str]) -> None:
    assert linked(source) == {expected}


def test_a_container_re_exported_by_a_package_is_followed() -> None:
    package = module("app.pkg", "from app.shared import REG\n", is_package=True)
    modules = {"app.shared": module("app.shared", SHARED), "app.pkg": package, "app.user": module("app.user", "from app.pkg import REG\n\ndef f(k):\n    REG[k] = 1\n")}
    assert [u.target for u in link_shared_state(modules)["app.user"].facts.effect_uses if u.is_module_state] == ["f.app.shared.REG"]


def test_a_re_export_cycle_ends() -> None:
    modules = {
        "a": module("a", "from b import X\n"), "b": module("b", "from a import X\n"),
        "app.user": module("app.user", "from a import X\n\ndef f():\n    X.append(1)\n"),
    }
    assert link_shared_state(modules)["app.user"].facts.effect_uses == ()


@pytest.mark.parametrize("source", [
    "from app.shared import VALUE\n\ndef f():\n    VALUE.append(1)\n",  # not a container there
    "from app.shared import missing\n\ndef f():\n    missing.append(1)\n",
    "from app import shared\n\ndef f():\n    shared.add(1)\n",  # a method of a module, not of a container
    "from app.shared import REG\n\ndef f():\n    return REG.get(1), list(REG)\n",
    "from app.shared import REG\nREG['a'] = 1\n",  # import-time registration is not a use (a call into a writer is)
    "from app.shared import REG\n\ndef f(REG):\n    REG['a'] = 1\n",
    "from app.shared import REG\n\ndef f():\n    REG = {}\n    REG['a'] = 1\n",
    "from app.shared import REG\nREG = Thing()\n\ndef f():\n    REG['a'] = 1\n",  # rebound by this module
    "from elsewhere import REG\n\ndef f():\n    REG['a'] = 1\n",  # not scanned
    "import sys\n\ndef f():\n    sys.path.append('a')\n",
])
def test_what_is_not_a_scanned_container_stays_silent(source: str) -> None:
    assert linked(source) == set()


def test_linking_twice_changes_nothing_and_leaves_other_modules_alone() -> None:
    modules = {"app.shared": module("app.shared", SHARED), "app.user": module("app.user", "from app.shared import REG\n\ndef f():\n    REG.clear()\n")}
    once = link_shared_state(modules)
    assert link_shared_state(once) == once and once["app.shared"] is modules["app.shared"]
    assert link_shared_state({}) == {}


def test_the_new_facts_round_trip_through_the_cache() -> None:
    facts = extract(SHARED + "\nfrom a import B\n\ndef f():\n    B.append(1)\n")
    assert facts.shared_state.containers and facts.shared_state.writes
    assert facts_from_json(json.loads(json.dumps(facts_to_json(facts)))) == facts


def found(root: Path, files: dict[str, str], **ring: Any) -> list[tuple[str, str]]:
    write_files(root, files)
    policy = mini_policy(ring=ring_with(**ring))
    scan = FsScanner().scan(policy, root)
    findings = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return sorted((v.rule, v.subject) for v in findings if v.rule.startswith("effect-"))


WRITER = {"app/ports.py": "REG = {}\n", "app/adapters.py": "from .ports import REG\n\n\ndef f() -> None:\n    REG['a'] = 1\n"}
OPEN = {"may_import": ["domain", "ports"], "external": [], "traits": ["permits-effects"]}


def test_the_writing_modules_ring_decides(repo: Path) -> None:
    assert found(repo, WRITER, adapters={**OPEN, "traits": ["no-registry"]}) == [(Rule.EFFECT_STATE, "app.adapters")]


def test_a_ring_that_does_not_list_registry_keeps_its_findings(repo: Path) -> None:
    assert found(repo, WRITER) == []


def test_a_warm_cache_links_the_same_way(repo: Path, tmp_path: Path) -> None:
    write_files(repo, WRITER)
    policy = mini_policy()
    scanner = FsScanner(tmp_path / "cache")
    first = scanner.scan(policy, repo)
    second = scanner.scan(policy, repo)
    assert second.parsed == 0 and second.modules == first.modules
    assert [u.category for u in second.modules["app.adapters"].facts.effect_uses] == ["registry"]


def test_a_changed_only_scan_still_sees_the_unchanged_container_module(repo: Path) -> None:
    write_files(repo, WRITER)
    scan = FsScanner().scan(mini_policy(), repo, frozenset({"app/adapters.py"}))
    assert [u.category for u in scan.modules["app.adapters"].facts.effect_uses] == ["registry"]


def test_a_call_at_import_into_an_imported_registry_writer_fires(repo: Path) -> None:
    files = {
        "app/domain.py": "REG = {}\n",
        "app/ports.py": "from .domain import REG\n\n\ndef add(k: str) -> None:\n    REG[k] = 1\n",
        "app/adapters.py": "from .ports import add\n\nadd('a')\n",
    }
    write_files(repo, files)
    policy = mini_policy(ring=ring_with(adapters={**OPEN, "traits": ["no-registry-at-import"]}))
    scan = FsScanner().scan(policy, repo)
    findings = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert [v.subject for v in findings if v.rule == Rule.EFFECT_AT_IMPORT] == ["app.adapters"]
