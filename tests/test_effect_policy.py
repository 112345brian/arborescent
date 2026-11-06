from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import CLASSIFY, mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError
from arborescent.rules import check_modules


def run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def effects(found: list[Any]) -> set[tuple[str, str]]:
    return {(finding.rule, finding.subject) for finding in found if finding.rule.startswith("effect-")}


def test_innermost_ring_forbids_effects_and_outermost_allows_them(repo: Path) -> None:
    source = "import os, time, uuid\nos.getenv('MODE')\ntime.time()\nuuid.uuid4()\n"
    # ports and adapters neutralized: they otherwise import domain and would also inherit its effects (effect-via-import).
    write_files(repo, {"app/domain.py": source, "app/main.py": source, "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n"})
    policy_rings = ring_with(
        domain={"external": ["os", "time", "uuid"]},
        composition={"may_import": ["*"], "external": ["os", "time", "uuid"]},
    )

    found = run(repo, ring=policy_rings)

    assert effects(found) == {
        ("effect-env", "app.domain"),
        ("effect-clock", "app.domain"),
        ("effect-identity", "app.domain"),
    }


def test_per_ring_forbid_override_replaces_the_preset_default(repo: Path) -> None:
    # adapters neutralized: it otherwise imports ports and would also inherit the clock effect (effect-via-import).
    write_files(repo, {"app/ports.py": "import time\ntime.time()\n", "app/adapters.py": "X = 1\n"})
    open_policy = ring_with(ports={"may_import": ["domain"], "external": ["time"], "forbid": []})
    targeted_policy = ring_with(ports={"may_import": ["domain"], "external": ["time"], "forbid": ["clock"]})

    assert effects(run(repo, ring=open_policy)) == set()
    assert effects(run(repo, ring=targeted_policy)) == {("effect-clock", "app.ports")}


def test_any_forbidden_effect_also_forbids_dynamic_import(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import importlib\nimportlib.import_module('app.adapters')\n", "app/adapters.py": "X = 1\n"})
    policy_rings = ring_with(ports={"may_import": ["domain"], "external": ["importlib"], "forbid": ["clock"]})

    assert effects(run(repo, ring=policy_rings)) == {("effect-dynamic-import", "app.ports")}


def test_standard_preset_middle_ring_uses_io_effect_defaults(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "import os, uuid\nos.getenv('MODE')\nuuid.uuid4()\n"})
    policy_rings = ring_with(adapters={"may_import": ["domain", "ports"], "external": ["os", "uuid"]})

    assert effects(run(repo, preset="standard", ring=policy_rings)) == {
        ("effect-env", "app.adapters"),
    }


def test_custom_ring_names_get_positional_defaults(repo: Path) -> None:
    write_files(repo, {"app/core.py": "import time\ntime.time()\n", "app/shell.py": "import time\ntime.time()\n"})
    policy = policy_from_mapping(
        {
            "sources": ["app"],
            "rings": ["core", "shell"],
            "ring": {"core": {"external": ["time"]}, "shell": {"may_import": ["*"], "external": ["time"]}},
            "classify": {"app.core": "core", "app.shell": "shell"},
        }
    )
    scan = FsScanner().scan(policy, repo)

    assert effects(check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)) == {("effect-clock", "app.core")}


def test_patch_target_uses_its_project_ring_and_only_in_configured_tests(repo: Path) -> None:
    write_files(
        repo,
        {
            "app/domain.py": "VALUE = 1\n",
            "tests/test_domain.py": 'from unittest.mock import patch\npatch("app.domain.VALUE")  # noqa\n',
            "app/main.py": 'from unittest.mock import patch\npatch("app.domain.VALUE")\n',
        },
    )
    raw = {
        "sources": ["app", "tests"],
        "source_roots": ["."],
        "rings": ["domain", "ports", "adapters", "composition"],
        "groups": {"pure": ["__future__"]},
        "ring": ring_with(domain={"external": ["unittest"]}, composition={"may_import": ["*"], "external": ["unittest"]}),
        "classify": {**CLASSIFY, "tests.*": "composition"},
        "practices": {"secrets-guard": "tests"},
    }
    policy = policy_from_mapping(raw)
    scan = FsScanner().scan(policy, repo)

    findings = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)

    assert effects(findings) == {("effect-patch", "tests.test_domain")}


def test_patch_target_annotation_wins_over_broad_classify_on_attribute(repo: Path) -> None:
    write_files(
        repo,
        {
            "app/domain.py": '__arbor__ = {"ring": "domain"}\nVALUE = 1\n',
            "tests/test_domain.py": 'from unittest.mock import patch\npatch("app.domain.VALUE")\n',
        },
    )
    policy = policy_from_mapping(
        {
            "sources": ["app", "tests"],
            "source_roots": ["."],
            "rings": ["domain", "ports", "adapters", "composition"],
            "groups": {"pure": ["__future__"]},
            "ring": ring_with(
                domain={"external": ["unittest"]},
                composition={"may_import": ["*"], "external": ["unittest"]},
            ),
            "classify": {"app*": "composition", "tests.*": "composition"},
            "practices": {"secrets-guard": "tests"},
        }
    )
    scan = FsScanner().scan(policy, repo)

    findings = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)

    assert effects(findings) == {("effect-patch", "tests.test_domain")}


@pytest.mark.parametrize("bad", ["unknown", 3, ["clock", "unknown"], None])
def test_unknown_or_malformed_forbid_policy_is_rejected(bad: object) -> None:
    raw = {"rings": ["domain", "composition"], "ring": {"domain": {"forbid": bad}}, "classify": {"app": "domain"}}

    with pytest.raises(PolicyError, match="forbid"):
        _ = policy_from_mapping(raw)


def test_relaxed_preset_enforces_no_middle_ring_effects_by_default(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import time\ntime.time()\n"})

    assert effects(run(repo, preset="relaxed")) == set()


def test_middle_forbid_opts_a_relaxed_policy_into_effect_enforcement_without_io_allow_lists(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import time, os\ntime.time()\nos.getcwd()\n", "app/adapters.py": "X = 1\n"})

    found = run(repo, preset="relaxed", middle_forbid=["clock"])

    # `time` and `os` are stdlib, so `relaxed` still asks for no allow-list entry; only the effect is reported.
    assert [f.rule for f in found] == ["effect-clock"]


def test_a_ring_forbid_table_still_beats_the_middle_forbid_key(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import time\ntime.time()\n", "app/adapters.py": "X = 1\n"})
    open_ring = ring_with(ports={"may_import": ["domain"], "forbid": []})

    assert effects(run(repo, preset="relaxed", middle_forbid=["clock"], ring=open_ring)) == set()


def test_middle_forbid_applies_to_the_innermost_and_outermost_rings_not_at_all(repo: Path) -> None:
    source = "import time\ntime.time()\n"
    write_files(repo, {"app/main.py": source})

    assert effects(run(repo, preset="relaxed", middle_forbid=["clock"])) == set()


@pytest.mark.parametrize("bad", ["clock", [""], [3], ["not-an-effect"]])
def test_middle_forbid_rejects_malformed_values(bad: object) -> None:
    with pytest.raises(PolicyError, match="middle_forbid"):
        mini_policy(preset="relaxed", middle_forbid=bad)


def test_middle_forbid_adds_dynamic_import_like_any_forbidding_ring(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "def f(n: str) -> object:\n    return __import__(n)\n"})

    assert any(f.rule == "effect-dynamic-import" for f in run(repo, preset="relaxed", middle_forbid=["clock"]))


def test_middle_forbid_is_reported_as_a_customization() -> None:
    assert "forbid" in mini_policy(preset="relaxed", middle_forbid=["clock"]).customization_labels


def test_middle_forbid_is_set_per_scope() -> None:
    scope = {"rings": ["domain", "ports", "adapters"], "classify": {}, "preset": "relaxed"}
    raw = {
        "scopes": {
            "a": {**scope, "sources": ["a/src"], "source_roots": ["a/src"], "middle_forbid": ["clock"]},
            "b": {**scope, "sources": ["b/src"], "source_roots": ["b/src"]},
        }
    }
    policy = policy_from_mapping(raw)

    assert policy.scope("a").policy.forbidden_effects("ports") >= {"clock", "dynamic-import"}
    assert policy.scope("b").policy.forbidden_effects("ports") == frozenset()


def test_database_and_filesystem_access_are_effects_a_ring_can_forbid(repo: Path) -> None:
    source = (
        "import shutil, sqlite3\n"
        "from pathlib import Path\n"
        "def load(path):\n"
        "    db = sqlite3.connect('x.db')\n"
        "    with open(path) as handle:\n"
        "        handle.read()\n"
        "    Path(path).write_text('x')\n"
        "    shutil.copy(path, path)\n"
    )
    write_files(repo, {"app/ports.py": source, "app/adapters.py": "X = 1\n"})
    ports = ring_with(ports={"may_import": ["domain"], "external": ["shutil", "sqlite3", "pathlib"], "forbid": ["database", "filesystem"]})

    assert effects(run(repo, ring=ports)) == {("effect-database", "app.ports"), ("effect-filesystem", "app.ports")}
    only_db = ring_with(ports={"may_import": ["domain"], "external": ["shutil", "sqlite3", "pathlib"], "forbid": ["database"]})
    assert effects(run(repo, ring=only_db)) == {("effect-database", "app.ports")}


def test_filesystem_and_database_effects_are_forbidden_in_the_innermost_ring_by_default(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import sqlite3\ndef read(p):\n    sqlite3.connect(p)\n    return open(p).read()\n",
        "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n",
    })
    domain = ring_with(domain={"external": ["sqlite3"]})
    allowed = ring_with(domain={"external": ["sqlite3"], "forbid": []})

    assert effects(run(repo, ring=domain)) == {("effect-database", "app.domain"), ("effect-filesystem", "app.domain")}
    assert effects(run(repo, ring=allowed)) == set()


def test_a_local_named_open_and_non_io_helpers_are_not_filesystem_use(repo: Path) -> None:
    source = (
        "import shutil, tempfile\n"
        "def f(open):\n    return open\n"
        "def g():\n    open = 1\n    return open\n"
        "def h():\n    return shutil.which('x'), tempfile.gettempdir()\n"
    )
    write_files(repo, {"app/domain.py": source})
    domain = ring_with(domain={"external": ["shutil", "tempfile"], "forbid": ["filesystem"]})

    assert effects(run(repo, ring=domain)) == set()


def test_tempfile_and_shutil_file_operations_are_filesystem_use(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import shutil, tempfile\ndef f():\n    tempfile.mkdtemp()\n    shutil.rmtree('x')\n",
        "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n",
    })
    domain = ring_with(domain={"external": ["shutil", "tempfile"], "forbid": ["filesystem"]})

    assert effects(run(repo, ring=domain)) == {("effect-filesystem", "app.domain")}


def test_pure_path_manipulation_is_not_filesystem_access(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from pathlib import Path\ndef name(p):\n    return Path(p).name + str(Path(p).parent)\n"})

    assert effects(run(repo, ring=ring_with(domain={"external": ["pathlib"]}))) == set()


SEAM_SOURCE = (
    "import time\nfrom typing import Callable\n\n"
    "def typed(clock: Callable[[], float] = time.time): return clock()\n"
    "def untyped(clock=time.time): return clock()\n"
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("injection", set()), ("typed", {("effect-clock", "app.domain")}), ("count", {("effect-clock", "app.domain")})],
)
def test_effect_defaults_decides_whether_a_parameter_default_is_a_use(repo: Path, mode: str, expected: set[tuple[str, str]]) -> None:
    write_files(repo, {"app/domain.py": SEAM_SOURCE, "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n"})
    domain = ring_with(domain={"external": ["time", "typing"]})

    assert effects(run(repo, ring=domain, effect_defaults=mode)) == expected


def test_effect_defaults_counts_each_seam_kind_by_mode(repo: Path) -> None:
    write_files(repo, {"app/domain.py": SEAM_SOURCE})
    domain = ring_with(domain={"external": ["time", "typing"]})
    lines = {mode: sorted(f.line for f in run(repo, ring=domain, effect_defaults=mode) if f.rule == "effect-clock") for mode in ("injection", "typed", "count")}

    assert lines == {"injection": [], "typed": [5], "count": [4, 5]}


def test_effect_defaults_rejects_unknown_modes() -> None:
    with pytest.raises(PolicyError, match="effect_defaults"):
        mini_policy(effect_defaults="sometimes")


def test_console_effect_flags_print_input_and_stream_writes(repo: Path) -> None:
    source = "import sys\ndef f():\n    print('hi')\n    input()\n    sys.stdout.write('x')\n    sys.stderr.write('y')\n"
    write_files(repo, {"app/domain.py": source, "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n"})
    domain = ring_with(domain={"external": ["sys"], "forbid": ["console"]})

    assert effects(run(repo, ring=domain)) == {("effect-console", "app.domain")}


def test_console_effect_is_allowed_in_the_outermost_ring_by_default(repo: Path) -> None:
    write_files(repo, {"app/main.py": "def f():\n    print('hi')\n"})

    assert effects(run(repo)) == set()


def test_a_local_print_or_input_is_not_console_use(repo: Path) -> None:
    source = "def f(print):\n    return print\ndef g():\n    input = 1\n    return input\n"
    write_files(repo, {"app/domain.py": source})
    domain = ring_with(domain={"forbid": ["console"]})

    assert effects(run(repo, ring=domain)) == set()


def test_global_effect_flags_a_function_that_rebinds_a_module_global(repo: Path) -> None:
    source = (
        "_cache: dict[str, int] = {}\n"
        "def set_cache(value):\n    global _cache\n    _cache = value\n"
        "def read_cache():\n    global _cache\n    return _cache\n"
    )
    write_files(repo, {"app/domain.py": source, "app/ports.py": "X = 1\n", "app/adapters.py": "X = 1\n"})
    domain = ring_with(domain={"forbid": ["global"]})

    assert effects(run(repo, ring=domain)) == {("effect-global", "app.domain")}


def test_global_effect_is_not_confused_by_a_same_named_comprehension_variable(repo: Path) -> None:
    source = "def f():\n    global x\n    return [x for x in range(3)]\n"
    write_files(repo, {"app/domain.py": source})
    domain = ring_with(domain={"forbid": ["global"]})

    assert effects(run(repo, ring=domain)) == set()


def test_effect_via_import_propagates_one_and_two_hops_with_a_chain(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import os\ndef read():\n    return os.getenv('MODE')\n",
        "app/ports.py": "from . import domain\n",
        "app/adapters.py": "from . import ports\n",
    })
    ring = ring_with(
        domain={"external": ["os"], "forbid": ["env"]},
        ports={"may_import": ["domain"], "external": [], "forbid": ["env"]},
        adapters={"may_import": ["domain", "ports"], "external": [], "forbid": ["env"]},
    )
    by_subject = {v.subject: v.detail for v in run(repo, ring=ring) if v.rule == "effect-via-import"}

    assert by_subject.keys() == {"app.ports", "app.adapters"}
    assert "app.domain" in by_subject["app.ports"] and "app.ports -> app.domain" not in by_subject["app.ports"]
    assert "app.ports -> app.domain" in by_subject["app.adapters"]


def test_effect_via_import_is_not_reported_when_the_importing_ring_allows_it(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import os\ndef read():\n    return os.getenv('MODE')\n",
        "app/ports.py": "from . import domain\n",
        "app/adapters.py": "X = 1\n",
    })
    ring = ring_with(
        domain={"external": ["os"], "forbid": ["env"]},
        ports={"may_import": ["domain"], "external": [], "forbid": []},
    )

    assert effects(run(repo, ring=ring)) == {("effect-env", "app.domain")}


def test_effect_via_import_does_not_propagate_through_a_type_checking_import(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import os\ndef read():\n    return os.getenv('MODE')\n",
        "app/ports.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import domain\n",
        "app/adapters.py": "X = 1\n",
    })
    ring = ring_with(
        domain={"external": ["os"], "forbid": ["env"]},
        ports={"may_import": ["domain"], "external": ["typing"], "forbid": ["env"]},
    )

    assert effects(run(repo, ring=ring)) == {("effect-env", "app.domain")}
