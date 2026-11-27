"""#194: no category became default-on. ``effect-unlisted-category`` says which detected categories no ring forbids: one note
per category, counting the modules of restricted rings that use it directly. Informational: never fails, never baselined.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, services, write_files

from arborescent.check_runner import run_check
from arborescent.config import policy_from_mapping
from arborescent.domain import INFORMATIONAL_RULES, Policy, Rule
from arborescent.engine import Options, enforced, run_own
from arborescent.presets import CATEGORY_TRAITS

# one use of every category the note covers
USES = {
    "console": "def f():\n    print()\n",
    "network": "import urllib.request\ndef f():\n    urllib.request.urlopen('u')\n",
    "global": "X = 0\ndef f():\n    global X\n    X = 1\n",
    "registry": "REG = {}\ndef f():\n    REG['a'] = 1\n",
    "cache": "import functools\n@functools.cache\ndef f():\n    return 1\n",
    "state": "import threading\nLOCK = threading.Lock()\n",
    "monotonic": "import time\ndef f():\n    time.monotonic()\n",
    "host": "import os\ndef f():\n    os.getpid()\n",
    "fs-query": "import os\ndef f():\n    os.getcwd()\n",
}
# a policy whose restricted rings forbid something else, so every category above is unlisted
BARE = ring_with(**{name: {"may_import": ["*"], "external": ["*"], "traits": ["no-env"]} for name in ("domain", "ports", "adapters")})


def notes(root: Path, policy: Policy | None = None, only: frozenset[str] | None = None) -> dict[str, str]:
    found, _ = run_own(policy or mini_policy(ring=BARE), root, services(), only, practices=False)
    return {v.subject: v.detail for v in found if v.rule == Rule.EFFECT_UNLISTED_CATEGORY}


def one(count: int, category: str) -> str:
    verb = "module in restricted rings uses" if count == 1 else "modules in restricted rings use"
    return f"{count} {verb} {category}; no ring forbids it"


@pytest.mark.parametrize("category", sorted(USES))
def test_each_category_is_noted_when_no_ring_forbids_it(repo: Path, category: str) -> None:
    write_files(repo, {"app/ports.py": USES[category]})
    assert notes(repo) == {category: one(1, category)}


def test_modules_are_counted_once_each_and_only_in_restricted_rings(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "def f():\n    print()\n    print()\n",
        "app/ports.py": USES["console"],
        "app/main.py": USES["console"],  # composition forbids nothing: not counted
    })
    assert notes(repo) == {"console": one(2, "console")}


@pytest.mark.parametrize("category", sorted(USES))
def test_listing_the_category_in_any_ring_silences_it(repo: Path, category: str) -> None:
    write_files(repo, {"app/ports.py": USES[category]})
    ring = {**BARE, "adapters": {**BARE["adapters"], "traits": [CATEGORY_TRAITS[category]]}}  # not the ring that uses it
    assert notes(repo, mini_policy(ring=ring)) == {}


def test_call_time_and_at_import_traits_count_as_listing(repo: Path) -> None:
    write_files(repo, {"app/domain.py": USES["state"], "app/ports.py": USES["host"]})
    plain = {name: {"may_import": ["*"], "external": ["*"], "traits": ["no-host"]} for name in ("ports", "adapters")}
    ring = ring_with(domain={"external": ["*"], "traits": ["no-env", "no-sync-primitives-at-import"]}, **plain)
    assert notes(repo, mini_policy(ring=ring)) == {}
    bare_domain = {**ring, "domain": {"external": ["*"], "traits": ["no-env"]}}
    assert set(notes(repo, mini_policy(ring=bare_domain))) == {"state"}


def test_the_default_policy_notes_only_what_it_does_not_already_forbid(repo: Path) -> None:
    write_files(repo, {"app/ports.py": USES["console"], "app/domain.py": USES["registry"]})
    assert notes(repo, mini_policy()) == {"registry": one(1, "registry")}


def test_a_write_to_another_modules_container_is_counted_without_any_opt_in(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "REG = {}\n",
        "app/ports.py": "from . import domain\ndef f():\n    domain.REG['a'] = 1\n",
    })
    assert notes(repo) == {"registry": one(1, "registry")}  # the writer, not the module that only owns the container


def test_only_direct_uses_count_never_what_an_import_reaches(repo: Path) -> None:
    write_files(repo, {"app/domain.py": USES["console"], "app/ports.py": "from . import domain\ndef g():\n    domain.f()\n"})
    assert notes(repo) == {"console": one(1, "console")}


def test_an_injection_seam_is_not_a_use(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import os, time\ndef f(pid=os.getpid, clock=time.monotonic):\n    pid()\n"})
    assert notes(repo) == {}


def test_no_restricted_ring_or_no_use_means_no_note(repo: Path) -> None:
    assert notes(repo) == {}
    write_files(repo, {"app/ports.py": USES["console"]})
    open_rings = ring_with(**{name: {"may_import": ["*"], "external": ["*"], "traits": ["permits-effects"]} for name in ("domain", "ports", "adapters")})
    assert notes(repo, mini_policy(ring=open_rings)) == {}


def test_a_partial_scan_says_nothing(repo: Path) -> None:
    write_files(repo, {"app/ports.py": USES["console"]})
    assert notes(repo, only=frozenset({"app/ports.py"})) == {}


def test_it_is_a_note_that_never_fails_and_is_never_baselined(repo: Path) -> None:
    write_files(repo, {"app/ports.py": USES["console"]})
    policy = mini_policy(ring=BARE)
    report = run_check(policy, repo, services(), Options(tools=False))
    assert Rule.EFFECT_UNLISTED_CATEGORY in INFORMATIONAL_RULES
    (note,) = [v for v in report.notes if v.rule == Rule.EFFECT_UNLISTED_CATEGORY]
    assert not [v for v in report.violations if v.rule == Rule.EFFECT_UNLISTED_CATEGORY]
    assert note.render().startswith("console: [effect-unlisted-category] 1 module") and "`traits`" in note.render()
    kept, demoted = enforced(policy, run_own(policy, repo, services(), practices=False)[0])
    assert Rule.EFFECT_UNLISTED_CATEGORY not in {v.rule for v in [*kept, *demoted]}


def _scope(name: str, forbid: list[str]) -> dict[str, Any]:
    return {
        "sources": [f"libs/{name}/src/{name}"], "source_roots": [f"libs/{name}/src"], "rings": ["core", "outer"],
        "classify": {f"{name}*": "core"}, "ring": {"core": {"external": ["*"], "traits": [CATEGORY_TRAITS[category] for category in forbid]}},
    }


def test_each_scope_is_judged_by_its_own_rings(repo: Path) -> None:
    write_files(repo, {f"libs/{name}/src/{name}/__init__.py": USES["console"] for name in ("alpha", "beta")})
    scoped = policy_from_mapping({"scopes": {"alpha": _scope("alpha", ["env"]), "beta": _scope("beta", ["console"])}})
    assert notes(repo, scoped) == {"alpha:console": one(1, "console")}
