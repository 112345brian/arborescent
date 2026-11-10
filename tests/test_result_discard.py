"""``result_discard`` (#141): a bare call statement drops the outcome of a function marked result-bearing.

Static and syntactic: a direct call by name (``f()``, ``module.f()``, an imported alias) is resolved through the module's imports;
methods, callbacks and ``getattr`` are out of scope and are never reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules

OUTCOME = "from __future__ import annotations\n\n\nclass PlaceResult:\n    ok: bool = True\n\n\ndef place() -> PlaceResult:\n    return PlaceResult()\n\n\ndef plain() -> int:\n    return 1\n"
SETTING: dict[str, Any] = {}


def run(root: Path, setting: dict[str, Any] | None = SETTING) -> list[Violation]:
    policy = mini_policy() if setting is None else mini_policy(result_discard=setting)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def dropped(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.RESULT_DISCARD]


def caller(body: str, header: str = "from .domain import place, plain\n") -> dict[str, str]:
    return {"app/domain.py": OUTCOME, "app/adapters.py": f"from __future__ import annotations\n\n{header}\n\ndef go() -> None:\n{body}\n"}


def test_a_bare_call_to_a_result_function_is_reported(repo: Path) -> None:
    write_files(repo, caller("    place()"))
    found = run(repo)
    assert dropped(found) == [("app.adapters", 7)]
    detail = next(v.detail for v in found if v.rule == Rule.RESULT_DISCARD)
    assert "app.domain.place" in detail and "_ = " in detail


@pytest.mark.parametrize(
    "body",
    [
        "    _ = place()",
        "    r = place()\n    print(r)",
        "    if place().ok:\n        pass",
        "    print(place())",
        "    x = [place()]\n    print(x)",
        "    plain()",
    ],
)
def test_a_used_deliberately_discarded_or_unmarked_call_is_fine(repo: Path, body: str) -> None:
    write_files(repo, caller(body))
    assert dropped(run(repo)) == []


def test_return_and_await_forms(repo: Path) -> None:
    files = caller("    return place()")
    write_files(repo, files)
    assert dropped(run(repo)) == []
    write_files(repo, {"app/domain.py": OUTCOME.replace("def place", "async def place"), "app/adapters.py": files["app/adapters.py"].replace("def go() -> None:\n    return place()", "async def go() -> None:\n    await place()")})
    assert dropped(run(repo)) == [("app.adapters", 7)]


def test_module_attribute_and_alias_imports_resolve(repo: Path) -> None:
    write_files(repo, caller("    d.place()\n    other()\n    app.domain.place()", "import app.domain\nfrom . import domain as d\nfrom .domain import place as other\n"))
    assert dropped(run(repo)) == [("app.adapters", 9), ("app.adapters", 10), ("app.adapters", 11)]


def test_an_absolute_import_resolves(repo: Path) -> None:
    write_files(repo, caller("    place()", "from app.domain import place\n"))
    assert dropped(run(repo)) == [("app.adapters", 7)]


def test_a_call_within_the_defining_module_is_reported(repo: Path) -> None:
    write_files(repo, {"app/domain.py": OUTCOME + "\n\ndef again() -> None:\n    place()\n"})
    assert dropped(run(repo)) == [("app.domain", 17)]


@pytest.mark.parametrize(
    "annotation",
    ["-> PlaceResult", "-> 'PlaceResult'", "-> PlaceResult | None", "-> Optional[PlaceResult]", "-> mod.PlaceOutcome"],
)
def test_the_naming_convention_covers_common_annotation_shapes(annotation: str) -> None:
    facts = extract(f"def f() {annotation}:\n    ...\n")
    assert facts.result_defs == ("f",)


@pytest.mark.parametrize("annotation", ["", "-> int", "-> ResultSet", "-> list[PlaceResult]", "-> Result_", "-> Result", "-> Outcome", "-> 'a b'"])
def test_other_annotations_are_not_result_bearing(annotation: str) -> None:
    source = f"def f() {annotation}:\n    ...\n"
    try:
        facts = extract(source)
    except SyntaxError:
        return
    assert facts.result_defs == ()


def test_a_nested_or_method_definition_is_not_a_marked_function() -> None:
    facts = extract("class C:\n    def m(self) -> XResult: ...\n\ndef outer():\n    def inner() -> XResult: ...\n")
    assert facts.result_defs == ()


def test_the_config_list_marks_a_function_with_any_annotation(repo: Path) -> None:
    write_files(repo, caller("    plain()"))
    assert dropped(run(repo, {"functions": ["app.domain.plain"]})) == [("app.adapters", 7)]


def test_the_convention_can_be_switched_off(repo: Path) -> None:
    write_files(repo, caller("    place()"))
    assert dropped(run(repo, {"convention": False})) == []


def test_rings_scope_where_discards_are_reported(repo: Path) -> None:
    write_files(repo, {**caller("    place()"), "app/main.py": "from . import domain\n\n\ndef go() -> None:\n    domain.place()\n"})
    assert dropped(run(repo, {"rings": ["composition"]})) == [("app.main", 5)]
    assert {s for s, _ in dropped(run(repo))} == {"app.adapters", "app.main"}


def test_it_is_off_without_the_setting(repo: Path) -> None:
    write_files(repo, caller("    place()"))
    assert dropped(run(repo, None)) == []


def test_dynamic_calls_are_out_of_scope(repo: Path) -> None:
    body = "    fn = place\n    fn()\n    getattr(domain_module, 'place')()\n    self_like.place()\n    [place][0]()"
    write_files(repo, caller(body))
    assert dropped(run(repo)) == []


def test_a_local_name_that_is_not_imported_is_not_resolved(repo: Path) -> None:
    write_files(repo, caller("    place()", ""))
    assert dropped(run(repo)) == []


def test_unparseable_or_empty_targets_do_not_crash(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "", "app/adapters.py": "from . import domain\n\ndomain.missing()\n(lambda: 1)()\n"})
    assert dropped(run(repo)) == []


def test_the_existing_discard_inventory_is_untouched() -> None:
    facts = extract("discard(f(), reason='x')\nf()\n")
    assert facts.discard_calls == (1,)
    assert facts.bare_calls == ((1, "discard"), (2, "f"))


def test_bare_call_facts_record_the_resolved_dotted_target() -> None:
    facts = extract("import a.b as m\nfrom c import d\nfrom .e import f\nm.x()\nd()\nf()\nlocal()\n")
    assert facts.bare_calls == ((4, "a.b.x"), (5, "c.d"), (6, ".e.f"), (7, "local"))


@pytest.mark.parametrize(
    "value",
    ["x", {"functions": "a.b"}, {"functions": [""]}, {"functions": [1]}, {"rings": "adapters"}, {"rings": [""]}, {"convention": "yes"}, {"surprise": 1}],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="result_discard"):
        policy_from_mapping({**MINI, "result_discard": value})


def test_it_is_parsed_into_a_policy_entry() -> None:
    assert mini_policy().result_discard is None
    entry = mini_policy(result_discard={"functions": ["a.b"], "rings": ["adapters"]}).result_discard
    assert entry is not None
    assert (entry.functions, entry.rings, entry.convention) == (("a.b",), ("adapters",), True)
