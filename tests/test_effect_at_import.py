"""#230 ``effect-at-import``: an effect that runs while a module is imported (module level, class body, defaults, decorators)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, services, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule
from arborescent.engine import run_own
from arborescent.facts import extract
from arborescent.presets import EFFECT_CATEGORIES
from arborescent.rules import RuleContext, check_modules, fits_ring
from arborescent.scan_cache_codec import CACHE_VERSION, facts_from_json, facts_to_json
from arborescent.selftest import plan, selftest

CATEGORIES = ["clock", "env", "filesystem", "network", "process", "randomness"]


def policy_for(*categories: str, **overrides: Any) -> Any:
    ring = {"may_import": ["domain"], "external": ["*"], "forbid": [], "forbid_at_import": list(categories or CATEGORIES)}
    return mini_policy(ring=ring_with(ports=ring), **overrides)


def findings(root: Path, source: str, *categories: str, **files: str) -> list[Any]:
    write_files(root, {"app/ports.py": source, "app/adapters.py": "X = 1\n", "app/main.py": "X = 1\n", **files})
    policy = policy_for(*categories)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == Rule.EFFECT_AT_IMPORT]


def lines(found: list[Any]) -> list[int]:
    return sorted(v.line for v in found)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nX = os.getenv('A')\n",
        "import os\nX = os.environ['A']\n",
        "import os\nX = os.environ.get('A')\n",
        "from os import environ\nX = environ['A']\n",
        "import time\nclass C:\n    started = time.time()\n",
        "import time\ndef f(at=time.time()):\n    return at\n",
        "import time\ndef f(*, at: float = time.time()) -> float:\n    return at\n",
        "import os\ndef deco(x):\n    return lambda f: f\n@deco(os.getenv('A'))\ndef f():\n    pass\n",
        "import time\nclass C:\n    def m(self, at=time.time()):\n        return at\n",
        "import time\nif True:\n    X = time.time()\n",
        "import time\ntry:\n    X = time.time()\nexcept ValueError:\n    X = 0\n",
        "import os\nX = [os.getenv(k) for k in ('A',)]\n",
        "from pathlib import Path\nX = Path('a').read_text()\n",
        "from pathlib import Path\nP = Path('a')\nX = P.read_text()\n",
        "X = open('a').read()\n",
        "import subprocess\nsubprocess.run(['x'])\n",
        "import random\nX = random.random()\n",
        "import urllib.request\nX = urllib.request.urlopen('u')\n",
        "import time\nif __name__ != 'x':\n    pass\nelse:\n    X = time.time()\n",
        "import time\nif __name__ == '__main__':\n    pass\nelse:\n    X = time.time()\n",
        "import time\nX = (lambda: time.time())()\n",
    ],
)
def test_an_effect_that_runs_at_import_time_is_reported(repo: Path, source: str) -> None:
    found = findings(repo, source)
    assert [v.subject for v in found] == ["app.ports"], source


@pytest.mark.parametrize(
    "source",
    [
        "",
        "# only a comment\n",
        "import time\ndef f():\n    return time.time()\n",
        "import time\nclass C:\n    def m(self):\n        return time.time()\n",
        "import time\nX = lambda: time.time()\n",
        "import time\ndef f(now=time.time):\n    return now()\n",  # a seam: nothing runs
        "import os\ndef f(environ=os.environ):\n    return environ\n",
        "import time\ndef f(now=lambda: time.time()):\n    return now()\n",
        "import time\nNOW = time.time\n",  # a reference runs nothing
        "import time\nfrom dataclasses import dataclass, field\n@dataclass\nclass C:\n    at: float = field(default_factory=time.time)\n",
        "import time\nif __name__ == '__main__':\n    time.time()\n",
        "from typing import TYPE_CHECKING\nimport os\nif TYPE_CHECKING:\n    X = os.getenv('A')\n",
        "import os\ndef f(x: os.environ) -> os.environ:\n    return x\n",
        "import random\nRNG = random.Random(3)\n",
        "def open():\n    pass\nopen()\n",
        "import time\ndef outer():\n    def inner(at=time.time()):\n        return at\n    return inner\n",
    ],
)
def test_what_does_not_run_at_import_time_is_not_reported(repo: Path, source: str) -> None:
    assert findings(repo, source) == [], source


def test_only_the_listed_categories_fail_and_the_ring_keeps_using_them_in_functions(repo: Path) -> None:
    source = "import os, time\nA = os.getenv('A')\nB = time.time()\ndef f():\n    return os.getenv('B'), time.time()\n"
    found = findings(repo, source, "env")
    assert [(v.line, v.detail) for v in found] == [(2, "[ports] effect env uses os.getenv at import time; move it into a function")]


def test_the_rule_is_off_without_forbid_at_import(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import time\nX = time.time()\n"})
    policy = mini_policy(ring=ring_with(ports={"may_import": ["domain"], "external": ["*"], "forbid": []}))
    scan = FsScanner().scan(policy, repo)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert [v for v in found if v.rule == Rule.EFFECT_AT_IMPORT] == []


@pytest.mark.parametrize("value", ["clock", ["clok"], ["random"], [""], [1]])
def test_forbid_at_import_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="forbid_at_import"):
        mini_policy(ring=ring_with(domain={"forbid_at_import": value}))


LOADER = "from pathlib import Path\n\ndef load_thing():\n    return Path('a').read_text()\n\ndef pure():\n    return 1\n"


@pytest.mark.parametrize(
    "source",
    [
        "from .loader import load_thing\nX = load_thing()\n",
        "from app.loader import load_thing as lt\nX = lt()\n",
        "from . import loader\nX = loader.load_thing()\n",
        "import app.loader\nX = app.loader.load_thing()\n",
        "from .loader import load_thing\ndef f(x=load_thing()):\n    return x\n",
        "from .loader import load_thing\nclass C:\n    x = load_thing()\n",
    ],
)
def test_a_call_into_a_first_party_function_is_followed_one_hop(repo: Path, source: str) -> None:
    found = findings(repo, source, "filesystem", **{"app/loader.py": LOADER, "app/ports.py": source})
    assert [(v.subject, v.detail) for v in found if v.subject == "app.ports"] == [
        ("app.ports", "[ports] import-time call of app.loader.load_thing runs effect filesystem; move it into a function"),
    ]
    assert "pathlib.Path.read_text" in found[0].hint


def test_one_hop_covers_local_functions_decorators_constructors_and_class_methods(repo: Path) -> None:
    source = (
        "import os\n"
        "def read():\n    return os.getenv('A')\n"
        "def register(fn):\n    os.getenv('B')\n    return fn\n"
        "class Built:\n    def __init__(self):\n        os.getenv('C')\n    @staticmethod\n    def make():\n        return os.getenv('D')\n"
        "A = read()\n"
        "@register\ndef f():\n    pass\n"
        "B = Built()\n"
        "C = Built.make()\n"
    )
    found = findings(repo, source, "env")
    assert sorted(v.detail.split(" runs ")[0].removeprefix("[ports] import-time call of ") for v in found) == [
        "app.ports.Built", "app.ports.Built.make", "app.ports.read", "app.ports.register",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from .loader import pure\nX = pure()\n",
        "from .loader import load_thing\ndef f():\n    return load_thing()\n",  # not at import
        "from .loader import load_thing\nX = load_thing\n",  # not called
        "from .loader import missing\nX = missing()\n",
        "import json\nX = json.dumps({})\n",
        "def mid():\n    return second()\ndef second():\n    import os\n    return os.getenv('A')\nX = mid()\n",  # two hops
        "from .loader import load_thing\nload_thing = len\nX = load_thing('a')\n",  # rebound: not the import any more
    ],
)
def test_one_hop_does_not_reach_further(repo: Path, source: str) -> None:
    assert findings(repo, source, "filesystem", "env", **{"app/loader.py": LOADER, "app/ports.py": source}) == [], source


def test_one_hop_ignores_a_callee_whose_effect_is_only_in_a_nested_function_or_a_seam_default(repo: Path) -> None:
    loader = (
        "import time\n"
        "def build():\n    def later():\n        return time.time()\n    return later\n"
        "def seam(now=time.time):\n    return now\n"
    )
    source = "from .loader import build, seam\nA = build()\nB = seam()\n"
    assert findings(repo, source, "clock", **{"app/loader.py": loader, "app/ports.py": source}) == []


def test_one_hop_follows_a_name_re_exported_by_a_package(repo: Path) -> None:
    files = {"app/pkg/__init__.py": "from .impl import load\n\n__all__ = ['load']\n", "app/pkg/impl.py": "def load():\n    return open('a')\n"}
    source = "from .pkg import load\nX = load()\n"
    write_files(repo, {**files, "app/ports.py": source})
    policy = mini_policy(
        ring=ring_with(ports={"may_import": ["domain"], "external": ["*"], "forbid": [], "forbid_at_import": ["filesystem"]}),
        classify={"app": "domain", "app.pkg*": "ports", "app.ports": "ports", "app.*": "composition"},
    )
    scan = FsScanner().scan(policy, repo)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert [v.subject for v in found if v.rule == Rule.EFFECT_AT_IMPORT] == ["app.ports"]


def test_repeated_uses_share_one_baseline_key(repo: Path) -> None:
    found = findings(repo, "import time\nA = time.time()\nB = time.time()\n", "clock")
    assert lines(found) == [2, 3] and len({v.key for v in found}) == 1
    assert baseline.in_family(found[0].key, "effects") and baseline.in_family(found[0].key, "effect-at-import")


def test_nul_bytes_in_a_literal_do_not_break_extraction(repo: Path) -> None:
    assert lines(findings(repo, "X = open('a\\x00b')\n", "filesystem")) == [1]


def test_a_large_module_is_extracted_in_reasonable_time() -> None:
    source = "import time\n" + "".join(f"def f{i}(a=1):\n    return time.time()\nX{i} = f{i}()\n" for i in range(2000))
    facts = extract(source)
    assert len(facts.import_time.calls) == 2000


def test_at_import_does_not_change_how_many_uses_the_effect_rules_see() -> None:
    uses = extract("import os\ndef f(x=os.getenv('A')): return os.getenv('A')\n").effect_uses
    assert [(u.line, u.target, u.at_import) for u in uses] == [(2, "os.getenv", True)]


def test_import_time_facts_survive_the_cache_round_trip() -> None:
    facts = extract("import os\nfrom .a import b\nX = os.getenv('A')\nY = b()\ndef g():\n    return os.getenv('B')\n")
    assert facts.import_time.calls and facts.import_time.function_effects
    assert facts_from_json(json.loads(json.dumps(facts_to_json(facts)))) == facts


def test_a_cache_written_before_import_time_facts_is_parsed_again(repo: Path, tmp_path: Path) -> None:
    """Content did not change, the record layout did: the version must make the old record a miss."""
    write_files(repo, {"app/ports.py": "import time\nX = time.time()\n"})
    policy = policy_for("clock")
    cache = tmp_path / "cache"
    run_own(policy, repo, services(cache=cache), practices=False)
    document = json.loads((cache / "scan.json").read_text(encoding="utf-8"))
    assert document["v"] == CACHE_VERSION and CACHE_VERSION > 25  # 25 is taken by the integration branch
    document["v"] = 24
    for record in document["files"].values():
        record[1] = record[1][:27]  # the 0.9.0 layout
        record[1][1] = [item[:4] for item in record[1][1]]
    (cache / "scan.json").write_text(json.dumps(document), encoding="utf-8")
    found, scan = run_own(policy, repo, services(cache=cache), practices=False)
    assert scan.parsed == len(scan.modules)
    assert [v.subject for v in found if v.rule == Rule.EFFECT_AT_IMPORT] == ["app.ports"]
    warm, again = run_own(policy, repo, services(cache=cache), practices=False)
    assert again.parsed == 0 and [v.key for v in warm] == [v.key for v in found]


def test_fits_ring_counts_import_time_effects(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os\nX = os.getenv('A')\n"})
    ring = {"may_import": ["domain"], "external": ["*"], "forbid": [], "forbid_at_import": ["env"]}
    policy = mini_policy(ring=ring_with(domain={"external": ["*"], "forbid": []}, ports=ring))
    scan = FsScanner().scan(policy, repo)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    info = scan.modules["app.domain"]
    assert fits_ring(context, "app.domain", info, "domain", scan.known)
    assert not fits_ring(context, "app.domain", info, "ports", scan.known)


# --- every category the setting accepts can fire: one reproducer each, so a new category cannot be accepted and never fire ----

# `global`, `registry` and a written cache happen only inside a function, so they run at import through a call (one hop).
RUNS_AT_IMPORT = {
    "clock": "import time\nX = time.time()\n",
    "env": "import os\nX = os.getenv('A')\n",
    "identity": "import uuid\nX = uuid.uuid4()\n",
    "randomness": "import random\nX = random.random()\n",
    "sleep": "import time\ntime.sleep(1)\n",
    "threads": "import threading\nX = threading.Thread()\n",
    "process": "import subprocess\nX = subprocess.run(['a'])\n",
    "database": "import sqlite3\nX = sqlite3.connect('a')\n",
    "network": "import urllib.request\nX = urllib.request.urlopen('u')\n",
    "filesystem": "X = open('a')\n",
    "dynamic-import": "import importlib\nX = importlib.import_module('a')\n",
    "patch": "from unittest.mock import patch\npatch('app.ports.X').start()\n",
    "console": "print('a')\n",
    "global": "X = 0\ndef setup():\n    global X\n    X = 1\nsetup()\n",
    "state": "import threading\nLOCK = threading.Lock()\n",
    "monotonic": "import time\nX = time.monotonic()\n",
    "host": "import os\nX = os.getpid()\n",
    "fs-query": "import os\nX = os.path.exists('a')\n",
    "registry": "REG = {}\ndef register(k):\n    REG[k] = 1\nregister('a')\n",
    "cache": "import functools\n@functools.cache\ndef load():\n    return 1\nX = load()\n",
}


def test_every_accepted_category_has_a_reproducer() -> None:
    assert set(RUNS_AT_IMPORT) == EFFECT_CATEGORIES


@pytest.mark.parametrize("category", sorted(RUNS_AT_IMPORT))
def test_every_accepted_category_fires_at_import(repo: Path, category: str) -> None:
    assert [v.subject for v in findings(repo, RUNS_AT_IMPORT[category], category)] == ["app.ports"]


@pytest.mark.parametrize(("category", "source"), [
    ("global", "X = 0\ndef setup():\n    global X\n    X += 1\nsetup()\n"),
    ("global", "X = 0\ndef setup():\n    global X\n    for X in (1,):\n        pass\nsetup()\n"),
    ("registry", "REG = []\ndef register(fn):\n    REG.append(fn)\n    return fn\n@register\ndef f():\n    pass\n"),
    ("registry", "REG = {}\nclass Plugin:\n    def __init__(self):\n        REG['p'] = self\nP = Plugin()\n"),
    ("registry", "class R:\n    items = []\n    @classmethod\n    def add(cls):\n        cls.items.append(1)\nR.add()\n"),
    ("cache", "_cache = {}\ndef load():\n    _cache['a'] = 1\nload()\n"),
    ("cache", "from functools import lru_cache\nclass A:\n    @staticmethod\n    @lru_cache(maxsize=1)\n    def load():\n        return 1\nX = A.load()\n"),
])
def test_module_state_written_by_a_call_at_import_fires(repo: Path, category: str, source: str) -> None:
    assert [v.subject for v in findings(repo, source, category)] == ["app.ports"], source


@pytest.mark.parametrize(("category", "source"), [
    ("global", "X = 0\ndef setup():\n    global X\n    X = 1\n"),  # never called
    ("global", "X = 0\ndef read():\n    global X\n    return X\nread()\n"),  # a read
    ("global", "global X\nX = 1\n"),  # at module level `global` says nothing
    ("registry", "REG = {}\nREG['a'] = 1\nfor k in 'ab':\n    REG[k] = 1\n"),  # building a table at import is deterministic
    ("registry", "REG = {}\ndef register(k):\n    REG[k] = 1\n"),
    ("registry", "REG = {}\ndef make():\n    return lambda k: REG.pop(k)\nX = make()\n"),  # a deferred write
    ("registry", "REG = {}\ndef make():\n    def later(k):\n        REG[k] = 1\n    return later\nX = make()\n"),
    ("cache", "import functools\n@functools.cache\ndef load():\n    return 1\n"),  # defining a cached loader fills nothing
    ("cache", "import functools\n@functools.cache\ndef load():\n    return 1\nF = load\n"),
])
def test_module_state_that_no_import_runs_does_not_fire(repo: Path, category: str, source: str) -> None:
    assert findings(repo, source, category) == [], source


@pytest.mark.parametrize(("category", "loader"), [
    ("global", "X = 0\ndef setup():\n    global X\n    X = 1\n"),
    ("registry", "REG = {}\ndef setup():\n    REG['a'] = 1\n"),
    ("cache", "import functools\n@functools.cache\ndef setup():\n    return 1\n"),
])
def test_module_state_fires_through_a_call_into_another_module(repo: Path, category: str, loader: str) -> None:
    source = "from .loader import setup\nsetup()\n"
    found = findings(repo, source, category, **{"app/loader.py": loader, "app/ports.py": source})
    assert [v.detail for v in found if v.subject == "app.ports"] == [
        f"[ports] import-time call of app.loader.setup runs effect {category}; move it into a function",
    ]


def test_selftest_probes_the_rule_when_a_ring_enables_it(repo: Path) -> None:
    policy = policy_for("env")
    scan = FsScanner().scan(policy, repo)
    assert Rule.EFFECT_AT_IMPORT in {m.rule for m in plan(policy, scan)[0]}
    report = selftest(policy, repo, services())
    assert any(item.startswith("[effect-at-import]") for item in report.killed)
    assert not [item for item in report.survived if "effect-at-import" in item]


def test_forbid_at_import_is_set_per_scope() -> None:
    ring = {"forbid_at_import": ["env"]}
    policy = policy_from_mapping({"scopes": {"a": {"sources": ["a"], "source_roots": ["."], "rings": ["domain"], "ring": {"domain": ring}}}})
    assert policy.scope("a").policy.ring("domain").forbid_at_import == frozenset({"env"})
