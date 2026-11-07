"""#98: function-level call-graph taint, on small synthetic repositories (mirrors test_rules.py's style)."""

from __future__ import annotations

from collections.abc import Mapping

from arborescent.facts import ModuleInfo, extract
from arborescent.taint import DEFAULT_TAINT_SEEDS, TaintResult, compute_taint

_PACKAGES = frozenset({"pkg"})  # module names in the fixtures below that are packages (their own __init__.py)


def build(sources: Mapping[str, str]) -> tuple[dict[str, ModuleInfo], dict[str, str]]:
    modules = {
        name: ModuleInfo(name, name.replace(".", "/") + ".py", name in _PACKAGES, extract(text))
        for name, text in sources.items()
    }
    return modules, dict(sources)


def run(sources: Mapping[str, str], seeds: tuple[str, ...] = DEFAULT_TAINT_SEEDS) -> TaintResult:
    modules, texts = build(sources)
    return compute_taint(modules, texts, frozenset(modules), seeds)


def test_a_direct_io_call_taints_its_own_function() -> None:
    result = run({"app": "def read() -> str:\n    return open('f').read()\n"})
    assert result.by_module["app"] == ("read",)


def test_a_function_with_no_io_and_no_tainted_call_is_clean() -> None:
    result = run({"app": "def double(x: int) -> int:\n    return x * 2\n"})
    assert result.tainted == {}


def test_one_hop_propagation_through_a_local_call() -> None:
    source = "def helper() -> None:\n    print('hi')\n\n\ndef caller() -> None:\n    helper()\n"
    result = run({"app": source})
    assert result.by_module["app"] == ("caller", "helper")


def test_multi_hop_propagation_through_several_local_calls() -> None:
    source = (
        "def a() -> None:\n    open('f')\n\n\ndef b() -> None:\n    a()\n\n\ndef c() -> None:\n    b()\n\n\ndef clean() -> None:\n    pass\n"
    )
    result = run({"app": source})
    assert result.by_module["app"] == ("a", "b", "c")
    [tainted_c] = [t for t in result.tainted.values() if t.qualname == "c"]
    assert tainted_c.chain == ("app:c", "app:b", "app:a")


def test_self_method_calls_propagate_within_a_class() -> None:
    source = (
        "class Service:\n"
        "    def run(self) -> None:\n"
        "        self.touch()\n\n"
        "    def touch(self) -> None:\n"
        "        open('f')\n"
    )
    result = run({"app": source})
    assert result.by_module["app"] == ("Service.run", "Service.touch")


def test_constructor_tracking_follows_a_local_variable_to_its_class() -> None:
    source = (
        "class Service:\n"
        "    def touch(self) -> None:\n"
        "        open('f')\n\n\n"
        "def caller() -> None:\n"
        "    svc = Service()\n"
        "    svc.touch()\n"
    )
    result = run({"app": source})
    assert result.by_module["app"] == ("Service.touch", "caller")


def test_constructor_call_itself_taints_when_init_does_io() -> None:
    source = (
        "class Service:\n"
        "    def __init__(self) -> None:\n"
        "        open('f')\n\n\n"
        "def caller() -> None:\n"
        "    Service()\n"
    )
    result = run({"app": source})
    assert result.by_module["app"] == ("Service.__init__", "caller")


def test_import_alias_propagates_to_the_aliased_function() -> None:
    sources = {
        "io_mod": "def touch() -> None:\n    open('f')\n",
        "app": "from io_mod import touch as do_touch\n\n\ndef caller() -> None:\n    do_touch()\n",
    }
    result = run(sources)
    assert result.by_module == {"io_mod": ("touch",), "app": ("caller",)}


def test_whole_module_import_alias_propagates_through_an_attribute_call() -> None:
    sources = {
        "io_mod": "def touch() -> None:\n    open('f')\n",
        "app": "import io_mod as m\n\n\ndef caller() -> None:\n    m.touch()\n",
    }
    result = run(sources)
    assert result.by_module["app"] == ("caller",)


def test_reexport_through_an_init_module_is_followed() -> None:
    sources = {
        "pkg": "from .sub import touch\n",  # pkg/__init__.py re-exporting a name from pkg/sub.py
        "pkg.sub": "def touch() -> None:\n    open('f')\n",
        "app": "from pkg import touch\n\n\ndef caller() -> None:\n    touch()\n",
    }
    result = run(sources)
    assert result.by_module == {"pkg.sub": ("touch",), "app": ("caller",)}


def test_a_local_variable_shadowing_a_library_name_is_not_a_seed() -> None:
    source = "requests = [1, 2, 3]\n\n\ndef caller() -> None:\n    requests.append(4)\n"
    result = run({"app": source})
    assert result.tainted == {}


def test_urllib_parse_is_not_a_network_seed() -> None:
    source = "import urllib.parse\n\n\ndef caller() -> None:\n    urllib.parse.urlsplit('http://x')\n"
    result = run({"app": source})
    assert result.tainted == {}


def test_urllib_request_is_a_network_seed() -> None:
    source = "import urllib.request\n\n\ndef caller() -> None:\n    urllib.request.urlopen('http://x')\n"
    result = run({"app": source})
    assert result.by_module["app"] == ("caller",)


def test_output_ordering_is_deterministic_regardless_of_input_order() -> None:
    sources = {
        "z_mod": "def z() -> None:\n    open('f')\n",
        "a_mod": "def a() -> None:\n    print('hi')\n",
    }
    forward = run(sources)
    backward = run(dict(reversed(list(sources.items()))))
    assert list(forward.tainted) == list(backward.tainted) == sorted(forward.tainted)
    assert forward.by_module == backward.by_module


def test_seed_list_is_configurable() -> None:
    source = "def talk() -> None:\n    print('hi')\n"
    assert run({"app": source}, seeds=()).tainted == {}
    assert run({"app": source}, seeds=("console",)).by_module["app"] == ("talk",)


def test_a_module_missing_from_sources_contributes_no_edges_without_erroring() -> None:
    modules, _ = build({"app": "def f() -> None:\n    open('f')\n"})
    result = compute_taint(modules, {}, frozenset(modules))
    assert result.tainted == {}


def test_a_call_through_an_unknown_object_is_not_followed() -> None:
    source = "def caller(helper: object) -> None:\n    helper.do_it()\n"
    result = run({"app": source})
    assert result.tainted == {}


def test_a_callback_only_ever_passed_is_not_tainted_by_that_alone() -> None:
    source = (
        "def effectful() -> None:\n    open('f')\n\n\n"
        "def register(cb: object) -> None:\n    pass\n\n\n"
        "def caller() -> None:\n    register(effectful)\n"
    )
    result = run({"app": source})
    assert result.by_module["app"] == ("effectful",)
    assert "caller" not in result.by_module.get("app", ())
    assert "register" not in result.by_module.get("app", ())
