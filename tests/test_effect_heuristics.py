"""0.8.0 effect heuristics: what counts as an effect, the sanctioned-seam table and its guard, mixed-purity ring filter, `exclude` anchoring.

The snippets are minimised from a ~1500-module real project in which 98% of `effect-via-import` findings were not actionable:
`importlib.util.find_spec`, a module-level `threading.Lock()`, `random.Random(seed)`, a `clock=lambda: datetime.now(UTC)` default
and a read-only port module that every importer inherited.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.cli_report import _print_excluded
from arborescent.config import policy_from_mapping
from arborescent.domain import EFFECT_RULES, PolicyError, Rule
from arborescent.engine import CheckReport
from arborescent.facts import extract
from arborescent.path_exclusion import excluded_by_policy, nested_bare_pattern
from arborescent.presets import DEFAULT_FORBID_CATEGORIES, EFFECT_CATEGORIES
from arborescent.rules import check_modules


def uses(source: str) -> set[tuple[str, str]]:
    """(category, target) of every effect use that counts under the default `effect_defaults = "injection"`."""
    return {(use.category, use.target) for use in extract(source, "m.py").effect_uses if use.seam == 0}


def categories(source: str) -> set[str]:
    return {category for category, _ in uses(source)}


# --- dynamic-import: only real run-time module loading ---------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "import importlib\nimportlib.import_module('x')\n",
    "from importlib import import_module\nimport_module('x')\n",
    "__import__('x')\n",
    "import importlib.util\nspec = importlib.util.spec_from_file_location('m', 'p.py')\nmod = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(mod)\n",
    "import importlib\nimportlib.reload(m)\n",
])
def test_real_run_time_module_loading_is_dynamic_import(source: str) -> None:
    assert "dynamic-import" in categories(source)


@pytest.mark.parametrize("source", [
    "import importlib.util\nfound = importlib.util.find_spec('openai_codex') is not None\n",
    "import importlib.resources\ntext = importlib.resources.files('pkg')\n",
    "import importlib.metadata\nversion = importlib.metadata.version('pkg')\n",
])
def test_find_spec_resources_and_metadata_load_nothing(source: str) -> None:
    assert "dynamic-import" not in categories(source)


# --- threads vs state ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("primitive", ["Lock", "RLock", "Semaphore", "BoundedSemaphore", "Event", "Condition", "Barrier", "local"])
def test_synchronisation_primitives_are_state_not_threads(primitive: str) -> None:
    assert categories(f"import threading\n_X = threading.{primitive}()\n") == {"state"}


@pytest.mark.parametrize("source", [
    "import threading\nthreading.Thread(target=f).start()\n",
    "import threading\nthreading.Timer(1.0, f)\n",
    "from concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor(2)\n",
])
def test_creating_threads_stays_threads(source: str) -> None:
    assert categories(source) == {"threads"}


def test_a_type_annotation_is_never_an_effect_use() -> None:
    source = (
        "import random, threading\nfrom concurrent.futures import ThreadPoolExecutor\n"
        "def f(lock: threading.Lock, rng: random.Random | None = None, pool: 'ThreadPoolExecutor' = None) -> ThreadPoolExecutor:\n"
        "    return pool\n"
        "_TOTAL: threading.Semaphore\n"
    )
    assert uses(source) == set()


def test_the_same_name_in_a_body_still_counts_next_to_its_annotation() -> None:
    assert categories("import threading\ndef f(lock: threading.Lock):\n    return threading.Lock()\n") == {"state"}


# --- randomness ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "import random\nrng = random.Random(7)\n",
    "import random\nrng = random.Random(seed)\n",
    "import random\nrng = random.Random(x=1)\n" if False else "import random\nrng = random.Random(a=seed)\n",
    "from random import Random\nrng = Random(_seed('v'))\n",
])
def test_a_seeded_random_generator_is_deterministic(source: str) -> None:
    assert categories(source) == set()


@pytest.mark.parametrize("source", [
    "import random\nrng = random.Random()\n",
    "import random\nrng = random.Random(None)\n",
    "import random\nrng = rng if rng is not None else random.Random()\n",
    "import random\nrandom.SystemRandom()\n",
    "import random\nrandom.random()\n",
    "import secrets\nsecrets.token_hex(8)\n",
    "import os\nos.urandom(8)\n",
])
def test_unseeded_and_system_randomness_still_counts(source: str) -> None:
    assert "randomness" in categories(source)


def test_a_clock_read_used_as_the_seed_is_still_a_clock_read() -> None:
    assert categories("import random, time\nrng = random.Random(time.time())\n") == {"clock"}


# --- lambda injection seam -------------------------------------------------------------------------------------------------


def test_a_lambda_default_whose_only_effect_is_its_call_is_a_seam() -> None:
    source = "from datetime import datetime, UTC\ndef f(clock=lambda: datetime.now(UTC)):\n    return clock()\n"
    assert uses(source) == set()
    assert {(u.category, u.seam) for u in extract(source, "m.py").effect_uses} == {("clock", 2)}


def test_an_effect_passed_into_the_lambda_call_still_counts() -> None:
    source = "import time\ndef f(fmt=lambda: time.strftime('%Y', time.localtime())):\n    return fmt()\n"
    assert ("clock", "time.localtime") in uses(source)
    assert ("clock", "time.strftime") not in uses(source)


def test_a_lambda_assigned_to_a_variable_is_not_a_seam() -> None:
    assert categories("import time\nnow = lambda: time.time()\n") == {"clock"}


# --- detection gaps --------------------------------------------------------------------------------------------------------


def test_path_methods_on_a_variable_bound_from_path_are_filesystem() -> None:
    source = (
        "from pathlib import Path\n"
        "def load(p: Path, name: str):\n"
        "    out = p / name\n"
        "    return out.read_text(), p.exists()\n"
    )
    assert uses(source) == {("filesystem", "pathlib.Path.read_text"), ("fs-query", "pathlib.Path.exists")}


def test_a_string_named_like_a_path_elsewhere_is_not_a_path() -> None:
    source = (
        "from pathlib import Path\n"
        "def a(path: Path):\n    return path.stat()\n"
        "def b(path):\n    return path.replace('x', 'y').open\n"
    )
    assert [use.line for use in extract(source, "m.py").effect_uses] == [3]


def test_a_module_that_does_not_import_pathlib_has_no_path_receivers() -> None:
    assert uses("def f(p):\n    return p.read_text()\n") == set()


def test_path_literal_receiver_queries_are_fs_query_and_io_stays_filesystem() -> None:
    source = "from pathlib import Path\nPath('x').exists()\nPath('x').write_text('a')\nPath.cwd()\nPath.home()\n"
    assert uses(source) == {
        ("fs-query", "pathlib.Path.exists"), ("filesystem", "pathlib.Path.write_text"),
        ("fs-query", "pathlib.Path.cwd"), ("fs-query", "pathlib.Path.home"),
    }


@pytest.mark.parametrize(("source", "expected"), [
    ("import os\nos.path.exists('x')\n", "fs-query"),
    ("import os\nos.getcwd()\n", "fs-query"),
    ("import shutil\nshutil.which('git')\n", "fs-query"),
    ("import os\nos.path.join('a', 'b')\n", None),
    ("import time\ntime.monotonic()\n", "monotonic"),
    ("import time\ntime.perf_counter()\n", "monotonic"),
    ("import time\ntime.time_ns()\n", "clock"),
    ("import sys\nsys.argv\n", "host"),
    ("import sys\nsys.stdin.read()\n", "host"),
    ("import os\nos.getpid()\n", "host"),
    ("import os\nos.cpu_count()\n", "host"),
    ("import socket\nsocket.gethostname()\n", "host"),
    ("import locale\nlocale.getlocale()\n", "host"),
    ("import platform\nplatform.node()\n", "host"),
    ("import platform\nplatform.system()\n", "os-branching"),
])
def test_machine_queries_have_their_own_categories(source: str, expected: str | None) -> None:
    assert categories(source) == (set() if expected is None else {expected})


def test_a_bare_monotonic_default_is_a_seam() -> None:
    assert uses("import time\ndef f(clock=time.monotonic):\n    return clock()\n") == set()


# --- policy: new categories are valid in `forbid` and in none by default ---------------------------------------------------


def test_the_new_categories_have_rules_and_are_not_forbidden_by_default() -> None:
    new = {"state", "monotonic", "host", "fs-query"}
    assert new <= EFFECT_CATEGORIES and not new & DEFAULT_FORBID_CATEGORIES
    assert {EFFECT_RULES[c] for c in new} == {Rule.EFFECT_STATE, Rule.EFFECT_MONOTONIC, Rule.EFFECT_HOST, Rule.EFFECT_FS_QUERY}
    policy = mini_policy()
    assert not new & policy.forbidden_effects("domain") and not new & policy.forbidden_effects("ports")


def test_a_ring_can_forbid_each_new_category(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os, sys, threading, time\nL = threading.Lock()\nt = time.monotonic()\nh = sys.argv\nq = os.getcwd()\n"})
    default = _effect_rules(_run(repo))
    forbidding = _effect_rules(_run(repo, ring=ring_with(domain={"external": ["os", "sys", "threading", "time"], "forbid": ["state", "monotonic", "host", "fs-query"]})))
    assert default == set()
    assert forbidding == {"effect-state", "effect-monotonic", "effect-host", "effect-fs-query"}


# --- sanctioned seams and the guard ----------------------------------------------------------------------------------------

PORT = "import os\n\n\ndef read(name):\n    return os.getenv(name)\n"


def _run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def _effect_rules(found: list[Any]) -> set[str]:
    return {finding.rule for finding in found if finding.rule.startswith("effect-")}


def _seam_repo(root: Path) -> dict[str, Any]:
    """`ports` is the provider ring (it reads the environment on purpose); `adapters` forbids env and imports it."""
    write_files(root, {"app/ports.py": PORT, "app/adapters.py": "from . import ports\n\n\ndef go():\n    return ports.read('X')\n"})
    return ring_with(
        ports={"may_import": ["domain"], "external": ["os"], "forbid": []},
        adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["env"]},
    )


def test_without_the_effects_table_a_port_taints_its_importers(repo: Path) -> None:
    found = _run(repo, ring=_seam_repo(repo))
    assert _effect_rules(found) == {"effect-via-import"}


def test_a_sanctioned_seam_does_not_propagate_its_own_uses(repo: Path) -> None:
    found = _run(repo, ring=_seam_repo(repo), effects={"sanctioned": ["app.ports"], "rings": ["ports"]})
    assert _effect_rules(found) == set()


def test_a_sanctioned_glob_and_prefix_both_match(repo: Path) -> None:
    ring = _seam_repo(repo)
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": ["app.p*"], "rings": ["ports"]})) == set()
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": ["app"], "rings": ["ports"]})) == set()


def test_importing_an_unlisted_provider_module_is_a_finding(repo: Path) -> None:
    ring = _seam_repo(repo)
    found = _run(repo, ring=ring, effects={"sanctioned": ["app.somewhere_else"], "rings": ["ports"]})
    assert _effect_rules(found) == {"effect-via-import", "effect-unsanctioned-import"}
    detail = next(f.detail for f in found if f.rule == "effect-unsanctioned-import")
    assert "app.ports" in detail and "effects.sanctioned" in detail


def test_an_empty_sanctioned_list_still_switches_the_guard_on(repo: Path) -> None:
    found = _run(repo, ring=_seam_repo(repo), effects={"sanctioned": [], "rings": ["ports"]})
    assert "effect-unsanctioned-import" in _effect_rules(found)


def test_the_guard_does_not_apply_to_a_type_only_import(repo: Path) -> None:
    ring = _seam_repo(repo)
    write_files(repo, {"app/adapters.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import ports\n"})
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": [], "rings": ["ports"]})) == set()


def test_the_guard_only_checks_rings_that_forbid_effects(repo: Path) -> None:
    ring = _seam_repo(repo)
    ring["adapters"] = {**ring["adapters"], "forbid": []}
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": [], "rings": ["ports"]})) == set()


def test_a_sanctioned_seam_that_imports_a_raw_effect_module_still_propagates(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": PORT,
        "app/ports.py": "from . import domain\n",
        "app/adapters.py": "from . import ports\n",
    })
    ring = ring_with(
        domain={"external": ["os"], "forbid": []},
        ports={"may_import": ["domain"], "external": [], "forbid": []},
        adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["env"]},
    )
    found = _run(repo, ring=ring, effects={"sanctioned": ["app.ports"], "rings": ["ports"]})
    assert [f.rule for f in found if f.rule == "effect-via-import"] == ["effect-via-import"]


def _pure_provider_repo(root: Path, provider: str) -> dict[str, Any]:
    write_files(root, {"app/ports.py": provider, "app/adapters.py": "from . import ports\n\n\ndef go():\n    return ports.VALUE\n"})
    return ring_with(
        ports={"may_import": ["domain"], "external": ["os"], "forbid": []},
        adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["env"]},
    )


def test_importing_a_pure_provider_ring_module_is_not_a_finding(repo: Path) -> None:
    """#202: `admit` steers a pure module into the effects ring; `check` must not then call its importers unsanctioned."""
    ring = _pure_provider_repo(repo, "VALUE = 1\n")
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": [], "rings": ["ports"]})) == set()


def test_an_empty_provider_module_is_not_a_finding(repo: Path) -> None:
    ring = _pure_provider_repo(repo, "")
    assert _effect_rules(_run(repo, ring=ring, effects={"sanctioned": [], "rings": ["ports"]})) == set()


def test_a_provider_module_with_a_raw_effect_is_still_a_finding(repo: Path) -> None:
    ring = _pure_provider_repo(repo, "import os\n\nVALUE = os.environ.get('X')\n")
    assert "effect-unsanctioned-import" in _effect_rules(_run(repo, ring=ring, effects={"sanctioned": [], "rings": ["ports"]}))


def test_a_pure_provider_module_that_reaches_a_raw_effect_still_taints_its_importer(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": PORT,
        "app/ports.py": "from . import domain\n\nVALUE = 1\n",
        "app/adapters.py": "from . import ports\n",
    })
    ring = ring_with(
        domain={"external": ["os"], "forbid": []},
        ports={"may_import": ["domain"], "external": [], "forbid": []},
        adapters={"may_import": ["ports"], "external": [], "forbid": ["env"]},
    )
    found = _run(repo, ring=ring, effects={"sanctioned": [], "rings": ["domain", "ports"]})
    assert "effect-via-import" in _effect_rules(found)


def test_effects_table_validation() -> None:
    assert policy_from_mapping({**_mini(), "effects": {"sanctioned": ["a.*"]}}).effects.sanctioned == ("a.*",)
    assert policy_from_mapping({**_mini(), "effects": {"sanctioned": ["a.*"]}}).effects.rings == ("effects",)
    assert not policy_from_mapping(_mini()).effects.configured
    with pytest.raises(PolicyError, match="unknown key in `effects`"):
        policy_from_mapping({**_mini(), "effects": {"sanction": []}})
    with pytest.raises(PolicyError, match="`effects` must be a table"):
        policy_from_mapping({**_mini(), "effects": ["x"]})
    with pytest.raises(PolicyError, match="at least one ring"):
        policy_from_mapping({**_mini(), "effects": {"rings": []}})


def _mini() -> dict[str, Any]:
    return dict(MINI)


# --- mixed-purity ring filter and kinds ------------------------------------------------------------------------------------

MIXED = (
    "import os\n\n\n" + "".join(f"def pure_{n}(x):\n    a = x + 1\n    b = a + 2\n    c = b + 3\n    return c\n\n\n" for n in range(2))
    + "def read():\n    return os.getenv('X')\n"
)


def _mixed(root: Path, **table: Any) -> set[str]:
    write_files(root, {"app/domain.py": MIXED, "app/main.py": MIXED})
    ring = ring_with(domain={"external": ["os"], "forbid": []}, composition={"may_import": ["*"], "external": ["os"]})
    found = _run(root, ring=ring, mixed_purity=table)
    return {f.subject for f in found if f.rule == Rule.MIXED_PURITY}


def test_mixed_purity_reports_every_ring_by_default(repo: Path) -> None:
    assert _mixed(repo) == {"app.domain", "app.main"}


def test_mixed_purity_exclude_rings_skips_those_rings(repo: Path) -> None:
    assert _mixed(repo, exclude_rings=["composition"]) == {"app.domain"}


def test_mixed_purity_kinds_restrict_what_makes_a_module_mixed(repo: Path) -> None:
    assert _mixed(repo, kinds=["clock"]) == set()
    assert _mixed(repo, kinds=["env"]) == {"app.domain", "app.main"}


def test_mixed_purity_accepts_the_new_kinds() -> None:
    policy = policy_from_mapping({**_mini(), "mixed_purity": {"kinds": ["monotonic", "host", "fs-query", "state"]}})
    assert policy.mixed_purity.kinds == ("monotonic", "host", "fs-query", "state")


def test_a_lock_still_makes_a_module_mixed_by_default(repo: Path) -> None:
    source = MIXED.replace("import os", "import threading").replace("os.getenv('X')", "threading.Lock()")
    write_files(repo, {"app/domain.py": source})
    ring = ring_with(domain={"external": ["threading"], "forbid": []})
    found = _run(repo, ring=ring)
    assert {f.subject for f in found if f.rule == Rule.MIXED_PURITY} == {"app.domain"}


# --- exclude patterns ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("rel", "pattern", "expected"), [
    ("client/a.py", "client", True),
    ("src/pkg/client/a.py", "client", True),  # an unanchored name matches a folder at any depth, like .gitignore
    ("client/a.py", "/client", True),
    ("client/deep/a.py", "/client", True),
    ("src/pkg/client/a.py", "/client", False),  # anchored: the repository root only
    ("src/pkg/client/a.py", "/src/pkg/client", True),
    ("src/pkg/client/a.py", "/src", True),
    ("src/pkg/client/a.py", "/src/*/client", True),
    ("a/clients/x.py", "/client", False),
    ("a.py", "/a.py", True),
    ("pkg/a.py", "/a.py", False),
    ("pkg/a.py", "a.py", True),
    ("client/a.py", "/client/", True),
])
def test_exclude_patterns_anchor_with_a_leading_slash(rel: str, pattern: str, expected: bool) -> None:
    assert excluded_by_policy(rel, [pattern]) is expected


def test_a_bare_name_excludes_a_nested_package_but_an_anchored_one_does_not(repo: Path) -> None:
    write_files(repo, {"app/__init__.py": "", "app/client/__init__.py": "", "app/client/loader.py": "X = 1\n", "client/__init__.py": "", "client/x.py": "Y = 1\n"})
    bare = FsScanner().scan(mini_policy(sources=["app", "client"], classify={"app*": "domain", "client*": "domain"}, exclude=["client"]), repo)
    anchored = FsScanner().scan(mini_policy(sources=["app", "client"], classify={"app*": "domain", "client*": "domain"}, exclude=["/client"]), repo)
    assert "app.client.loader" not in bare.modules and bare.excluded == {"app/client": 2, "client": 2}
    assert "app.client.loader" in anchored.modules and anchored.excluded == {"client": 2}


def test_nested_bare_pattern_names_the_entry_that_reaches_below_the_root() -> None:
    assert nested_bare_pattern("src/pkg/client", ["client"]) == "client"
    assert nested_bare_pattern("client", ["client"]) is None
    assert nested_bare_pattern("src/pkg/client", ["/client"]) is None
    assert nested_bare_pattern("src/pkg/client", ["*client"]) is None
    assert nested_bare_pattern("src/pkg/client", ["pkg/client"]) is None
    assert nested_bare_pattern("src/pkg/client", ["other"]) is None


def test_the_excluded_folders_listing_flags_a_nested_match_of_a_bare_name(capsys: pytest.CaptureFixture[str]) -> None:
    _print_excluded(CheckReport(excluded={"client": 2, "src/pkg/client": 53}, excluded_nested={"src/pkg/client": "client"}))
    lines = capsys.readouterr().out.splitlines()
    assert "  client/ (2 file(s))" in lines and "  src/pkg/client/ (53 file(s))" in lines
    assert [line for line in lines if "note:" in line] == [
        "  note: the bare entry 'client' also hid 1 folder(s) below the root (src/pkg/client/ ...); write '/client' to exclude only the top-level one"
    ]
