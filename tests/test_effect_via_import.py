"""Rule `effect-via-import`: a ring that forbids an effect still reaches it through a helper in a ring that allows it (#103)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.rules import check_modules

RINGS = ["domain", "lib", "service", "composition"]
RING = {
    "domain": {"external": ["@pure"]},
    "lib": {"may_import": ["domain"], "external": ["@pure", "os"], "forbid": []},
    "service": {"may_import": ["domain", "lib"], "external": ["@pure"], "forbid": ["console", "filesystem"]},
    "composition": {"may_import": ["*"], "external": ["@pure"]},
}
CLASSIFY = {
    "app": "domain",
    "app.domain": "domain",
    "app.lib": "lib",
    "app.mid": "lib",
    "app.leaf": "lib",
    "app.service": "service",
    "app.service2": "service",
    "app.service3": "service",
    "app.main": "composition",
}
PRINTER = "def show(text):\n    print(text)\n"


def run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(rings=RINGS, ring=RING, classify=CLASSIFY, **overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def via(found: list[Any]) -> list[Any]:
    return [finding for finding in found if finding.rule == "effect-via-import"]


def test_an_effect_reached_through_one_import_is_reported_with_the_chain(repo: Path) -> None:
    write_files(repo, {"app/lib.py": PRINTER, "app/service.py": "from . import lib\n\ndef run():\n    lib.show('x')\n"})

    (finding,) = via(run(repo))

    assert finding.subject == "app.service"
    assert "app.service -> app.lib" in finding.detail
    assert "console" in finding.detail
    assert "print" in finding.detail
    assert "line 2" in finding.detail
    assert finding.line == 1


def test_the_chain_names_every_hop_through_two_imports(repo: Path) -> None:
    write_files(repo, {
        "app/leaf.py": PRINTER,
        "app/mid.py": "from . import leaf\n",
        "app/service.py": "from . import mid\n",
    })

    (finding,) = via(run(repo))

    assert "app.service -> app.mid -> app.leaf" in finding.detail


def test_only_the_importer_whose_ring_forbids_the_effect_is_reported(repo: Path) -> None:
    write_files(repo, {
        "app/lib.py": PRINTER,
        "app/mid.py": "from . import lib\n",
        "app/main.py": "from . import lib\n",
    })

    assert via(run(repo)) == []


def test_a_ring_that_allows_the_effect_inherits_nothing(repo: Path) -> None:
    write_files(repo, {"app/leaf.py": PRINTER, "app/mid.py": "from . import leaf\n", "app/main.py": "from . import mid\n"})

    assert via(run(repo)) == []


def test_a_type_checking_import_does_not_propagate(repo: Path) -> None:
    write_files(repo, {
        "app/lib.py": PRINTER,
        "app/service.py": "from __future__ import annotations\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import lib\n",
    })

    assert via(run(repo)) == []


def test_a_lazy_function_level_import_still_propagates(repo: Path) -> None:
    write_files(repo, {"app/lib.py": PRINTER, "app/service.py": "def run():\n    from . import lib\n    lib.show('x')\n"})

    assert [finding.subject for finding in via(run(repo))] == ["app.service"]


def test_a_forbidding_ring_is_a_barrier_so_a_direct_use_is_not_reported_twice(repo: Path) -> None:
    write_files(repo, {"app/service2.py": PRINTER, "app/service3.py": "from . import service2\n"})

    found = run(repo)

    assert via(found) == []
    assert {(finding.rule, finding.subject) for finding in found if finding.rule == "effect-console"} == {("effect-console", "app.service2")}


def test_a_module_with_its_own_use_gets_only_the_direct_finding(repo: Path) -> None:
    write_files(repo, {"app/lib.py": PRINTER, "app/service.py": "from . import lib\n\ndef run():\n    print('x')\n"})

    found = run(repo)

    assert via(found) == []
    assert {(finding.rule, finding.subject) for finding in found if finding.rule == "effect-console"} == {("effect-console", "app.service")}


def test_each_forbidden_category_is_reported_once_per_module(repo: Path) -> None:
    write_files(repo, {
        "app/lib.py": "def show(path):\n    print(path)\n    return open(path)\n",
        "app/mid.py": "from . import lib\n",
        "app/service.py": "from . import lib, mid\n",
    })

    found = via(run(repo))

    assert sorted((finding.subject, "console" in finding.detail) for finding in found) == [("app.service", False), ("app.service", True)]
    assert any("filesystem" in finding.detail for finding in found)


def test_an_effect_injected_as_a_default_does_not_propagate(repo: Path) -> None:
    write_files(repo, {"app/lib.py": "def show(text, out=print):\n    out(text)\n", "app/service.py": "from . import lib\n"})

    assert via(run(repo)) == []


def test_an_import_cycle_terminates(repo: Path) -> None:
    write_files(repo, {
        "app/mid.py": "from . import leaf\n",
        "app/leaf.py": "from . import mid\n" + PRINTER,
        "app/service.py": "from . import mid\n",
    })

    assert [finding.subject for finding in via(run(repo))] == ["app.service"]


def test_a_module_imported_by_no_one_and_a_module_importing_nothing_are_fine(repo: Path) -> None:
    write_files(repo, {"app/lib.py": PRINTER, "app/service.py": "VALUE = 1\n"})

    assert via(run(repo)) == []


def test_an_unresolvable_import_does_not_crash_the_pass(repo: Path) -> None:
    write_files(repo, {"app/lib.py": PRINTER, "app/service.py": "from .. import nothing\nfrom . import lib\n"})

    assert [finding.subject for finding in via(run(repo))] == ["app.service"]
